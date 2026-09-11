"""add embedding_provider_id and embedding_model_id to agent_versions

Revision ID: b7d4f1a02c58
Revises: a3f8c2e91d47
Create Date: 2026-09-11 23:31:00.000000+00:00

Adds two nullable FK columns so an agent version can declare which embedding
provider + model to use when RAG is active (knowledge_base_id is set).
Both columns are nullable so pre-existing published versions remain valid —
they simply don't use retrieval.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7d4f1a02c58"
down_revision: str | None = "a3f8c2e91d47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_versions",
        sa.Column(
            "embedding_provider_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("providers.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.add_column(
        "agent_versions",
        sa.Column(
            "embedding_model_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_versions", "embedding_model_id")
    op.drop_column("agent_versions", "embedding_provider_id")
