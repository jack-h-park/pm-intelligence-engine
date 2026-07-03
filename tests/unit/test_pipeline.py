"""Unit tests for the stage registry (app/pipeline.py).

Two jobs:
  1. The registry is internally correct (positions, depths, pause points).
  2. It is *equivalent* to every scattered definition it now sources — so wiring
     `MODES`, `ended_by`, and the `_VALID_MODES` sets to derive from it changed no
     behavior. These equivalence tests are the safety net for the consolidation.
"""

from app import pipeline


# ---------------------------------------------------------------------------
# Registry correctness
# ---------------------------------------------------------------------------


def test_positions_in_pipeline_order():
    assert [s.id for s in pipeline.STAGES] == [
        "s1", "s2", "s3", "s4", "s5", "s6a", "s6b", "s7"
    ]


def test_depths_are_the_five_stop_positions_in_order():
    assert pipeline.depths() == ("archive", "note", "structure", "evaluate", "decide")


def test_intermediate_positions_have_no_depth_or_completion_reason():
    for sid in ("s5", "s6a", "s6b"):
        s = pipeline.by_id(sid)
        assert s.stop_depth is None
        assert s.ended_by is None


def test_pause_positions_are_the_three_gates():
    # Gate 1 (after S2), Gate 2 (after S4), Gate 3 (after S5).
    assert pipeline.pause_positions() == ("s2", "s4", "s5")


def test_position_for_depth_roundtrip():
    assert pipeline.position_for_depth("archive") == "s1"
    assert pipeline.position_for_depth("note") == "s2"
    assert pipeline.position_for_depth("structure") == "s3"
    assert pipeline.position_for_depth("evaluate") == "s4"
    assert pipeline.position_for_depth("decide") == "s7"


def test_s1_produces_no_artifact():
    s1 = pipeline.by_id("s1")
    assert s1.artifact_type is None
    assert s1.artifact_label is None


def test_by_id_unknown_raises():
    import pytest
    with pytest.raises(KeyError):
        pipeline.by_id("s99")


# ---------------------------------------------------------------------------
# Equivalence with the definitions the registry now sources
# ---------------------------------------------------------------------------


def test_modes_constant_derives_from_registry():
    from app.modes import MODES
    assert MODES == pipeline.depths()


def test_runmode_enum_matches_registry_depths():
    from app.models.workflow import RunMode
    assert {m.value for m in RunMode} == set(pipeline.depths())


def test_ended_by_map_matches_registry():
    # The run_finalizer completion map must equal the registry derivation.
    from app.services.run_finalizer import _ENDED_BY_FROM_MODE
    assert _ENDED_BY_FROM_MODE == pipeline.ended_by_by_depth()
    assert _ENDED_BY_FROM_MODE == {
        "archive": "archived", "note": "noted", "structure": "structured",
        "evaluate": "evaluated", "decide": "decided",
    }


def test_valid_modes_sets_derive_from_registry():
    from app.api.direction import _VALID_MODES
    assert _VALID_MODES == set(pipeline.depths())


def test_artifact_types_are_valid_artifact_type_values():
    # Every artifact a stage produces must be a real ArtifactType (minus the
    # dropped `checkpoint`, which the registry intentionally omits).
    from app.models.workflow import ArtifactType
    valid = {t.value for t in ArtifactType}
    for s in pipeline.STAGES:
        if s.artifact_type is not None:
            assert s.artifact_type in valid


def test_registry_artifact_types_match_known_stage_outputs():
    # Locks the position→artifact mapping the stage code emits today.
    assert pipeline.artifact_type_for("s2") == "insight_memo"
    assert pipeline.artifact_type_for("s3") == "opportunity_memo"
    assert pipeline.artifact_type_for("s4") == "evaluation_brief"
    assert pipeline.artifact_type_for("s5") == "decision_memo"
    assert pipeline.artifact_type_for("s6a") == "poc_plan"
    assert pipeline.artifact_type_for("s6b") == "prd"
    assert pipeline.artifact_type_for("s7") == "executive_summary"
