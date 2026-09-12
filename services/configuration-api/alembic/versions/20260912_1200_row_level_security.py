"""Enable PostgreSQL Row Level Security on all tenant-owned tables (spec 7, 3b.1).

Revision ID: f4d8b2c93e15
Revises: b7d4f1a02c58
Create Date: 2026-09-12 12:00:00.000000+00:00

RLS is the database-level second layer of tenant isolation beneath
``TenantRepository``.  The first layer (``WHERE tenant_id = :tid``) prevents
cross-tenant reads through the normal application path.  RLS prevents them
even when a query is issued outside ``TenantRepository`` — for example by a
future endpoint that forgets the filter, or by a compromised query parameter.

Policy
------
Each tenant-owned table gets one permissive policy named ``tenant_isolation``::

    USING (
        nullif(current_setting('app.tenant_id', TRUE), '') IS NULL
        OR tenant_id = nullif(current_setting('app.tenant_id', TRUE), '')::uuid
    )

``current_setting('app.tenant_id', TRUE)`` returns an empty string when the
GUC is not set (the second argument suppresses the "unrecognised parameter"
error).  The ``nullif(..., '')`` converts the empty string to NULL.

- **Tenant request** (GUC set): first condition FALSE; second condition
  matches only the caller's rows.
- **Platform / admin request** (GUC not set): ``NULL IS NULL`` = TRUE;
  all rows visible — platform routes are not scoped by tenant and must be
  able to see the full table.
- **Wrong tenant** (GUC set to a different UUID): both conditions FALSE;
  row excluded.

``FORCE ROW LEVEL SECURITY`` is required because the application connects as
``voice_agent``, which owns the tables and would otherwise bypass RLS.

Where the GUC is set
--------------------
``app.core.dependencies.get_tenant_scope`` calls
``SELECT set_config('app.tenant_id', :tid, true)`` immediately after
resolving the tenant ID from the token, so every subsequent query in the same
transaction sees the correct tenant boundary.

Tables covered (27)
-------------------
All tables in ``TENANT_OWNED_TABLES`` in ``app.db.models.__init__``.
The three ``TENANT_OPTIONAL_TABLES`` (``users``, ``user_roles``,
``audit_logs``) have nullable ``tenant_id`` and are left to application-layer
control.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f4d8b2c93e15"
down_revision: str | None = "b7d4f1a02c58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Authoritative list — kept in sync with TENANT_OWNED_TABLES in
# app/db/models/__init__.py.  Updating one without the other is a defect.
_TENANT_OWNED_TABLES: list[str] = [
    "agents",
    "agent_versions",
    "agent_tools",
    "agent_voices",
    "billing",
    "business_hours",
    "business_hours_intervals",
    "call_events",
    "call_recordings",
    "call_transcripts",
    "call_transcript_segments",
    "calls",
    "knowledge_bases",
    "knowledge_chunks",
    "knowledge_documents",
    "livekit_dispatch_rules",
    "pbxs",
    "phone_numbers",
    "provider_credentials",
    "routing_rules",
    "sip_credentials",
    "sip_trunks",
    "subscriptions",
    "tool_permissions",
    "tools",
    "transfer_destinations",
    "usage",
]

#: Policy expression shared by every table.  Reads cleanly as:
#:   "pass when the GUC is absent (platform routes) OR when it matches this row."
_POLICY_USING = (
    "nullif(current_setting('app.tenant_id', TRUE), '') IS NULL "
    "OR tenant_id = nullif(current_setting('app.tenant_id', TRUE), '')::uuid"
)


def upgrade() -> None:
    for table in _TENANT_OWNED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING ({_POLICY_USING})"
        )


def downgrade() -> None:
    for table in _TENANT_OWNED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
