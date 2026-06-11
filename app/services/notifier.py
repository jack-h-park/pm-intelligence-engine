"""Notification service — PM gate alerts via Telegram and/or Slack.

Architecture
------------
TelegramNotifier and SlackNotifier each implement the same two methods.
FanoutNotifier wraps any number of providers and fans out to all of them.
build_notifier() reads config and returns a FanoutNotifier with only the
providers whose credentials are present.

Callers (runs.py, approvals.py) interact exclusively with FanoutNotifier —
they never need to know which providers are active.

Provider failure is non-fatal: a network error in Telegram must never block
a PRD from being generated. Each provider failure is logged and swallowed.

Gate messages
-------------
Gate 1 (awaiting_direction): fired after Stage 2; tells PM what mode S2
    recommends and how to proceed via the API.
Gate 2 (waiting_approval): fired after Stage 4; shows the 4-persona scores,
    the Skeptic's key concern, and the three approval endpoints.
"""

from __future__ import annotations

import httpx

from app.logging import emit_event


def _short(text: str, limit: int = 110) -> str:
    """Truncate long arguments so gate messages stay within provider limits."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._base = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id

    async def send_gate1(
        self,
        run_id: str,
        product_id: str,
        signal_title: str,
        relevance_score: int,
        suggested_mode: str,
        reasoning: str,
        what_changed: str = "",
        relevance_explanation: str = "",
        pillar_references: list[str] | None = None,
    ) -> None:
        pillars = ", ".join(pillar_references) if pillar_references else "—"
        changed_block = f"\n<b>What changed:</b> {_short(what_changed, 280)}\n" if what_changed else ""
        why_block = f"<b>Why it matters:</b> {_short(relevance_explanation, 280)}\n" if relevance_explanation else ""
        text = (
            f"📡 <b>[Gate 1] New Signal</b>\n\n"
            f"Product: <code>{product_id}</code>\n"
            f"Title: {signal_title}\n"
            f"Relevance: <b>{relevance_score}/5</b> · Suggested depth: <b>{suggested_mode}</b>\n"
            f"{changed_block}"
            f"{why_block}"
            f"Pillars: {pillars}\n"
            f"<i>\"{reasoning}\"</i>\n\n"
            f"Depth ladder: archive &lt; note &lt; structure &lt; evaluate &lt; decide\n"
            f"To proceed:\n"
            f"<code>POST /runs/{run_id}/direction</code>\n"
            f'<code>{{"depth": "{suggested_mode}"}}</code>'
        )
        await self._send(text, run_id)

    async def send_gate2(
        self,
        run_id: str,
        product_id: str,
        signal_title: str,
        explorer_score: int,
        strategist_score: int,
        builder_score: int,
        skeptic_score: int,
        key_concern: str,
        review_url: str = "",
    ) -> None:
        link_line = (
            f'\n\n🔗 <a href="{review_url}">Open Review Page</a>'
            if review_url else ""
        )
        text = (
            f"🧠 <b>[Gate 2] S4 Evaluation Ready</b>\n\n"
            f"Product: <code>{product_id}</code>\n"
            f"Signal: {signal_title}\n"
            f"Run: <code>{run_id}</code>\n\n"
            f"Explorer: {explorer_score} · Strategist: {strategist_score} · "
            f"Builder: {builder_score} · Skeptic: {skeptic_score}\n"
            f"<i>\"{key_concern}\"</i>"
            f"{link_line}"
        )
        await self._send(text, run_id)

    async def send_gate3(
        self,
        run_id: str,
        product_id: str,
        signal_title: str,
        routing: str,
        composite_score: float,
        blocking_count: int,
        assumptions: list[dict] | None = None,
        persona_lines: list[str] | None = None,
        rubric_total: str | None = None,
        closing_window: bool = False,
    ) -> None:
        icon = {"kill": "⚠️", "poc": "🔬", "prd": "📋"}.get(routing, "🔀")
        routing_label = routing.upper()
        rubric_part = f" · S4 rubric: <b>{rubric_total}</b>" if rubric_total else ""
        closing_line = (
            "\n⏳ <b>Closing window</b> — value is transient and Impact is high; "
            f"consider a fast time-boxed bet over the default {routing_label}.\n"
            if closing_window else ""
        )
        persona_block = (
            "\n" + "\n".join(f"• {_short(line)}" for line in persona_lines) + "\n"
            if persona_lines else ""
        )
        assumption_block = ""
        if assumptions:
            rows = "\n".join(
                f"{'❗' if a.get('severity') == 'Blocking' else '·'} "
                f"[{a.get('severity', '?')}] {_short(a.get('statement', ''))}"
                for a in assumptions
            )
            assumption_block = f"\n<b>Assumptions ({blocking_count} blocking):</b>\n{rows}\n"
        overrides = [r for r in ("kill", "poc", "prd") if r != routing]
        override_lines = "\n".join(
            f'<code>{{"action": "override", "routing": "{r}"}}</code>  → {r.upper()}'
            for r in overrides
        )
        text = (
            f"{icon} <b>[Gate 3] Routing Review — {routing_label}</b>\n\n"
            f"Product: <code>{product_id}</code>\n"
            f"Signal: {signal_title}\n"
            f"Run: <code>{run_id}</code>\n\n"
            f"Composite: <b>{composite_score:.1f}</b>{rubric_part}\n"
            f"{closing_line}"
            f"{persona_block}"
            f"{assumption_block}\n"
            f"S5 recommends <b>{routing_label}</b>. Confirm or override:\n"
            f"<code>POST /runs/{run_id}/routing-review</code>\n"
            f'<code>{{"action": "confirm"}}</code>  → {routing_label}\n'
            f"{override_lines}"
        )
        await self._send(text, run_id)

    async def _send(self, text: str, run_id: str) -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._base,
                json={"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10.0,
            )
            resp.raise_for_status()
        emit_event("notifier", "telegram_sent", run_id)


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


class SlackNotifier:
    def __init__(self, webhook_url: str) -> None:
        self._webhook = webhook_url

    async def send_gate1(
        self,
        run_id: str,
        product_id: str,
        signal_title: str,
        relevance_score: int,
        suggested_mode: str,
        reasoning: str,
        what_changed: str = "",
        relevance_explanation: str = "",
        pillar_references: list[str] | None = None,
    ) -> None:
        pillars = ", ".join(pillar_references) if pillar_references else "—"
        insight = ""
        if what_changed:
            insight += f"*What changed:* {_short(what_changed, 280)}\n"
        if relevance_explanation:
            insight += f"*Why it matters:* {_short(relevance_explanation, 280)}\n"
        insight += f"*Pillars:* {pillars}"
        payload = {
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": "📡 Gate 1 — New Signal"},
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Product:*\n`{product_id}`"},
                        {"type": "mrkdwn", "text": f"*Relevance:*\n{relevance_score}/5 · depth `{suggested_mode}`"},
                    ],
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*{signal_title}*\n{insight}\n_{reasoning}_",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "Depth ladder: archive < note < structure < evaluate < decide\n"
                            f"```POST /runs/{run_id}/direction\n"
                            f'{{ "depth": "{suggested_mode}" }}```'
                        ),
                    },
                },
            ]
        }
        await self._send(payload, run_id)

    async def send_gate2(
        self,
        run_id: str,
        product_id: str,
        signal_title: str,
        explorer_score: int,
        strategist_score: int,
        builder_score: int,
        skeptic_score: int,
        key_concern: str,
        review_url: str = "",
    ) -> None:
        scores_text = (
            f"Explorer: {explorer_score}  ·  Strategist: {strategist_score}  ·  "
            f"Builder: {builder_score}  ·  Skeptic: {skeptic_score}"
        )
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🧠 Gate 2 — S4 Evaluation Ready"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Product:*\n`{product_id}`"},
                    {"type": "mrkdwn", "text": f"*Run:*\n`{run_id}`"},
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{signal_title}*\n{scores_text}\n_{key_concern}_",
                },
            },
        ]
        if review_url:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🔗 <{review_url}|Open Review Page>",
                },
            })
        await self._send({"blocks": blocks}, run_id)

    async def send_gate3(
        self,
        run_id: str,
        product_id: str,
        signal_title: str,
        routing: str,
        composite_score: float,
        blocking_count: int,
        assumptions: list[dict] | None = None,
        persona_lines: list[str] | None = None,
        rubric_total: str | None = None,
        closing_window: bool = False,
    ) -> None:
        icon = {"kill": "⚠️", "poc": "🔬", "prd": "📋"}.get(routing, "🔀")
        routing_label = routing.upper()
        rubric_part = f"  ·  S4 rubric: {rubric_total}" if rubric_total else ""
        closing_part = (
            f"\n⏳ *Closing window* — value is transient and Impact is high; "
            f"consider a fast time-boxed bet over the default {routing_label}."
            if closing_window else ""
        )
        overrides = [r for r in ("kill", "poc", "prd") if r != routing]
        override_cmds = "\n".join(
            f'{{ "action": "override", "routing": "{r}" }}  → {r.upper()}'
            for r in overrides
        )
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{icon} Gate 3 — Routing Review ({routing_label})"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Product:*\n`{product_id}`"},
                    {"type": "mrkdwn", "text": f"*Run:*\n`{run_id}`"},
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{signal_title}*\n"
                        f"Composite: {composite_score:.1f}{rubric_part}{closing_part}\n"
                        f"S5 recommends *{routing_label}*. Confirm or override:"
                    ),
                },
            },
        ]
        if persona_lines:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "\n".join(f"• {_short(line)}" for line in persona_lines),
                },
            })
        if assumptions:
            rows = "\n".join(
                f"{'❗' if a.get('severity') == 'Blocking' else '·'} "
                f"[{a.get('severity', '?')}] {_short(a.get('statement', ''))}"
                for a in assumptions
            )
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Assumptions ({blocking_count} blocking):*\n{rows}",
                },
            })
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"```POST /runs/{run_id}/routing-review\n"
                    f'{{ "action": "confirm" }}  → {routing_label}\n'
                    f"{override_cmds}```"
                ),
            },
        })
        await self._send({"blocks": blocks}, run_id)

    async def _send(self, payload: dict, run_id: str) -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(self._webhook, json=payload, timeout=10.0)
            resp.raise_for_status()
        emit_event("notifier", "slack_sent", run_id)


# ---------------------------------------------------------------------------
# Fanout — the only type callers interact with
# ---------------------------------------------------------------------------


class FanoutNotifier:
    """Sends to all configured providers. One provider failing never affects others."""

    def __init__(self, providers: list) -> None:
        self._providers = providers

    @property
    def is_configured(self) -> bool:
        return len(self._providers) > 0

    async def send_gate1(
        self,
        run_id: str,
        product_id: str,
        signal_title: str,
        relevance_score: int,
        suggested_mode: str,
        reasoning: str,
        what_changed: str = "",
        relevance_explanation: str = "",
        pillar_references: list[str] | None = None,
    ) -> None:
        for provider in self._providers:
            try:
                await provider.send_gate1(
                    run_id=run_id,
                    product_id=product_id,
                    signal_title=signal_title,
                    relevance_score=relevance_score,
                    suggested_mode=suggested_mode,
                    reasoning=reasoning,
                    what_changed=what_changed,
                    relevance_explanation=relevance_explanation,
                    pillar_references=pillar_references,
                )
            except Exception as exc:  # noqa: BLE001
                emit_event("notifier", "send_failed", run_id, {
                    "provider": type(provider).__name__,
                    "error": str(exc),
                })

    async def send_gate2(
        self,
        run_id: str,
        product_id: str,
        signal_title: str,
        explorer_score: int,
        strategist_score: int,
        builder_score: int,
        skeptic_score: int,
        key_concern: str,
        review_url: str = "",
    ) -> None:
        for provider in self._providers:
            try:
                await provider.send_gate2(
                    run_id=run_id,
                    product_id=product_id,
                    signal_title=signal_title,
                    explorer_score=explorer_score,
                    strategist_score=strategist_score,
                    builder_score=builder_score,
                    skeptic_score=skeptic_score,
                    key_concern=key_concern,
                    review_url=review_url,
                )
            except Exception as exc:  # noqa: BLE001
                emit_event("notifier", "send_failed", run_id, {
                    "provider": type(provider).__name__,
                    "error": str(exc),
                })

    async def send_gate3(
        self,
        run_id: str,
        product_id: str,
        signal_title: str,
        routing: str,
        composite_score: float,
        blocking_count: int,
        assumptions: list[dict] | None = None,
        persona_lines: list[str] | None = None,
        rubric_total: str | None = None,
        closing_window: bool = False,
    ) -> None:
        for provider in self._providers:
            try:
                await provider.send_gate3(
                    run_id=run_id,
                    product_id=product_id,
                    signal_title=signal_title,
                    routing=routing,
                    composite_score=composite_score,
                    blocking_count=blocking_count,
                    assumptions=assumptions,
                    persona_lines=persona_lines,
                    rubric_total=rubric_total,
                    closing_window=closing_window,
                )
            except Exception as exc:  # noqa: BLE001
                emit_event("notifier", "send_failed", run_id, {
                    "provider": type(provider).__name__,
                    "error": str(exc),
                })


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_notifier() -> FanoutNotifier:
    """Build a FanoutNotifier from environment config.

    Providers with missing credentials are silently excluded.
    Returns a FanoutNotifier with zero providers if nothing is configured
    (all send_* calls become no-ops).

    Single chokepoint for the gate-notification cutover (US-48): when
    ``GATE_NOTIFICATIONS_ENABLED`` is false, no providers are wired, so every
    send_gate1/2/3 call becomes a no-op without touching the three call sites.
    Set false on the iMac once Hermes-ops owns gate (and terminal) messaging;
    keep true locally/in tests. Reversible — flip the flag and restart.
    """
    from config import settings

    if not settings.GATE_NOTIFICATIONS_ENABLED:
        emit_event("notifier", "gate_notifications_disabled", "-")
        return FanoutNotifier([])

    providers: list = []

    if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID:
        providers.append(
            TelegramNotifier(
                bot_token=settings.TELEGRAM_BOT_TOKEN,
                chat_id=settings.TELEGRAM_CHAT_ID,
            )
        )

    if settings.SLACK_WEBHOOK_URL:
        providers.append(SlackNotifier(webhook_url=settings.SLACK_WEBHOOK_URL))

    return FanoutNotifier(providers)
