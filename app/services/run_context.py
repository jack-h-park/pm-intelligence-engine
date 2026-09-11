"""Build stage context while preserving an optional pinned DecisionCase."""

from app.models.stages import RunContext


def load_run_context(run_id: str, engine) -> RunContext:
    run = engine.store.get_run(run_id)
    if run is None:
        raise ValueError(f"Run {run_id} not found")
    full_context = engine.context_loader.load_full_context(run["product_id"])
    return RunContext(
        run_id=run_id,
        product_id=run["product_id"],
        pm_identity=full_context.pm_identity,
        company_context=full_context.company_context,
        product_context=full_context.product_context,
        decision_case=engine.store.get_decision_case(run_id),
    )
