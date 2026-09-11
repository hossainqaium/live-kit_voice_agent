"""separate a provider's adapter from its name

Revision ID: d1c4e7a90b23
Revises: c8f1a2d43b90
Create Date: 2026-09-11 12:30:00.000000+00:00

``providers.slug`` served as both the row's name and the worker's registry key,
and it is unique per kind. That made a catalog able to hold only one row per
adapter per kind — so "OpenAI-compatible, hosted" and "OpenAI-compatible,
self-hosted" could not both exist, and the local fallback tier spec 55 asks for
could not be selected.

``adapter`` is nullable and falls back to ``slug``, so every existing row keeps
resolving to exactly the adapter it resolved to before this ran.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d1c4e7a90b23"
down_revision: str | None = "c8f1a2d43b90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("providers", sa.Column("adapter", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("providers", "adapter")
