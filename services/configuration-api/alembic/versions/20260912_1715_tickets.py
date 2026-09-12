"""Tenant support tickets with RLS (Phase 6.0).

Revision ID: c8e1a4b70d29
Revises: f4d8b2c93e15
Create Date: 2026-09-12 17:15:00.000000+00:00

Tickets are tenant-owned, so they get the same ``tenant_isolation`` policy
as the tables covered by ``20260912_1200_row_level_security``. They are
listed here rather than retrofitted into that revision, because that
revision has already been applied.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c8e1a4b70d29"
down_revision: str | None = "f4d8b2c93e15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Tables this revision adds to RLS. Unioned with the original list in
#: ``tests/test_rls.py`` so TENANT_OWNED_TABLES stays complete.
_ADDITIONAL_RLS_TABLES: list[str] = ["tickets"]

_POLICY_USING = (
    "nullif(current_setting('app.tenant_id', TRUE), '') IS NULL "
    "OR tenant_id = nullif(current_setting('app.tenant_id', TRUE), '')::uuid"
)


def upgrade() -> None:
    op.create_table(
        "tickets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ticket_number", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=64), nullable=False, server_default="OPEN"),
        sa.Column("priority", sa.String(length=64), nullable=False, server_default="NORMAL"),
        sa.Column("source", sa.String(length=64), nullable=False, server_default="MANUAL"),
        sa.Column("caller_number", sa.String(length=64), nullable=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("call_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "ticket_number", name="uq_tickets_tenant_number"),
    )
    op.create_index("ix_tickets_tenant_id", "tickets", ["tenant_id"])
    op.create_index("ix_tickets_status", "tickets", ["status"])
    op.create_index("ix_tickets_agent_id", "tickets", ["agent_id"])
    op.create_index("ix_tickets_call_id", "tickets", ["call_id"])

    for table in _ADDITIONAL_RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({_POLICY_USING})")


def downgrade() -> None:
    for table in _ADDITIONAL_RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_tickets_call_id", table_name="tickets")
    op.drop_index("ix_tickets_agent_id", table_name="tickets")
    op.drop_index("ix_tickets_status", table_name="tickets")
    op.drop_index("ix_tickets_tenant_id", table_name="tickets")
    op.drop_table("tickets")
