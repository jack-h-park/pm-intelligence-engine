"""Immutable, provenance-preserving context for a product decision run."""

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.insights import PreparedFact


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _utc_now() -> datetime:
    return datetime.now(UTC)


class InsightRevisionReference(BaseModel):
    """An exact, immutable insight revision selected for this case."""

    model_config = ConfigDict(extra="forbid")

    insight_id: str = Field(min_length=1)
    revision: int = Field(ge=1)


class DecisionCase(BaseModel):
    """Pinned input for D2-D6; facts remain distinct from hypotheses."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(default_factory=_new_uuid)
    revision: int = Field(default=1, ge=1)
    prepared_context_id: str = Field(min_length=1)
    prepared_context_revision: int = Field(ge=1)
    product_id: str = Field(min_length=1)
    decision_question: str = Field(min_length=1)
    input_origins: list[Literal["direct", "insight"]] = Field(min_length=1)
    source_references: list[str] = Field(default_factory=list)
    insight_references: list[InsightRevisionReference] = Field(default_factory=list)
    confirmed_facts: list[PreparedFact] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    deadline: datetime | None = None
    options: list[str] = Field(default_factory=list)
    authorized_depth: Literal["archive", "note", "structure", "evaluate", "decide"]
    created_at: datetime = Field(default_factory=_utc_now)

    @property
    def input_origin(self) -> Literal["direct", "insight"]:
        """Compatibility convenience for callers that construct one-origin cases."""
        return self.input_origins[0]

    @model_validator(mode="after")
    def _selected_insights_require_their_origin(self) -> "DecisionCase":
        if self.insight_references and "insight" not in self.input_origins:
            raise ValueError("insight references require an insight input origin")
        if "insight" in self.input_origins and not self.insight_references:
            raise ValueError("insight input origin requires an immutable insight revision")
        return self
