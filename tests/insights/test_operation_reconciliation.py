import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import func, select

from app.models.insights import (
    BudgetReservation,
    IntelligenceBudgetReservationRow,
    IntelligenceTriageReconciliationRow,
)


def _seed_unknown_operation(store, operation_id="op-unknown"):
    from app.models.insights import IntelligenceTriageRow

    with store.engine.begin() as connection:
        connection.execute(IntelligenceTriageRow.__table__.insert().values(
            operation_id=operation_id, state="running", payload_json="{}"
        ))
        reservation = BudgetReservation(
            operation_id=operation_id,
            operation_type="semantic_triage",
            policy_revision="fixture-v1",
            provider="openai-oauth",
            rate_revision="fixture-v1",
            maximum_micros=100000,
            allowance_class="sensing",
            state="unknown",
        )
        connection.execute(IntelligenceBudgetReservationRow.__table__.insert().values(
            reservation_id=reservation.reservation_id,
            operation_id=operation_id,
            allowance_class=reservation.allowance_class,
            state=reservation.state,
            maximum_micros=reservation.maximum_micros,
            payload_json=json.dumps(reservation.model_dump(mode="json")),
        ))
    return reservation.reservation_id


def test_reconcile_unknown_fences_operation_and_preserves_reservation(client, monkeypatch):
    from app.api.deps import get_engine
    from config import settings

    engine = client.app.dependency_overrides[get_engine]()
    store = engine.insight_store
    reservation_id = _seed_unknown_operation(store)
    monkeypatch.setattr(settings, "S2K_RECONCILIATION_TOKEN", "operator-secret")
    monkeypatch.setattr(settings, "S2K_RECONCILIATION_OPERATOR_ID", "jack-park")

    response = client.post(
        "/insight-operations/op-unknown/reconcile-unknown",
        headers={"Authorization": "Bearer insight-test-token",
                 "X-S2K-Reconciliation-Token": "operator-secret"},
        json={"reason": "Provider activity was reviewed; outcome remains unknown."},
    )

    assert response.status_code == 200
    assert response.json()["triage_state"] == "terminal_unknown"
    assert response.json()["operator_id"] == "jack-park"
    assert store.get_operation_status("op-unknown")["triage_state"] == "terminal_unknown"
    with store.engine.connect() as connection:
        reservation = connection.execute(
            IntelligenceBudgetReservationRow.__table__.select().where(
                IntelligenceBudgetReservationRow.operation_id == "op-unknown"
            )
        ).mappings().one()
        audits = connection.execute(
            IntelligenceTriageReconciliationRow.__table__.select()
        ).mappings().all()
    assert reservation["reservation_id"] == reservation_id
    assert reservation["state"] == "unknown"
    assert json.loads(reservation["payload_json"])["actual_micros"] is None
    assert len(audits) == 1
    assert audits[0]["reason"] == "Provider activity was reviewed; outcome remains unknown."


def test_late_provider_completion_cannot_overwrite_terminal_unknown(client, monkeypatch):
    from app.api.deps import get_engine
    from app.storage.insight_store import TriageOperationNotRunning
    from config import settings

    store = client.app.dependency_overrides[get_engine]().insight_store
    _seed_unknown_operation(store)
    monkeypatch.setattr(settings, "S2K_RECONCILIATION_TOKEN", "operator-secret")
    monkeypatch.setattr(settings, "S2K_RECONCILIATION_OPERATOR_ID", "jack-park")
    reconciled = client.post(
        "/insight-operations/op-unknown/reconcile-unknown",
        headers={"Authorization": "Bearer insight-test-token",
                 "X-S2K-Reconciliation-Token": "operator-secret"},
        json={"reason": "Provider outcome remains unresolved."},
    )
    assert reconciled.status_code == 200

    with pytest.raises(TriageOperationNotRunning):
        store.complete_triage("op-unknown", {"disposition": "admit"})

    operation = store.get_operation_status("op-unknown")
    assert operation["triage_state"] == "terminal_unknown"
    assert operation["has_triage_result"] is False


def test_concurrent_reconciliation_returns_one_original_audit(client, monkeypatch):
    from app.api.deps import get_engine
    from config import settings

    store = client.app.dependency_overrides[get_engine]().insight_store
    _seed_unknown_operation(store)
    monkeypatch.setattr(settings, "S2K_RECONCILIATION_TOKEN", "operator-secret")
    monkeypatch.setattr(settings, "S2K_RECONCILIATION_OPERATOR_ID", "jack-park")
    headers = {"Authorization": "Bearer insight-test-token",
               "X-S2K-Reconciliation-Token": "operator-secret"}
    url = "/insight-operations/op-unknown/reconcile-unknown"
    barrier = Barrier(2)

    def reconcile(reason):
        barrier.wait()
        return client.post(url, headers=headers, json={"reason": reason})

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(reconcile, ["Reviewed first.", "Reviewed second."]))

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    with store.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(
            IntelligenceTriageReconciliationRow.__table__
        )) == 1


@pytest.mark.parametrize("headers,expected", [
    ({"Authorization": "Bearer insight-test-token"}, 401),
    ({"Authorization": "Bearer insight-test-token",
      "X-S2K-Reconciliation-Token": "wrong"}, 401),
])
def test_reconcile_unknown_requires_dedicated_credential(client, monkeypatch, headers, expected):
    from config import settings

    monkeypatch.setattr(settings, "S2K_RECONCILIATION_TOKEN", "operator-secret")
    monkeypatch.setattr(settings, "S2K_RECONCILIATION_OPERATOR_ID", "jack-park")
    response = client.post("/insight-operations/op-unknown/reconcile-unknown", headers=headers,
                           json={"reason": "Reviewed."})
    assert response.status_code == expected


def test_reconcile_unknown_rejects_blank_reason(client, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "S2K_RECONCILIATION_TOKEN", "operator-secret")
    monkeypatch.setattr(settings, "S2K_RECONCILIATION_OPERATOR_ID", "jack-park")
    response = client.post(
        "/insight-operations/op-unknown/reconcile-unknown",
        headers={"Authorization": "Bearer insight-test-token",
                 "X-S2K-Reconciliation-Token": "operator-secret"},
        json={"reason": "  "},
    )
    assert response.status_code == 422


def test_reconcile_unknown_refuses_non_running_operation(client, monkeypatch):
    from app.api.deps import get_engine
    from app.models.insights import IntelligenceTriageRow
    from config import settings

    store = client.app.dependency_overrides[get_engine]().insight_store
    _seed_unknown_operation(store)
    with store.engine.begin() as connection:
        connection.execute(IntelligenceTriageRow.__table__.update().values(state="complete"))
    monkeypatch.setattr(settings, "S2K_RECONCILIATION_TOKEN", "operator-secret")
    monkeypatch.setattr(settings, "S2K_RECONCILIATION_OPERATOR_ID", "jack-park")
    response = client.post(
        "/insight-operations/op-unknown/reconcile-unknown",
        headers={"Authorization": "Bearer insight-test-token",
                 "X-S2K-Reconciliation-Token": "operator-secret"},
        json={"reason": "Reviewed."},
    )
    assert response.status_code == 409
