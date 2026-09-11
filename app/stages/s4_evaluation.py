"""Stage 4 — Persona Evaluation.

Runs 4 independent persona agents in parallel (asyncio.gather).
No cross-contamination: each agent sees only the opportunity and product context.
Stores 4 individual outputs + aggregate rubric score.
"""

import asyncio

from app.agents.builder import BuilderAgent
from app.agents.explorer import ExplorerAgent
from app.agents.skeptic import SkepticAgent
from app.agents.strategist import StrategistAgent
from app.llm.protocol import LLMProvider
from app.logging import emit_event
from app.models.stages import (
    PersonaOutput,
    RunContext,
    S4Input,
    S4Output,
    S4OutputData,
    S4RubricResult,
    StageMetadata,
)
from app.services.template_service import TemplateService
from app.storage.protocol import PMWorkflowStore
from eval.rubrics.s4_rubric import check as check_s4_rubric


async def run(
    stage_input: S4Input,
    context: RunContext,
    llm: LLMProvider,
    store: PMWorkflowStore,
) -> S4Output:
    """Run 4 persona agents in parallel and score the result with the S4 rubric."""
    from config import settings

    agents = [ExplorerAgent(), StrategistAgent(), BuilderAgent(), SkepticAgent()]

    # Persona lens + question are owned by decision-context (US-37); load once.
    template_service = TemplateService(settings.DECISION_SYSTEM_ROOT)
    prompts = {
        agent.persona: template_service.load_persona_prompt(agent.persona) for agent in agents
    }

    # Parallel execution — agents cannot see each other's outputs. All four share
    # one usage_sink; each appends its call's tokens (asyncio.gather on a single
    # event loop, so list.append between awaits is safe) → S4 stage total.
    usage_sink: list = []
    personas: list[PersonaOutput] = list(
        await asyncio.gather(
            *[
                agent.evaluate(
                    stage_input.s3_output,
                    context,
                    llm,
                    prompt=prompts[agent.persona],
                    feedback=stage_input.feedback,
                    usage_sink=usage_sink,
                )
                for agent in agents
            ]
        )
    )

    # Store each persona output independently for traceability
    for persona_out in personas:
        store.save_stage_output(
            run_id=context.run_id,
            stage=f"s4_{persona_out.persona}",
            output_json=persona_out.model_dump_json(),
            version=stage_input.version,
        )

    rubric = check_s4_rubric(personas, context.product_context)

    output_data = S4OutputData(personas=personas, rubric=rubric)
    output = S4Output(
        run_id=context.run_id,
        version=stage_input.version,
        output=output_data,
        metadata=StageMetadata.with_usage(_resolve_model(), usage_sink),
    )

    store.save_stage_output(
        run_id=context.run_id,
        stage="s4",
        output_json=output.model_dump_json(),
    )

    store.save_artifact(
        run_id=context.run_id,
        artifact_type="evaluation_brief",
        content_md=build_evaluation_brief(personas, rubric),
        source_stage="s4",
    )

    emit_event(
        "s4",
        "completed",
        context.run_id,
        {
            "rubric_score": f"{rubric.total_score}/12",
            "rubric_passed": rubric.passed,
            "scores": {p.persona: p.score for p in personas},
        },
    )
    return output


_PERSONA_LABELS = {
    "explorer": ("Impact", "🔭"),
    "strategist": ("Strategic Fit", "🧭"),
    "builder": ("Feasibility", "🔨"),
    "skeptic": ("Confidence", "🔍"),
}


def build_evaluation_brief(personas: list[PersonaOutput], rubric: S4RubricResult) -> str:
    rows = []
    for p in personas:
        label, icon = _PERSONA_LABELS.get(p.persona, (p.dimension, ""))
        rows.append(
            f"| {icon} {p.persona.capitalize()} | {label} | {p.score}/5 | {p.key_argument} |"
        )

    table = "\n".join(rows)

    open_qs = "\n".join(f"- **{p.persona.capitalize()}:** {p.open_question}" for p in personas)

    rubric_status = "✅ Passed" if rubric.passed else "❌ Failed"

    # The per-dimension breakdown and the issue list used to exist only in the
    # archive's separate rubric file, so "the brief" and "the archive" were not
    # two renderings of one document — one of them was strictly missing facts.
    # Folding them in makes this the single canonical S4 document.
    issues_md = ""
    if rubric.issues:
        issues_md = "\n\n## Rubric Issues\n" + "\n".join(f"- {i}" for i in rubric.issues)

    return f"""# Evaluation Brief

## Persona Scores
| Persona | Dimension | Score | Key Argument |
|---------|-----------|-------|--------------|
{table}

## Open Questions
{open_qs}

## Rubric
**{rubric_status}** ({rubric.total_score}/12)

| Dimension | Score |
|-----------|-------|
| Score Grounding | {rubric.score_grounding}/3 |
| Skeptic Quality | {rubric.skeptic_quality}/3 |
| Open Question Quality | {rubric.open_question_quality}/3 |
| Persona Independence | {rubric.persona_independence}/3 |{issues_md}
"""


def _resolve_model() -> str:
    from config import settings

    return (
        settings.ANTHROPIC_MODEL
        if settings.LLM_PROVIDER.lower() == "claude"
        else settings.OPENAI_MODEL
    )
