from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_engine
from app.factory import PMEngine

router = APIRouter(prefix="/runs", tags=["artifacts"])

# Canonical artifact types (mirror app.models.workflow.ArtifactType).
_ARTIFACT_TYPES = {
    "poc_plan", "prd", "executive_summary", "insight_memo",
    "opportunity_memo", "evaluation_brief", "decision_memo", "checkpoint",
}
# Depth-named guesses a client may send. The artifact types kept their pre-US-43
# names when the depth ladder was renamed (opportunity→structure), so a caller
# reasoning depth→artifact queries `structure_memo` and used to get a 500.
_ARTIFACT_TYPE_ALIASES = {
    "structure_memo": "opportunity_memo",
    "opportunity": "opportunity_memo",
    "insight": "insight_memo",
    "evaluation": "evaluation_brief",
    "decision": "decision_memo",
}


def _normalize_artifact_type(artifact_type: Optional[str]) -> Optional[str]:
    """Resolve an artifact_type query param to a canonical value, or 422.

    A previously-unhandled value reached ``ArtifactType(<bad>)`` → ValueError →
    500 deep in the store. Validate at the edge instead: map known aliases,
    accept canonical values, and reject the rest with a clear 422.
    """
    if artifact_type is None:
        return None
    resolved = _ARTIFACT_TYPE_ALIASES.get(artifact_type, artifact_type)
    if resolved not in _ARTIFACT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown artifact_type '{artifact_type}'. "
                   f"Must be one of: {', '.join(sorted(_ARTIFACT_TYPES))}",
        )
    return resolved


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
    resolved_type = _normalize_artifact_type(artifact_type)

    run = engine.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    artifacts = engine.store.list_artifacts(
        run_id=run_id,
        artifact_type=resolved_type,
        limit=limit,
    )
    return [ArtifactResponse(**a) for a in artifacts]
