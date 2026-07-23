"""Stage 1 — Signal Ingestion.

No LLM call. Normalizes and structures the raw signal into a typed S1Output.
"""

import re

from app.llm.protocol import LLMProvider
from app.logging import emit_event
from app.models.stages import RunContext, S1Input, S1Output, S1OutputData, StageMetadata
from app.stages.chrome import strip_chrome
from app.storage.protocol import PMWorkflowStore

# Length of the verbatim excerpt S1 keeps as the signal summary.
_SUMMARY_CHARS = 800

_VALID_CATEGORIES = {"competitor", "platform", "regulation", "technology", "other"}

# Ordered keyword sets for keyword-based category inference. First category with a
# match wins, so the order encodes priority.
_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("regulation", ("regulation", "stig", "mandate", "compliance", "nist", "disa")),
    ("platform", ("android", "ios", "platform", "os", "api", "kernel", "amapi")),
    ("competitor", ("competitor", "graykey", "cellebrite", "graphene", "zimperium")),
    ("technology", ("exploit", "cve", "zero-day", "vulnerability", "rkp", "attestation")),
)

# Match on word boundaries, not substrings. A substring match let the regulation
# keyword "disa" (the DISA agency) fire inside "disables", mis-tagging an Android
# malware signal as "regulation" once site-chrome boilerplate leaked into
# raw_content (run d014f313: "Salesforce disables Klue …").
_CATEGORY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (category, re.compile(r"\b(?:" + "|".join(re.escape(w) for w in keywords) + r")\b"))
    for category, keywords in _CATEGORY_KEYWORDS
)


async def run(
    input: S1Input,
    context: RunContext,
    llm: LLMProvider,
    store: PMWorkflowStore,
) -> S1Output:
    # Strip site chrome (nav banners, "Skip to main content", cookie/consent
    # furniture) before excerpting, so the summary starts at the article rather
    # than the page's navigation band. strip_chrome returns the original when it
    # would leave too little to be the article, so genuinely short signals are
    # unaffected.
    body = strip_chrome(input.raw_content)
    summary = (
        body[:_SUMMARY_CHARS].rsplit(" ", 1)[0] + " …"
        if len(body) > _SUMMARY_CHARS
        else body
    )

    # Infer category from title/content keywords when not provided. Match against
    # the stripped body so chrome text can't fire a keyword (site chrome once
    # mis-tagged an Android signal "regulation" via "disa" in "disables").
    category = _infer_category(input.title + " " + body)

    output_data = S1OutputData(
        signal_id=input.signal_id,
        title=input.title,
        summary=summary,
        category=category,
        source=input.source_url or "manual",
        event_date=None,
        quality_passed=bool(input.title and input.raw_content),
    )

    output = S1Output(
        run_id=context.run_id,
        output=output_data,
        metadata=StageMetadata(model_used=None),
    )

    store.save_stage_output(
        run_id=context.run_id,
        stage="s1",
        output_json=output.model_dump_json(),
    )

    emit_event("s1", "completed", context.run_id, {"signal_id": input.signal_id})
    return output


def _infer_category(text: str) -> str:
    lower = text.lower()
    for category, pattern in _CATEGORY_PATTERNS:
        if pattern.search(lower):
            return category
    return "other"
