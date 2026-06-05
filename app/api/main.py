from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app.api.approvals import router as approvals_router
from app.api.artifacts import router as artifacts_router
from app.api.deps import require_auth
from app.api.direction import router as direction_router
from app.api.review import router as review_router
from app.api.routing_review import router as routing_review_router
from app.api.runs import router as runs_router
from app.api.signals import router as signals_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.factory import build_engine

    app.state.engine = build_engine("local")
    yield


app = FastAPI(
    title="PM Agentic Platform",
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
app.include_router(review_router, dependencies=_auth)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
