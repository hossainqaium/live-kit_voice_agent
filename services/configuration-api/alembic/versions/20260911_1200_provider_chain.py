"""agent version provider fallback and local tiers

Revision ID: c8f1a2d43b90
Revises: b46727df64e0
Create Date: 2026-09-11 12:00:00.000000+00:00

Spec 55 requires a fallback provider for every AI provider, and spec 25 requires
local/self-hosted models to be usable. Both are per-agent decisions, so they
belong on the version snapshot rather than on the tenant.

Every column is nullable: a version that names only a primary provider keeps
working exactly as it did, which is what lets this run against a database with
published versions already in it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c8f1a2d43b90"
down_revision: str | None = "b46727df64e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: (column, referenced table). RESTRICT matches the primary-tier columns: a
#: catalog row an agent depends on must not vanish underneath a published
#: version.
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("stt_fallback_provider_id", "providers"),
    ("stt_fallback_model_id", "models"),
    ("llm_fallback_provider_id", "providers"),
    ("llm_fallback_model_id", "models"),
    ("tts_fallback_provider_id", "providers"),
    ("tts_fallback_model_id", "models"),
    ("tts_fallback_voice_id", "voices"),
    ("stt_local_provider_id", "providers"),
    ("stt_local_model_id", "models"),
    ("tts_local_provider_id", "providers"),
    ("tts_local_model_id", "models"),
    ("tts_local_voice_id", "voices"),
)


def upgrade() -> None:
    for column, table in _COLUMNS:
        op.add_column(
            "agent_versions",
            sa.Column(column, postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            f"fk_agent_versions_{column}",
            "agent_versions",
            table,
            [column],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    for column, _table in reversed(_COLUMNS):
        op.drop_constraint(f"fk_agent_versions_{column}", "agent_versions", type_="foreignkey")
        op.drop_column("agent_versions", column)
