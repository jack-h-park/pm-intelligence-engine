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


def render_decision_case(case: DecisionCase | None) -> str:
    """Render the pinned evidence boundary for a stage prompt without relabeling it."""
    if case is None:
        return "No DecisionCase was selected for this historical run."
    facts = "\n".join(f"- {fact.text}" for fact in case.confirmed_facts) or "- None supplied"
    hypotheses = "\n".join(f"- {item}" for item in case.hypotheses) or "- None supplied"
    constraints = "\n".join(f"- {item}" for item in case.constraints) or "- None supplied"
    options = "\n".join(f"- {item}" for item in case.options) or "- None supplied"
    return f"""Decision question: {case.decision_question}

Confirmed facts (do not promote hypotheses into facts):
{facts}

Hypotheses:
{hypotheses}

Constraints:
{constraints}

Options (status quo/defer can remain the result):
{options}"""
