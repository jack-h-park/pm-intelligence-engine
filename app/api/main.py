from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.approvals import router as approvals_router
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

app.include_router(signals_router)
app.include_router(runs_router)
app.include_router(direction_router)
app.include_router(approvals_router)
app.include_router(routing_review_router)
app.include_router(review_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
