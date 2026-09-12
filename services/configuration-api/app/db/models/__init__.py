"""SQLAlchemy models — the schema from spec 68.

Every model is imported here so ``Base.metadata`` is complete when Alembic
autogenerates a revision. A model that is not imported produces an empty diff
and, worse, a later autogenerate that tries to drop its table.

Tenant isolation (spec 6, 7) is structural: every tenant-owned model uses
``TenantOwnedMixin``, so a tenant-owned table cannot be declared without
``tenant_id``. The three deliberate exceptions each carry a nullable
``tenant_id`` and a comment explaining why:

* ``users`` — platform staff belong to no tenant
* ``user_roles`` — a platform-scoped role assignment has no tenant
* ``audit_logs`` — platform-level actions must still be audited

The platform catalog (``providers``, ``models``, ``voices``, ``roles``,
``permissions``, ``role_permissions``) has no tenant dimension at all: it is
managed by SUPER_ADMIN in the platform console (spec 60).
"""

from __future__ import annotations

from app.db.models.agent import Agent, AgentTool, AgentVersion, AgentVoice
from app.db.models.audit import AuditLog
from app.db.models.auth import Permission, Role, RolePermission, User, UserRole
from app.db.models.billing import Billing, Subscription, Usage
from app.db.models.call import (
    Call,
    CallEvent,
    CallRecording,
    CallTranscript,
    CallTranscriptSegment,
)
from app.db.models.knowledge import KnowledgeBase, KnowledgeChunk, KnowledgeDocument
from app.db.models.provider import Model, Provider, ProviderCredential, Voice
from app.db.models.routing import (
    BusinessHours,
    BusinessHoursInterval,
    RoutingRule,
    TransferDestination,
)
from app.db.models.telephony import (
    LiveKitDispatchRule,
    Pbx,
    PhoneNumber,
    SipCredential,
    SipTrunk,
)
from app.db.models.tenant import Tenant
from app.db.models.ticket import Ticket
from app.db.models.tool import Tool, ToolPermission

__all__ = [
    "Agent",
    "AgentTool",
    "AgentVersion",
    "AgentVoice",
    "AuditLog",
    "Billing",
    "BusinessHours",
    "BusinessHoursInterval",
    "Call",
    "CallEvent",
    "CallRecording",
    "CallTranscript",
    "CallTranscriptSegment",
    "KnowledgeBase",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "LiveKitDispatchRule",
    "Model",
    "Pbx",
    "Permission",
    "PhoneNumber",
    "Provider",
    "ProviderCredential",
    "Role",
    "RolePermission",
    "RoutingRule",
    "SipCredential",
    "SipTrunk",
    "Subscription",
    "Tenant",
    "Ticket",
    "Tool",
    "ToolPermission",
    "TransferDestination",
    "Usage",
    "User",
    "UserRole",
    "Voice",
]

#: Tables that must carry ``tenant_id`` with a NOT NULL constraint.
#: Asserted by a test, so adding a tenant-owned model without the mixin fails
#: CI rather than shipping a cross-tenant leak (spec 6, 7).
TENANT_OWNED_TABLES: frozenset[str] = frozenset(
    {
        "agents",
        "agent_versions",
        "agent_tools",
        "agent_voices",
        "business_hours",
        "business_hours_intervals",
        "calls",
        "call_events",
        "call_recordings",
        "call_transcripts",
        "call_transcript_segments",
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
        "billing",
        "tickets",
        "tools",
        "tool_permissions",
        "transfer_destinations",
        "usage",
    }
)

#: Tables with a nullable ``tenant_id``, each for a stated reason above.
TENANT_OPTIONAL_TABLES: frozenset[str] = frozenset({"users", "user_roles", "audit_logs"})

#: Platform-level tables with no tenant dimension.
PLATFORM_TABLES: frozenset[str] = frozenset(
    {
        "tenants",
        "roles",
        "permissions",
        "role_permissions",
        "providers",
        "models",
        "voices",
        "alembic_version",
    }
)
