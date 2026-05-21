"""Stage 1 — Signal Ingestion.

No LLM call. Normalizes and structures the raw signal into a typed S1Output.
"""

from app.logging import emit_event
from app.llm.protocol import LLMProvider
from app.models.stages import RunContext, S1Input, S1Output, S1OutputData, StageMetadata
from app.storage.protocol import PMWorkflowStore

_VALID_CATEGORIES = {"competitor", "platform", "regulation", "technology", "other"}


async def run(
    input: S1Input,
    context: RunContext,
    llm: LLMProvider,
    store: PMWorkflowStore,
) -> S1Output:
    summary = (
        input.raw_content[:800].rsplit(" ", 1)[0] + " …"
        if len(input.raw_content) > 800
        else input.raw_content
    )

    # Infer category from title/content keywords when not provided
    category = _infer_category(input.title + " " + input.raw_content)

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
    if any(w in lower for w in ("regulation", "stig", "mandate", "compliance", "nist", "disa")):
        return "regulation"
    if any(w in lower for w in ("android", "ios", "platform", "os", "api", "kernel", "amapi")):
        return "platform"
    if any(w in lower for w in ("competitor", "graykey", "cellebrite", "graphene", "zimperium")):
        return "competitor"
    if any(w in lower for w in ("exploit", "cve", "zero-day", "vulnerability", "rkp", "attestation")):
        return "technology"
    return "other"
