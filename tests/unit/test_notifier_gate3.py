"""Unit tests for the enriched Gate 3 notification rendering (US-30)."""

import pytest
from unittest.mock import AsyncMock, patch

from app.services.notifier import FanoutNotifier, TelegramNotifier

_ASSUMPTIONS = [
    {"statement": "Platform API ships in GA", "severity": "Blocking", "reason": "r"},
    {"statement": "Admins want unified enforcement", "severity": "Adjusting", "reason": "r"},
]
_PERSONA_LINES = [
    "Explorer (Impact) 4/5 — Opens adjacent market.",
    "Skeptic (Confidence) 3/5 — Adoption assumption untested.",
]


@pytest.mark.asyncio
async def test_telegram_gate3_renders_enriched_fields():
    notifier = TelegramNotifier(bot_token="t", chat_id="c")
    with patch.object(notifier, "_send", new=AsyncMock()) as send:
        await notifier.send_gate3(
            run_id="r1",
            product_id="p1",
            signal_title="Sig",
            routing="poc",
            composite_score=4.3,
            blocking_count=1,
            assumptions=_ASSUMPTIONS,
            persona_lines=_PERSONA_LINES,
            rubric_total="11/12",
        )
    text = send.call_args.args[0]
    assert "S4 rubric: <b>11/12</b>" in text
    assert "[Blocking] Platform API ships in GA" in text
    assert "[Adjusting] Admins want unified enforcement" in text
    assert "Skeptic (Confidence) 3/5" in text
    assert "Assumptions (1 blocking)" in text


@pytest.mark.asyncio
async def test_telegram_gate3_backward_compatible_without_enrichment():
    """Old call shape (ops-plane skill before payload update) still works."""
    notifier = TelegramNotifier(bot_token="t", chat_id="c")
    with patch.object(notifier, "_send", new=AsyncMock()) as send:
        await notifier.send_gate3(
            run_id="r1",
            product_id="p1",
            signal_title="Sig",
            routing="kill",
            composite_score=1.4,
            blocking_count=2,
        )
    text = send.call_args.args[0]
    assert "[Gate 3] Routing Review — KILL" in text
    assert "Assumptions" not in text  # no enrichment provided, no empty section


@pytest.mark.asyncio
async def test_fanout_passes_enrichment_through():
    provider = AsyncMock()
    fanout = FanoutNotifier([provider])
    await fanout.send_gate3(
        run_id="r1",
        product_id="p1",
        signal_title="Sig",
        routing="prd",
        composite_score=4.5,
        blocking_count=0,
        assumptions=_ASSUMPTIONS,
        persona_lines=_PERSONA_LINES,
        rubric_total="10/12",
    )
    kwargs = provider.send_gate3.call_args.kwargs
    assert kwargs["assumptions"] == _ASSUMPTIONS
    assert kwargs["persona_lines"] == _PERSONA_LINES
    assert kwargs["rubric_total"] == "10/12"


@pytest.mark.asyncio
async def test_telegram_gate1_renders_s2_insight():
    notifier = TelegramNotifier(bot_token="t", chat_id="c")
    with patch.object(notifier, "_send", new=AsyncMock()) as send:
        await notifier.send_gate1(
            run_id="r1", product_id="p1", signal_title="Sig",
            relevance_score=5, suggested_mode="note", reasoning="why",
            what_changed="APM enables MTE",
            relevance_explanation="provable memory-safety posture required",
            pillar_references=["Hardware-rooted security"],
        )
    text = send.call_args.args[0]
    assert "What changed:" in text and "APM enables MTE" in text
    assert "Why it matters:" in text and "provable memory-safety" in text
    assert "Hardware-rooted security" in text
    assert "Suggested depth: <b>note</b>" in text
    assert '{"depth": "note"}' in text
    assert "archive" in text and "decide" in text  # ladder reminder


@pytest.mark.asyncio
async def test_gate_notifications_flag_disables_all_push():
    """GATE_NOTIFICATIONS_ENABLED=false → build_notifier wires no providers (US-48)."""
    from app.services.notifier import build_notifier
    with patch("config.settings.GATE_NOTIFICATIONS_ENABLED", False), \
         patch("config.settings.TELEGRAM_BOT_TOKEN", "t"), \
         patch("config.settings.TELEGRAM_CHAT_ID", "c"), \
         patch("config.settings.SLACK_WEBHOOK_URL", "https://hook"):
        n = build_notifier()
    assert not n.is_configured  # despite creds present, disabled → no providers
    # all gate sends are silent no-ops (do not raise)
    await n.send_gate1(run_id="r", product_id="p", signal_title="s",
                       relevance_score=3, suggested_mode="note", reasoning="x")
    await n.send_gate3(run_id="r", product_id="p", signal_title="s",
                       routing="kill", composite_score=1.0, blocking_count=0)


@pytest.mark.asyncio
async def test_gate_notifications_flag_enabled_wires_providers():
    from app.services.notifier import build_notifier
    with patch("config.settings.GATE_NOTIFICATIONS_ENABLED", True), \
         patch("config.settings.TELEGRAM_BOT_TOKEN", "t"), \
         patch("config.settings.TELEGRAM_CHAT_ID", "c"), \
         patch("config.settings.SLACK_WEBHOOK_URL", ""):
        n = build_notifier()
    assert n.is_configured
