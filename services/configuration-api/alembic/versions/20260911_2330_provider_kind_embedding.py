"""add EMBEDDING to providerkind (no-op for VARCHAR schema)

Revision ID: a3f8c2e91d47
Revises: d1c4e7a90b23
Create Date: 2026-09-11 23:30:00.000000+00:00

``ProviderKind`` is stored as VARCHAR(64) with client-side validation only
(``native_enum=False`` in enum_column). There is no native PostgreSQL enum
type to alter and no CHECK constraint on providers.kind, so adding a new
Python enum value (EMBEDDING) requires no database schema change.

This revision exists purely to preserve the migration chain. The next
revision (b7d4f1a02c58) adds the actual schema change: the nullable FK
columns on agent_versions for the embedding provider and model.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "a3f8c2e91d47"
down_revision: str | None = "d1c4e7a90b23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nothing to do. providers.kind is VARCHAR(64) — any string the Python
    # ProviderKind enum accepts is accepted by the database without a schema
    # change.
    pass


def downgrade() -> None:
    pass
