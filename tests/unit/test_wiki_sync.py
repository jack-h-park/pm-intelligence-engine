"""Contract tests for wiki_sync.py — canonical path rendering.

These tests do NOT write to the real WIKI_ROOT. They use tmp_path fixtures
and assert on the paths returned by the functions, not on external repo state.

Contracts verified:
  - sync_executive_summary() writes to WIKI_ROOT/raw/from-pm-decision-context/{subdir}/
  - subdir mapping: prd→prds, poc→poc-upgrades, kill→kills
  - archive_auto_triaged() writes to raw/from-pm-decision-context/kills/auto-triaged/
  - brief/opportunity/evaluate are not valid routing values (ValueError)
  - filename convention: YYYY-MM-DD-<slug>.md
  - file content includes canonical wiki YAML frontmatter
"""

from unittest.mock import MagicMock, patch

import pytest

from app.models.stages import S2OutputData

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_s2_output(
    relevance_score: int = 2,
    what_changed: str = "NFC policy changed.",
    reframing: str = "Affects the platform's admin model.",
    reasoning: str = "Score below threshold.",
    pillars: list | None = None,
) -> S2OutputData:
    return S2OutputData(
        relevance_score=relevance_score,
        what_changed=what_changed,
        reframing=reframing,
        relevance_explanation="This signal affects the platform's admin API surface.",
        suggestion_reasoning=reasoning,
        pillar_references=pillars or ["Attack Surface Reduction"],
        suggested_mode="file",
    )


FIXED_DATE = "2026-05-24"


def test_archive_auto_triaged_if_enabled_skips_when_flag_disabled():
    """The transitional local archive path can be disabled for an ops cutover."""
    from app.api.runs import _archive_auto_triaged_if_enabled

    settings_obj = MagicMock()
    settings_obj.AUTO_TRIAGE_LOCAL_ARCHIVE_ENABLED = False
    settings_obj.WIKI_ROOT = "/tmp/wiki"

    with patch("app.api.runs._archive_auto_triaged") as mock_archive:
        _archive_auto_triaged_if_enabled(
            run_id="run-abc",
            product_id="example-security-product",
            signal_title="Signal",
            s2_output=_make_s2_output(),
            settings_obj=settings_obj,
        )

    mock_archive.assert_not_called()


def test_archive_auto_triaged_if_enabled_calls_legacy_helper_when_flag_enabled():
    """The transitional local archive path remains callable until Hermes takeover."""
    from app.api.runs import _archive_auto_triaged_if_enabled

    settings_obj = MagicMock()
    settings_obj.AUTO_TRIAGE_LOCAL_ARCHIVE_ENABLED = True
    settings_obj.WIKI_ROOT = "/tmp/wiki"

    with patch("app.api.runs._archive_auto_triaged") as mock_archive:
        _archive_auto_triaged_if_enabled(
            run_id="run-abc",
            product_id="example-security-product",
            signal_title="Signal",
            s2_output=_make_s2_output(),
            settings_obj=settings_obj,
        )

    mock_archive.assert_called_once()


