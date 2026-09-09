"""Stage 7 — Executive Summary.

Loads all prior stage outputs from the store and synthesizes a narrative
that is readable by a stakeholder who was not part of the run.
Output is saved as both a StageOutput and an Artifact (Markdown).
"""

import json

from app.logging import emit_event
from app.llm.json_call import complete_json
from app.llm.protocol import LLMProvider
from app.models.stages import (
    RunContext,
    S1OutputData,
    S2OutputData,
    S3OutputData,
    S4OutputData,
    S7Input,
    S7Output,
    S7OutputData,
    StageMetadata,
)
from app.services.template_service import TemplateService
from app.storage.protocol import PMWorkflowStore

_JSON_SCHEMA_FULL = """{
  "what_we_saw": "<2–3 sentences: factual signal summary — why it was worth pursuing>",
  "what_it_means": "<2–3 sentences: insight and product-specific implications, reference a strategy pillar>",
  "what_we_decided": "<1–2 sentences: opportunity in one sentence + composite score + track + routing rationale>",
  "what_we_will_do_next": "<2–3 sentences: PoC plan or PRD next steps in plain language>",
  "markdown": "<full formatted executive summary as Markdown — use the Run Summary table from the template>"
}"""

_JSON_SCHEMA_PARTIAL = """{
  "what_we_saw": "<2–3 sentences: factual signal summary — why it was worth noting>",
  "what_it_means": "<2–3 sentences: insight and product-specific implications, reference a strategy pillar>",
  "what_we_decided": "<1 sentence: what depth was chosen and why, e.g. 'Filed as brief insight — no actionable opportunity identified at this time'>",
  "what_we_will_do_next": "<1 sentence: follow-up action or 'No further action required'>",
  "markdown": "<formatted executive summary as Markdown>"
}"""


async def run(
    stage_input: S7Input,
    context: RunContext,
    llm: LLMProvider,
    store: PMWorkflowStore,
) -> S7Output:
    """Synthesize all prior stage outputs into an executive summary."""
    from config import settings

    template_service = TemplateService(settings.DECISION_SYSTEM_ROOT)
    template = template_service.load_template("s7")

    # Load S1–S4 outputs from store (S5/S6 come from stage_input)
    s1_data, s2_data, s3_data, s4_data = _load_prior_stages(store, context.run_id)

    s5 = stage_input.s5_output
    mode = stage_input.mode
    is_full = mode == "decide" and s5 is not None
    json_schema = _JSON_SCHEMA_FULL if is_full else _JSON_SCHEMA_PARTIAL

    persona_scores = (
        ", ".join(f"{p.persona.capitalize()} {p.score}/5" for p in s4_data.personas)
        if s4_data
        else "N/A"
    )

    s5_section = (
        f"Composite: {s5.composite_score}/5.00 | Routing: {s5.routing.upper()}\n"
        f"Rationale: {s5.rationale}\n"
        f"Blocking assumptions: {s5.blocking_count}"
        if s5
        else f"Not run — pipeline stopped at mode '{mode}'"
    )
    s6_summary = _summarize_s6(stage_input)

    system_message = (
        "You are a Product Manager. Follow the PM identity and operating philosophy below.\n\n"
        f"{context.pm_identity}"
    )

    user_message = f"""## Stage 7 Framework
{template}

---

## Product Context
{context.product_context}

---

## Run Mode
{mode} — {_mode_description(mode)}

## Run Data Summary

### Signal Ingestion (Stage 1)
Title: {s1_data.title if s1_data else "N/A"}
Category: {s1_data.category if s1_data else "N/A"}
Summary: {s1_data.summary if s1_data else "N/A"}

### Insight Extraction (Stage 2)
What changed: {s2_data.what_changed if s2_data else "N/A"}
Reframing: {s2_data.reframing if s2_data else "N/A"}
Pillars referenced: {", ".join(s2_data.pillar_references) if s2_data else "N/A"}

### Opportunity Creation (Stage 3)
Problem: {s3_data.problem_statement if s3_data else "N/A — not run in this mode"}
Hypothesis: {s3_data.hypothesis if s3_data else "N/A"}
Target user: {s3_data.target_user if s3_data else "N/A"}

### Persona Evaluation (Stage 4)
{persona_scores}

### Prioritization and Routing (Stage 5)
{s5_section}

### Next Step (Stage 6)
{s6_summary}

---

## Your Task
Write an executive summary appropriate for this run's depth ({mode} mode).

Respond with a single JSON object matching this schema exactly — no markdown, no commentary:

{json_schema}

Rules:
- No unexplained acronyms or internal jargon.
- Only reference sections that were actually run. Mark unrun sections as "Not run in this mode."
- "markdown" must include a Run Summary table filled with actual data from the stages that ran.
- A stakeholder who reads only the markdown should understand what was done and why."""

    usage_sink: list = []
    data = await complete_json(
        llm,
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ],
        stage="s7",
        run_id=context.run_id,
        usage_sink=usage_sink,
        max_tokens=2048,
    )
    output_data = S7OutputData(**data)
    if context.decision_pipeline_version == "evidence_v1" and context.decision_case is not None:
        output_data.markdown = output_data.markdown.rstrip() + _traceability_footer(
            context.decision_case.case_id,
            context.decision_case.revision,
            s5.readiness.provisional if s5 and s5.readiness else True,
        )

    output = S7Output(
        run_id=context.run_id,
        output=output_data,
        metadata=StageMetadata.with_usage(_resolve_model(), usage_sink),
    )

    # S7 can legitimately run twice for one run (a note-depth summary, then a
    # deepen to decide re-summarizes over the full pipeline) — bump the version
    # so the latest wins on read instead of colliding at version 1.
    existing_s7 = store.get_stage_output(context.run_id, "s7")
    store.save_stage_output(
        run_id=context.run_id,
        stage="s7",
        output_json=output.model_dump_json(),
        version=(existing_s7["version"] + 1) if existing_s7 else 1,
    )

    # Save Markdown as Artifact for easy export
    store.save_artifact(
        run_id=context.run_id,
        artifact_type="executive_summary",
        content_md=output_data.markdown,
        source_stage="s7",
    )

    emit_event("s7", "completed", context.run_id)
    return output


