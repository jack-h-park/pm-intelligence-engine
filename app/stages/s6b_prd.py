"""Stage 6B — PRD Generation.

Produces a full PRD from S5 output. Only runs when S5 routing is 'prd'.
Completeness check is computed deterministically from the LLM output.
"""

from app.llm.json_call import complete_json
from app.llm.protocol import LLMProvider
from app.logging import emit_event
from app.models.stages import (
    PRDCompletenessCheck,
    RunContext,
    S6BInput,
    S6BOutput,
    S6BOutputData,
    StageMetadata,
)
from app.services.decision_case import render_decision_case
from app.services.template_service import TemplateService
from app.storage.protocol import PMWorkflowStore

_JSON_SCHEMA = """{
  "problem_statement": "<specific problem for the target user>",
  "target_user": "<role + context — specific enough for an engineer to build for>",
  "success_metrics": [
    "<metric with baseline and target>",
    "<metric with measurement method>"
  ],
  "user_stories": [
    "As a [specific user], I want to [action] so that [outcome].",
    "..."
  ],
  "in_scope": ["<explicit capability 1>", "<explicit capability 2>"],
  "out_of_scope": ["<explicit exclusion 1>", "<explicit exclusion 2>"],
  "technical_dependencies": ["<API / platform / partner requirement>"],
  "open_questions": ["<question — owner: [role]>"],
  "risks": ["<risk description>"]
}"""


async def run(
    stage_input: S6BInput,
    context: RunContext,
    llm: LLMProvider,
    store: PMWorkflowStore,
) -> S6BOutput:
    """Generate a complete PRD actionable by an engineering team."""
    from config import settings

    template_service = TemplateService(settings.DECISION_SYSTEM_ROOT)
    template = template_service.load_template("s6b")

    s5 = stage_input.s5_output
    adjusting = [a for a in s5.assumptions if a.severity == "Adjusting"]
    adjusting_text = "\n".join(f"- {a.statement}" for a in adjusting) if adjusting else "None identified."

    system_message = (
        "You are a Product Manager. Follow the PM identity and operating philosophy below.\n\n"
        f"{context.pm_identity}"
    )

    user_message = f"""## Stage 6B Framework
{template}

---

## Product Context
{context.product_context}

---

## Stage 5 Prioritization Output
Composite score: {s5.composite_score}/5.00
Impact: {s5.impact_score}/5 | Strategic Fit: {s5.strategic_fit_score}/5 | Feasibility: {s5.feasibility_score}/5 | Confidence: {s5.confidence_score}/5
Rationale: {s5.rationale}

Assumptions to manage (Adjusting — not Blocking):
{adjusting_text}

---

## Pinned Decision Case
{render_decision_case(context.decision_case)}

---

## Your Task
Produce a PRD that an engineer unfamiliar with this run can implement with at most 2 clarifying questions.

Respond with a single JSON object matching this schema exactly — no markdown, no commentary:

{_JSON_SCHEMA}

Rules:
- user_stories must have at least 3 entries; each must be independently testable.
- out_of_scope must have at least 2 explicit exclusions.
- success_metrics must have at least 2 entries, each with a measurement method.
- open_questions must name a suggested owner role in parentheses."""

    usage_sink: list = []
    data = await complete_json(
        llm,
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ],
        stage="s6b",
        run_id=context.run_id,
        usage_sink=usage_sink,
        max_tokens=2048,
    )
    completeness = _compute_completeness(data)
    output_data = S6BOutputData(**data, completeness=completeness)

    output = S6BOutput(
        run_id=context.run_id,
        output=output_data,
        metadata=StageMetadata.with_usage(_resolve_model(), usage_sink),
    )

    store.save_stage_output(
        run_id=context.run_id,
        stage="s6b",
        output_json=output.model_dump_json(),
    )

    store.save_artifact(
        run_id=context.run_id,
        artifact_type="prd",
        content_md=build_prd(output_data),
        source_stage="s6b",
    )

    emit_event(
        "s6b",
        "completed",
        context.run_id,
        {"completeness_score": f"{completeness.score}/12"},
    )
    return output


def build_prd(data: S6BOutputData) -> str:
    def _fmt_list(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items) if items else "—"

    return f"""# PRD

## Problem Statement
{data.problem_statement}

## Target User
{data.target_user}

## Success Metrics
{_fmt_list(data.success_metrics)}

## User Stories
{_fmt_list(data.user_stories)}

## In Scope
{_fmt_list(data.in_scope)}

## Out of Scope
{_fmt_list(data.out_of_scope)}

## Technical Dependencies
{_fmt_list(data.technical_dependencies)}

## Open Questions
{_fmt_list(data.open_questions)}

## Risks
{_fmt_list(data.risks)}

---
**Completeness:** {data.completeness.score}/12
"""


def _compute_completeness(data: dict) -> PRDCompletenessCheck:
    return PRDCompletenessCheck(
        problem_statement=bool(data.get("problem_statement", "").strip()),
        target_user=bool(data.get("target_user", "").strip()),
        hypothesis=True,
        success_metrics=len(data.get("success_metrics", [])) >= 2,
        user_stories=len(data.get("user_stories", [])) >= 3,
        in_scope=len(data.get("in_scope", [])) > 0,
        out_of_scope=len(data.get("out_of_scope", [])) >= 2,
        technical_dependencies=len(data.get("technical_dependencies", [])) > 0,
        open_questions=len(data.get("open_questions", [])) > 0,
        non_goals=len(data.get("out_of_scope", [])) >= 2,
        rollout_phases=False,
        risks=len(data.get("risks", [])) > 0,
    )


def _resolve_model() -> str:
    from config import settings

    if settings.LLM_PROVIDER.lower() == "claude":
        return settings.ANTHROPIC_MODEL
    return settings.OPENAI_MODEL
