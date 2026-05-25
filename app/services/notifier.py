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
    ) -> None:
        text = (
            f"📡 <b>[Gate 1] New Signal</b>\n\n"
            f"Product: <code>{product_id}</code>\n"
            f"Title: {signal_title}\n"
            f"Relevance: <b>{relevance_score}/5</b> · Suggested: <b>{suggested_mode}</b>\n"
            f"<i>\"{reasoning}\"</i>\n\n"
            f"To proceed:\n"
            f"<code>POST /runs/{run_id}/direction</code>\n"
            f'<code>{{"mode": "{suggested_mode}"}}</code>'
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
        composite_score: float,
        blocking_count: int,
    ) -> None:
        text = (
            f"⚠️ <b>[Gate 3] Kill Routing Review</b>\n\n"
            f"Product: <code>{product_id}</code>\n"
            f"Signal: {signal_title}\n"
            f"Run: <code>{run_id}</code>\n\n"
            f"Composite: <b>{composite_score:.1f}</b> · "
            f"Blocking assumptions: <b>{blocking_count}</b>\n\n"
            f"S5 recommends <b>KILL</b>. Confirm or override:\n"
            f"<code>POST /runs/{run_id}/routing-review</code>\n"
            f'<code>{{"action": "confirm"}}</code>  → kill\n'
            f'<code>{{"action": "override", "routing": "poc"}}</code>  → POC\n'
            f'<code>{{"action": "override", "routing": "prd"}}</code>  → PRD'
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
    ) -> None:
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
                        {"type": "mrkdwn", "text": f"*Relevance:*\n{relevance_score}/5 · `{suggested_mode}`"},
                    ],
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*{signal_title}*\n_{reasoning}_",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"```POST /runs/{run_id}/direction\n"
                            f'{{ "mode": "{suggested_mode}" }}```'
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
        composite_score: float,
        blocking_count: int,
    ) -> None:
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "⚠️ Gate 3 — Kill Routing Review"},
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
                        f"Composite: {composite_score:.1f}  ·  "
                        f"Blocking assumptions: {blocking_count}\n\n"
                        f"S5 recommends *KILL*. Confirm or override:"
                    ),
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"```POST /runs/{run_id}/routing-review\n"
                        f'{{ "action": "confirm" }}         → kill\n'
                        f'{{ "action": "override", "routing": "poc" }}  → POC\n'
                        f'{{ "action": "override", "routing": "prd" }}  → PRD```'
                    ),
                },
            },
        ]
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
        composite_score: float,
        blocking_count: int,
    ) -> None:
        for provider in self._providers:
            try:
                await provider.send_gate3(
                    run_id=run_id,
                    product_id=product_id,
                    signal_title=signal_title,
                    composite_score=composite_score,
                    blocking_count=blocking_count,
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
    """
    from config import settings

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
