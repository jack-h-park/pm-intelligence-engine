"""Build immutable decision cases from already-prepared evidence."""

from datetime import datetime
from typing import Literal

from app.models.decision_case import DecisionCase, InsightRevisionReference
from app.models.insights import PreparedContext


def build_decision_case(
    *,
    prepared_context: PreparedContext,
    product_id: str,
    decision_question: str,
    authorized_depth: Literal["archive", "note", "structure", "evaluate", "decide"] | None,
    input_origin: Literal["direct", "insight"],
    insight_references: list[InsightRevisionReference] | None = None,
    deadline: datetime | None = None,
    options: list[str] | None = None,
) -> DecisionCase:
    """Copy only prepared, attributed evidence into a versioned case.

    This intentionally does not infer facts from an insight's prose.  The
    PreparedContext is the evidence boundary: its facts have passage IDs, while
    hypotheses and constraints keep their uncertainty labels when a run starts.
    """
    return DecisionCase(
        prepared_context_id=prepared_context.prepared_context_id,
        prepared_context_revision=prepared_context.revision,
        product_id=product_id,
        decision_question=decision_question,
        input_origins=[input_origin],
        source_references=[prepared_context.candidate_id, prepared_context.bundle_id],
        insight_references=insight_references or [],
        confirmed_facts=list(prepared_context.facts),
        hypotheses=list(prepared_context.hypotheses),
        constraints=list(prepared_context.constraints),
        deadline=deadline,
        options=list(options or []),
        authorized_depth=authorized_depth,
    )