def _load_prior_stages(
    store: PMWorkflowStore, run_id: str
) -> tuple[S1OutputData | None, S2OutputData | None, S3OutputData | None, S4OutputData | None]:
    def _load(stage: str, model_class):  # type: ignore[no-untyped-def]
        raw = store.get_stage_output(run_id, stage)
        if raw is None:
            return None
        return model_class(**json.loads(raw["output_json"])["output"])

    return (
        _load("s1", S1OutputData),
        _load("s2", S2OutputData),
        _load("s3", S3OutputData),
        _load("s4", S4OutputData),
    )


def _summarize_s6(stage_input: S7Input) -> str:
    if stage_input.s6b_output:
        prd = stage_input.s6b_output
        stories = len(prd.user_stories)
        metrics = len(prd.success_metrics)
        return f"PRD track — {stories} user stories, {metrics} success metrics defined."
    if stage_input.s6a_output:
        poc = stage_input.s6a_output
        return f"PoC track — {poc.timeline_weeks}-week experiment: {poc.experiment_goal}"
    return f"Not run — pipeline stopped at mode '{stage_input.mode}'"


def _traceability_footer(case_id: str, revision: int, provisional: bool) -> str:
    return (
        "\n\n## Decision Traceability\n"
        f"- Case: {case_id} revision {revision}\n"
        f"- Status: {'Provisional' if provisional else 'Grounded'}\n"
        "- Metrics and resource estimates are proposed unless explicitly measured.\n"
    )


def _mode_description(mode: str) -> str:
    descriptions = {
        "archive": "signal normalized and set aside only",
        "note": "signal + insight extraction + note",
        "structure": "signal + insight + opportunity structuring",
        "evaluate": "signal + insight + opportunity + full persona evaluation",
        "decide": "full pipeline — evaluation, routing, and next step",
    }
    return descriptions.get(mode, mode)




def _resolve_model() -> str:
    from config import settings

    if settings.LLM_PROVIDER.lower() == "claude":
        return settings.ANTHROPIC_MODEL
    return settings.OPENAI_MODEL
