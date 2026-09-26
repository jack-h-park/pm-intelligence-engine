"""Explicit, additive schema initialization for the insight store."""

from datetime import UTC, datetime
from typing import cast

from sqlalchemy import Engine, Table, inspect, select, text

from app.models.insights import IntelligenceSchemaVersionRow
from app.models.workflow import Base

INSIGHT_SCHEMA_VERSION = 10


def initialize_insight_schema(engine: Engine) -> None:
    """Create only additive intelligence tables and record the applied version.

    This is invoked during application startup (or explicitly by tests), never
    by a request handler.  ``create_all`` preserves every existing legacy table.
    """
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        job_columns = {
            column["name"] for column in inspect(connection).get_columns("intelligence_jobs")
        }
        if "scoped_candidate_id" not in job_columns:
            connection.execute(
                text("ALTER TABLE intelligence_jobs ADD COLUMN scoped_candidate_id VARCHAR")
            )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_intelligence_scoped_candidate_job "
                "ON intelligence_jobs(scoped_candidate_id)"
            )
        )

        version = connection.execute(
            select(IntelligenceSchemaVersionRow.version).where(
                IntelligenceSchemaVersionRow.version == INSIGHT_SCHEMA_VERSION
            )
        ).scalar_one_or_none()
        if version is None:
            connection.execute(
                cast(Table, IntelligenceSchemaVersionRow.__table__).insert().values(
                    version=INSIGHT_SCHEMA_VERSION,
                    applied_at=datetime.now(UTC),
                )
            )
