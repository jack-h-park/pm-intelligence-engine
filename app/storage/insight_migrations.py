"""Explicit, additive schema initialization for the insight store."""

from datetime import UTC, datetime

from sqlalchemy import Engine, select

from app.models.insights import IntelligenceSchemaVersionRow
from app.models.workflow import Base

INSIGHT_SCHEMA_VERSION = 3


def initialize_insight_schema(engine: Engine) -> None:
    """Create only additive intelligence tables and record the applied version.

    This is invoked during application startup (or explicitly by tests), never
    by a request handler.  ``create_all`` preserves every existing legacy table.
    """
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        version = connection.execute(
            select(IntelligenceSchemaVersionRow.version).where(
                IntelligenceSchemaVersionRow.version == INSIGHT_SCHEMA_VERSION
            )
        ).scalar_one_or_none()
        if version is None:
            connection.execute(
                IntelligenceSchemaVersionRow.__table__.insert().values(
                    version=INSIGHT_SCHEMA_VERSION,
                    applied_at=datetime.now(UTC),
                )
            )