# ---------------------------------------------------------------------------
# sync_executive_summary — canonical path mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("routing,expected_subdir", [
    ("prd", "prds"),
    ("poc", "poc-upgrades"),
    ("kill", "kills"),
])
def test_sync_executive_summary_path_mapping(routing, expected_subdir, tmp_path):
    """sync_executive_summary writes to the correct subdir under raw/from-pm-decision-context/."""
    from app.services.wiki_sync import sync_executive_summary

    with patch("app.services.wiki_sync.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = FIXED_DATE

        path = sync_executive_summary(
            run_id="run-abc",
            product_id="example-security-product",
            routing=routing,
            markdown="# Executive Summary\n\nContent here.",
            signal_title="Android 16 NFC allowlist",
            wiki_root=str(tmp_path),
        )

    expected_dir = tmp_path / "raw" / "from-pm-decision-context" / expected_subdir
    assert path.parent == expected_dir
    assert expected_dir.exists()


def test_sync_executive_summary_filename_convention(tmp_path):
    """Filename must be YYYY-MM-DD-<slugified-title>.md."""
    from app.services.wiki_sync import sync_executive_summary

    with patch("app.services.wiki_sync.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = FIXED_DATE

        path = sync_executive_summary(
            run_id="run-abc",
            product_id="example-security-product",
            routing="prd",
            markdown="# Executive Summary",
            signal_title="Android 16 NFC Allowlist",
            wiki_root=str(tmp_path),
        )

    assert path.name.startswith(FIXED_DATE)
    assert path.suffix == ".md"
    assert "android" in path.name


def test_sync_executive_summary_frontmatter_content(tmp_path):
    """Written file must contain the canonical wiki YAML frontmatter fields."""
    from app.services.wiki_sync import sync_executive_summary

    with patch("app.services.wiki_sync.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = FIXED_DATE

        path = sync_executive_summary(
            run_id="run-frontmatter-test",
            product_id="example-security-product",
            routing="prd",
            markdown="# Report",
            signal_title="Test signal",
            wiki_root=str(tmp_path),
        )

    content = path.read_text(encoding="utf-8")
    assert "source: decision-context-companion-repo" in content
    assert "run: run-frontmatter-test" in content
    assert "type: prd" in content
    assert f"date: {FIXED_DATE}" in content
    assert "review_needed: false" in content


def test_sync_executive_summary_frontmatter_maps_poc_to_poc_upgrade(tmp_path):
    """PoC routing must be written using the wiki type name poc-upgrade."""
    from app.services.wiki_sync import sync_executive_summary

    with patch("app.services.wiki_sync.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = FIXED_DATE

        path = sync_executive_summary(
            run_id="run-poc-test",
            product_id="example-security-product",
            routing="poc",
            markdown="# Report",
            signal_title="Test signal",
            wiki_root=str(tmp_path),
        )

    content = path.read_text(encoding="utf-8")
    assert "type: poc-upgrade" in content


def test_sync_executive_summary_body_appended_after_frontmatter(tmp_path):
    """Markdown body must appear after the YAML frontmatter block."""
    from app.services.wiki_sync import sync_executive_summary

    with patch("app.services.wiki_sync.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = FIXED_DATE

        path = sync_executive_summary(
            run_id="run-body-test",
            product_id="example-security-product",
            routing="prd",
            markdown="# Executive Summary\n\nDetailed content.",
            signal_title="Test signal body",
            wiki_root=str(tmp_path),
        )

    content = path.read_text(encoding="utf-8")
    assert "---\n\n# Executive Summary" in content or "---\n# Executive Summary" in content


def test_sync_executive_summary_invalid_routing_raises():
    """Passing an invalid routing to sync_executive_summary raises ValueError."""
    from app.services.wiki_sync import sync_executive_summary

    with pytest.raises(ValueError, match="Unknown routing"):
        sync_executive_summary(
            run_id="run-abc",
            product_id="example-security-product",
            routing="brief",  # not a valid wiki sync routing
            markdown="# Report",
            signal_title="Test",
            wiki_root="/tmp/nonexistent-wiki",
        )


@pytest.mark.parametrize("invalid_routing", ["brief", "opportunity", "evaluate", "file", ""])
def test_sync_executive_summary_rejects_non_wiki_routings(invalid_routing, tmp_path):
    """brief/opportunity/evaluate/file are not wiki sync targets — must raise ValueError."""
    from app.services.wiki_sync import sync_executive_summary

    with pytest.raises(ValueError):
        sync_executive_summary(
            run_id="run-abc",
            product_id="example-security-product",
            routing=invalid_routing,
            markdown="# Report",
            signal_title="Test",
            wiki_root=str(tmp_path),
        )


# ---------------------------------------------------------------------------
# archive_auto_triaged — path contract
# ---------------------------------------------------------------------------


def test_archive_auto_triaged_path(tmp_path):
    """archive_auto_triaged writes to raw/from-pm-decision-context/kills/auto-triaged/."""
    from app.services.wiki_sync import archive_auto_triaged

    s2 = _make_s2_output()

    with patch("app.services.wiki_sync.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = FIXED_DATE

        path = archive_auto_triaged(
            run_id="run-triage",
            product_id="example-security-product",
            signal_title="DISA mandate",
            s2_output=s2,
            wiki_root=str(tmp_path),
        )

    expected_dir = tmp_path / "raw" / "from-pm-decision-context" / "kills" / "auto-triaged"
    assert path.parent == expected_dir
    assert expected_dir.exists()
    assert path.suffix == ".md"


def test_archive_auto_triaged_filename(tmp_path):
    """Auto-triage archive filename starts with date and includes slugified title."""
    from app.services.wiki_sync import archive_auto_triaged

    with patch("app.services.wiki_sync.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = FIXED_DATE

        path = archive_auto_triaged(
            run_id="run-triage",
            product_id="example-security-product",
            signal_title="DISA MTD Mandate",
            s2_output=_make_s2_output(),
            wiki_root=str(tmp_path),
        )

    assert path.name.startswith(FIXED_DATE)
    assert "disa" in path.name


def test_archive_auto_triaged_content_includes_key_fields(tmp_path):
    """Auto-triage archive content must include score, title, and reasoning."""
    from app.services.wiki_sync import archive_auto_triaged

    s2 = _make_s2_output(
        relevance_score=1,
        what_changed="DISA published new STIG requirements.",
        reframing="Requires a competing solution.",
        reasoning="Score too low; no platform-specific impact.",
    )

    with patch("app.services.wiki_sync.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = FIXED_DATE

        path = archive_auto_triaged(
            run_id="run-triage-content",
            product_id="example-security-product",
            signal_title="DISA MTD Mandate",
            s2_output=s2,
            wiki_root=str(tmp_path),
        )

    content = path.read_text(encoding="utf-8")
    assert "DISA MTD Mandate" in content
    assert "1/5" in content
    assert "DISA published new STIG requirements." in content
    assert "Score too low" in content


def test_archive_auto_triaged_frontmatter(tmp_path):
    """Auto-triage archive must include YAML frontmatter with triage: auto."""
    from app.services.wiki_sync import archive_auto_triaged

    with patch("app.services.wiki_sync.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = FIXED_DATE

        path = archive_auto_triaged(
            run_id="run-frontmatter",
            product_id="example-security-product",
            signal_title="Low relevance signal",
            s2_output=_make_s2_output(),
            wiki_root=str(tmp_path),
        )

    content = path.read_text(encoding="utf-8")
    assert "triage: auto" in content
    assert "run_id: run-frontmatter" in content


# ---------------------------------------------------------------------------
# Decision-system export path stability
# ---------------------------------------------------------------------------


def test_decision_system_export_path_uses_canonical_archive_only(tmp_path):
    """Export returns the canonical pm-engine archive path only."""
    from app.services.run_exporter import export_run

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
    store.get_signal.return_value = {
        "signal_id": "sig-123",
        "title": "Android 16 NFC Allowlist",
        "raw_content": "Full signal text.",
    }
    store.get_all_stage_outputs.return_value = []
    store.get_stage_output.return_value = None

    legacy_root = tmp_path / "legacy"
    canonical_root = tmp_path / "archive" / "runs"

    with patch("app.services.run_exporter.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = FIXED_DATE
        with patch("app.services.run_exporter._canonical_archive_root", return_value=canonical_root):
            path = export_run(
                run_id="run-export-test",
                store=store,
                decision_system_root=str(legacy_root),
            )

    legacy_path = (
        legacy_root / "products" / "example-security-product" / "runs"
        / "2026-05-24-run-export-test"
    )

    assert path == canonical_root / "example-security-product" / "2026-05-24-run-export-test"
    assert path.exists()
    assert not legacy_path.exists()


def test_wiki_sync_not_called_from_run_exporter():
    """run_exporter.export_run must not import or call wiki_sync functions."""
    import ast
    import inspect

    from app.services import run_exporter

    source = inspect.getsource(run_exporter)
    tree = ast.parse(source)

    # Check that wiki_sync is not imported at all
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else ([node.module] if node.module else [])
            )
            for name in names:
                assert "wiki_sync" not in (name or ""), (
                    "run_exporter must not import wiki_sync — "
                    "wiki sync is Hermes-owned per EXPORT_AND_SYNC_CONTRACT.md"
                )
