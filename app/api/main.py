from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI

from app.api.approvals import router as approvals_router
from app.api.artifacts import router as artifacts_router
from app.api.decision import router as decision_router
from app.api.deepen import router as deepen_router
from app.api.deps import require_auth
from app.api.direction import router as direction_router
from app.api.insight_budget import router as insight_budget_router
from app.api.insights import router as insights_router
from app.api.review import router as review_router
from app.api.routing_review import router as routing_review_router
from app.api.runs import router as runs_router
from app.api.signals import router as signals_router
from app.api.void import router as void_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.agents.builder import BuilderAgent
    from app.agents.explorer import ExplorerAgent
    from app.agents.skeptic import SkepticAgent
    from app.agents.strategist import StrategistAgent
    from app.factory import build_engine
    from app.services.template_service import TemplateService
    from config import settings

    # Preflight: persona prompts live in decision-context (US-37). Fail fast at
    # boot if that checkout is stale rather than crashing mid-run at S4.
    personas = [a.persona for a in (ExplorerAgent, StrategistAgent, BuilderAgent, SkepticAgent)]
    TemplateService(settings.DECISION_SYSTEM_ROOT).validate_persona_prompts(personas)

    engine = build_engine("local")
    # Insight schema initialization is an explicit startup operation.  Request
    # handlers only use the already-initialized store and never issue DDL.
    if engine.insight_store is None:  # pragma: no cover - factory invariant
        raise RuntimeError("Insight storage was not initialized")
    engine.insight_store.initialize_schema()
    if settings.INSIGHT_PROJECTION_ENABLED:
        from app.services.insight_projection import reconcile_store_projections

        reconcile_store_projections(
            engine.insight_store, Path(settings.WIKI_ROOT) / "outputs" / "signal-intelligence"
        )
    app.state.engine = engine
    yield


app = FastAPI(
    title="Jack H. Park's PM Intelligence Engine",
    description="Automates the PM signal-to-decision workflow",
    version="0.1.0",
    lifespan=lifespan,
)

# Every router requires a valid bearer token (see app.api.deps.require_auth).
# GET /health is declared directly on the app below, outside any router, so it
# stays unauthenticated for liveness probes.
_auth = [Depends(require_auth)]

app.include_router(signals_router, dependencies=_auth)
app.include_router(runs_router, dependencies=_auth)
app.include_router(artifacts_router, dependencies=_auth)
app.include_router(direction_router, dependencies=_auth)
app.include_router(approvals_router, dependencies=_auth)
app.include_router(routing_review_router, dependencies=_auth)
app.include_router(void_router, dependencies=_auth)
app.include_router(deepen_router, dependencies=_auth)
app.include_router(decision_router, dependencies=_auth)
app.include_router(review_router, dependencies=_auth)
app.include_router(insights_router, dependencies=_auth)
app.include_router(insight_budget_router, dependencies=_auth)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
