from types import SimpleNamespace
from unittest.mock import MagicMock, patch


FIXED_DATE = "2026-05-24"


def _make_store() -> MagicMock:
    store = MagicMock()
    store.get_run.return_value = {
        "run_id": "run-export-test",
        "product_id": "example-security-product",
        "signal_id": "sig-123",
        "status": "completed",
        "mode": "decide",
        "routing": "prd",
        "composite_score": 4.30,
        "created_at": "2026-05-24T00:00:00",
        "completed_at": "2026-05-24T01:00:00",
    }
    return store


def test_export_run_overwrites_existing_files_on_repeat_export(tmp_path):
    """Repeated export for the same run/date/slug overwrites the prior file content."""
    from app.services.run_exporter import export_run

    store = _make_store()

    s1 = SimpleNamespace(
        signal_id="sig-123",
        title="Android 16 NFC Allowlist",
        summary="Signal summary.",
        category="platform",
        source="https://example.com",
        event_date="2026-05-24",
    )
    s7_first = SimpleNamespace(markdown="# First summary")
    s7_second = SimpleNamespace(markdown="# Second summary")

    with patch("app.services.run_exporter.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = FIXED_DATE

        with patch(
            "app.services.run_exporter._load_stage",
            side_effect=[s1, None, None, None, None, None, None, s7_first],
        ):
            path_first = export_run(
                run_id="run-export-test",
                store=store,
                decision_system_root=str(tmp_path),
            )

        first_content = (path_first / "s7-report.md").read_text(encoding="utf-8")
        assert first_content == "# First summary"

        with patch(
            "app.services.run_exporter._load_stage",
            side_effect=[s1, None, None, None, None, None, None, s7_second],
        ):
            path_second = export_run(
                run_id="run-export-test",
                store=store,
                decision_system_root=str(tmp_path),
            )

    assert path_first == path_second
    second_content = (path_second / "s7-report.md").read_text(encoding="utf-8")
    assert second_content == "# Second summary"


def test_export_run_writes_expected_stage_file_set(tmp_path):
    """PRD-path export writes the documented markdown files only."""
    from app.services.run_exporter import export_run

    store = _make_store()

    s1 = SimpleNamespace(
        signal_id="sig-123",
        title="Android 16 NFC Allowlist",
        summary="Signal summary.",
        category="platform",
        source="https://example.com",
        event_date="2026-05-24",
    )
    s2 = SimpleNamespace(
        relevance_score=4,
        what_changed="Android added NFC allowlist support.",
        reframing="This is a managed API change.",
        relevance_explanation="Relevant to Knox controls.",
        pillar_references=["Attack Surface Reduction"],
        suggested_mode="structure",
        claims=[],
    )
    s3 = SimpleNamespace(
        problem_statement="Admins cannot control NFC safely.",
        target_user="Enterprise admin",
        hypothesis="If admins can allowlist NFC, policy control improves.",
        assumed_value_user="Stronger control.",
        assumed_value_business="More enterprise readiness.",
        value_horizon="durable",
    )
    s4 = SimpleNamespace(
        personas=[
            SimpleNamespace(persona="explorer", dimension="Impact", key_argument="Large impact.", score=4, open_question="How often used?"),
            SimpleNamespace(persona="skeptic", dimension="Confidence", key_argument="Needs OEM adoption.", score=3, open_question="API stability?"),
        ],
        rubric=SimpleNamespace(
            issues=[],
            passed=True,
            score_grounding=3,
            skeptic_quality=2,
            open_question_quality=2,
            persona_independence=2,
            total_score=9,
        ),
    )
    s5 = SimpleNamespace(
        impact_score=4,
        strategic_fit_score=4,
        feasibility_score=3,
        confidence_score=3,
        composite_score=3.7,
        assumptions=[],
        routing="prd",
        rationale="High enough to ship a PRD.",
        blocking_count=0,
        governing_heuristics=[],
        closing_window=False,
    )
    s6b = SimpleNamespace(
        problem_statement="Admins need NFC allowlist controls.",
        target_user="Enterprise admin",
        success_metrics=["Reduced unmanaged NFC use", "Policy adoption"],
        user_stories=["As admin...", "As IT...", "As security lead..."],
        in_scope=["Policy control"],
        out_of_scope=["Consumer UX", "Hardware changes"],
        technical_dependencies=["Android 16 API"],
        open_questions=["OEM coverage"],
        risks=["API changes"],
        completeness=SimpleNamespace(
            score=12,
            problem_statement=True,
            target_user=True,
            hypothesis=True,
            success_metrics=True,
            user_stories=True,
            in_scope=True,
            out_of_scope=True,
            technical_dependencies=True,
            open_questions=True,
            non_goals=True,
            rollout_phases=True,
            risks=True,
        ),
    )
    s7 = SimpleNamespace(markdown="# Final report")

    with patch("app.services.run_exporter.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = FIXED_DATE
        with patch(
            "app.services.run_exporter._load_stage",
            side_effect=[s1, s2, s3, s4, s5, None, s6b, s7],
        ):
            path = export_run(
                run_id="run-export-test",
                store=store,
                decision_system_root=str(tmp_path),
            )

    assert sorted(p.name for p in path.iterdir()) == sorted(
        [
            "s1-signal.md",
            "s2-insight.md",
            "s3-opportunity.md",
            "s4-evaluation.md",
            "s4-evaluation-rubric-score.md",
            "s5-prioritization.md",
            "s6-prd.md",
            "s7-report.md",
        ]
    )


def test_export_run_writes_to_pm_engine_archive_root_and_returns_canonical_path(tmp_path):
    """Export writes to archive/runs/<product>/<date-slug>/ and returns that canonical path."""
    from app.services.run_exporter import export_run

    store = _make_store()
    s1 = SimpleNamespace(
        signal_id="sig-123",
        title="Android 16 NFC Allowlist",
        summary="Signal summary.",
        category="platform",
        source="https://example.com",
        event_date="2026-05-24",
    )
    s7 = SimpleNamespace(markdown="# Final report")

    with patch("app.services.run_exporter.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = FIXED_DATE
        with patch(
            "app.services.run_exporter._load_stage",
            side_effect=[s1, None, None, None, None, None, None, s7],
        ):
            with patch("app.services.run_exporter._canonical_archive_root", return_value=tmp_path / "archive" / "runs"):
                path = export_run(
                    run_id="run-export-test",
                    store=store,
                    decision_system_root=str(tmp_path / "legacy"),
                )

    assert path == tmp_path / "archive" / "runs" / "example-security-product" / "2026-05-24-android-16-nfc-allowlist"
    assert (path / "s1-signal.md").exists()
    assert (path / "s7-report.md").exists()


def test_export_run_does_not_write_legacy_path(tmp_path):
    """Export writes only to the canonical pm-engine archive path."""
    from app.services.run_exporter import export_run

    store = _make_store()
    s1 = SimpleNamespace(
        signal_id="sig-123",
        title="Android 16 NFC Allowlist",
        summary="Signal summary.",
        category="platform",
        source="https://example.com",
        event_date="2026-05-24",
    )
    s7 = SimpleNamespace(markdown="# Final report")

    legacy_root = tmp_path / "legacy"
    canonical_root = tmp_path / "archive" / "runs"

    with patch("app.services.run_exporter.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = FIXED_DATE
        with patch(
            "app.services.run_exporter._load_stage",
            side_effect=[s1, None, None, None, None, None, None, s7],
        ):
            with patch("app.services.run_exporter._canonical_archive_root", return_value=canonical_root):
                canonical_path = export_run(
                    run_id="run-export-test",
                    store=store,
                    decision_system_root=str(legacy_root),
                )

    legacy_path = legacy_root / "products" / "example-security-product" / "runs" / "2026-05-24-android-16-nfc-allowlist"
    assert canonical_path == canonical_root / "example-security-product" / "2026-05-24-android-16-nfc-allowlist"
    assert (canonical_path / "s7-report.md").read_text(encoding="utf-8") == "# Final report"
    assert not legacy_path.exists()


def test_archive_body_matches_the_stage_artifact_body():
    """The archive file and the DB artifact are one document, not two.

    Both are renderings of a single stage output. They used to come from
    separate templates, so the same facts were phrased three ways and every
    consumer had to guess whether the difference was meaningful. The stage
    module now owns the body; the archive adds only a heading and metadata
    block above the first `## `, which is exactly where the Observatory's
    duplicate check starts, so the two collapse into one document there.
    """
    from app.models.stages import S2OutputData, S3OutputData
    from app.services.run_exporter import _render_s2, _render_s3, _sections_of
    from app.stages.s2_insight import build_insight_memo
    from app.stages.s3_opportunity import build_opportunity_memo

    s1 = SimpleNamespace(signal_id="sig-123", title="Android 16 NFC Allowlist", category="platform")
    s2 = S2OutputData(
        what_changed="Android added NFC allowlist support.",
        reframing="This is a managed API change.",
        relevance_explanation="Relevant to Knox controls.",
        relevance_score=4,
        suggested_mode="structure",
        suggestion_reasoning="Directly touches a named pillar.",
        pillar_references=["Attack Surface Reduction"],
    )
    s3 = S3OutputData(
        problem_statement="Admins cannot control NFC safely.",
        target_user="Enterprise admin",
        hypothesis="If admins can allowlist NFC, policy control improves.",
        assumed_value_user="Stronger control.",
        assumed_value_business="More enterprise readiness.",
        value_horizon="durable",
    )

    memo2 = build_insight_memo(s1.title, s1.category, s2)
    memo3 = build_opportunity_memo(s3)
    assert _sections_of(_render_s2(s2, s1, FIXED_DATE)) == _sections_of(memo2)
    assert _sections_of(_render_s3(s3, FIXED_DATE)) == _sections_of(memo3)

    # The archive still carries its own heading + metadata above that point —
    # that part is allowed to differ, and the run matcher depends on it.
    assert _render_s2(s2, s1, FIXED_DATE).startswith("# Stage 2: Insight Extraction")
    assert f"**Signal ref:** {s1.signal_id}" in _render_s2(s2, s1, FIXED_DATE)
