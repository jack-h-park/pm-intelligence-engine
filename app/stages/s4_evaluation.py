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
from app.logging import emit_event
from app.llm.protocol import LLMProvider
from app.models.stages import (
    PersonaOutput,
    RunContext,
    S4Input,
    S4Output,
    S4OutputData,
    StageMetadata,
)
from app.storage.protocol import PMWorkflowStore
from eval.rubrics.s4_rubric import check as check_s4_rubric


async def run(
    stage_input: S4Input,
    context: RunContext,
    llm: LLMProvider,
    store: PMWorkflowStore,
) -> S4Output:
    """Run 4 persona agents in parallel and score the result with the S4 rubric."""
    agents = [ExplorerAgent(), StrategistAgent(), BuilderAgent(), SkepticAgent()]

    # Parallel execution — agents cannot see each other's outputs
    personas: list[PersonaOutput] = list(
        await asyncio.gather(
            *[
                agent.evaluate(stage_input.s3_output, context, llm, feedback=stage_input.feedback)
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
        metadata=StageMetadata(model_used=_resolve_model()),
    )

    store.save_stage_output(
        run_id=context.run_id,
        stage="s4",
        output_json=output.model_dump_json(),
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


def _resolve_model() -> str:
    from config import settings

    return settings.ANTHROPIC_MODEL if settings.LLM_PROVIDER.lower() == "claude" else settings.OPENAI_MODEL
