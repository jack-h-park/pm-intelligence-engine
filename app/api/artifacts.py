from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_engine
from app.factory import PMEngine

router = APIRouter(prefix="/runs", tags=["artifacts"])


class ArtifactResponse(BaseModel):
    artifact_id: str
    run_id: str
    type: str
    content_md: str
    content_json: str
    created_at: str


@router.get("/{run_id}/artifacts", response_model=list[ArtifactResponse])
async def list_artifacts(
    run_id: str,
    artifact_type: Optional[str] = None,
    limit: int = 20,
    engine: PMEngine = Depends(get_engine),
) -> list[ArtifactResponse]:
    run = engine.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    artifacts = engine.store.list_artifacts(
        run_id=run_id,
        artifact_type=artifact_type,
        limit=limit,
    )
    return [ArtifactResponse(**a) for a in artifacts]
