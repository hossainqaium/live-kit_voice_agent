"""Platform console schemas (spec 60).

Tenants, the AI provider catalog, audit history and capacity. Everything here
is platform-scoped: no request body carries a tenant ID for the caller to
choose, except the tenant resource itself, which *is* the thing being created.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, IPvAnyAddress, field_validator

from app.schemas.common import TimestampedResponse
from shared.models import PlatformRole, ProviderKind, ResourceStatus, TenantStatus

_SLUG_ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789-")


def _validate_slug(value: str) -> str:
    """A slug ends up in URLs and LiveKit metadata, so keep it boring."""
    lowered = value.strip().lower()
    if not lowered:
        raise ValueError("a slug cannot be empty")
    if set(lowered) - _SLUG_ALLOWED:
        raise ValueError("a slug may contain only lowercase letters, digits and hyphens")
    if lowered.startswith("-") or lowered.endswith("-"):
        raise ValueError("a slug cannot start or end with a hyphen")
    return lowered


# --------------------------------------------------------------------------- #
# Tenants
# --------------------------------------------------------------------------- #


class TenantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=2, max_length=100)

    timezone: str = Field(default="UTC", max_length=64)
    default_language: str = Field(default="en", max_length=16)

    #: Null means no limit. Spec 47 makes these the platform's lever, which is
    #: why they are settable here and not in the tenant's own settings.
    max_concurrent_calls: int | None = Field(default=None, ge=1, le=10000)
    max_daily_calls: int | None = Field(default=None, ge=1)
    max_monthly_minutes: int | None = Field(default=None, ge=1)

    notes: str | None = Field(default=None, max_length=4000)

    #: The first administrator. Creating a tenant with no way to sign in would
    #: leave the platform operator to do it in two steps, and the second step
    #: is the one that gets forgotten.
    admin_email: str | None = None
    admin_full_name: str | None = Field(default=None, max_length=255)
    admin_password: str | None = Field(default=None, min_length=12, max_length=72)

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str) -> str:
        return _validate_slug(value)


class TenantUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    status: TenantStatus | None = None
    timezone: str | None = Field(default=None, max_length=64)
    default_language: str | None = Field(default=None, max_length=16)
    max_concurrent_calls: int | None = Field(default=None, ge=1, le=10000)
    max_daily_calls: int | None = Field(default=None, ge=1)
    max_monthly_minutes: int | None = Field(default=None, ge=1)
    notes: str | None = Field(default=None, max_length=4000)


class TenantSummary(TimestampedResponse):
    """A tenant plus the counts the platform list view needs."""

    name: str
    slug: str
    status: TenantStatus
    timezone: str
    default_language: str

    max_concurrent_calls: int | None
    max_daily_calls: int | None
    max_monthly_minutes: int | None
    notes: str | None

    user_count: int = 0
    agent_count: int = 0
    pbx_count: int = 0
    phone_number_count: int = 0
    calls_last_30_days: int = 0
    active_calls: int = 0

    #: Present only in the create response, and only when an administrator was
    #: requested. Never a password — just the address to hand over.
    admin_email: str | None = None


# --------------------------------------------------------------------------- #
# Provider catalog
# --------------------------------------------------------------------------- #


class ProviderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ProviderKind
    slug: str = Field(min_length=2, max_length=64)

    #: Which worker adapter drives it. Defaults to the slug, which is what
    #: every provider seeded before this field existed relies on. Set it when
    #: registering a second endpoint for a protocol that already has one — a
    #: self-hosted OpenAI-compatible server alongside the hosted service.
    adapter: str | None = Field(default=None, min_length=2, max_length=64)

    display_name: str = Field(min_length=1, max_length=128)

    supports_streaming: bool = True
    default_base_url: str | None = Field(default=None, max_length=512)

    #: False for a self-hosted provider reached over the LAN (spec 25). The
    #: agent validator reads this, so getting it wrong either blocks a valid
    #: self-hosted setup or lets a cloud provider be selected with no key.
    requires_credential: bool = True

    notes: str | None = Field(default=None, max_length=4000)

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str) -> str:
        return _validate_slug(value)


class ProviderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    status: ResourceStatus | None = None
    supports_streaming: bool | None = None
    default_base_url: str | None = Field(default=None, max_length=512)
    requires_credential: bool | None = None
    notes: str | None = Field(default=None, max_length=4000)


class ProviderResponse(TimestampedResponse):
    kind: ProviderKind
    slug: str
    adapter: str | None
    display_name: str
    status: ResourceStatus
    supports_streaming: bool
    default_base_url: str | None
    requires_credential: bool
    notes: str | None

    model_count: int = 0
    voice_count: int = 0

    #: How many tenants hold a credential for it. Platform staff never see the
    #: keys themselves — spec 26 keeps those write-only — but knowing a
    #: provider is in use is what stops it being retired by accident.
    credential_count: int = 0


class ModelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: uuid.UUID
    slug: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)
    languages: list[str] = Field(default_factory=list)
    is_default: bool = False


class ModelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    languages: list[str] | None = None
    status: ResourceStatus | None = None
    is_default: bool | None = None


class ModelResponse(TimestampedResponse):
    provider_id: uuid.UUID
    provider_slug: str | None = None
    provider_kind: ProviderKind | None = None
    slug: str
    display_name: str
    languages: list[str]
    status: ResourceStatus
    is_default: bool


class VoiceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: uuid.UUID

    #: The provider's own identifier, passed through to the TTS call verbatim.
    voice_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)

    language: str | None = Field(default=None, max_length=16)
    accent: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=4000)
    is_default: bool = False


class VoiceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    language: str | None = Field(default=None, max_length=16)
    accent: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=4000)
    status: ResourceStatus | None = None
    is_default: bool | None = None


class VoiceResponse(TimestampedResponse):
    provider_id: uuid.UUID
    provider_slug: str | None = None
    voice_id: str
    name: str
    language: str | None
    accent: str | None
    description: str | None
    status: ResourceStatus
    is_default: bool
    sample_object_key: str | None


class VoiceTestRequest(BaseModel):
    """One-shot preview synthesis (spec 27, 4b.5).

    ``api_key`` is write-only: used for this request, never stored or returned.
    Required only when the provider's ``requires_credential`` is true.
    """

    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, min_length=1, max_length=500)
    api_key: str | None = Field(default=None, min_length=1, max_length=512)
    model: str | None = Field(default=None, min_length=1, max_length=128)


class VoiceTestResponse(BaseModel):
    sample_object_key: str
    content_type: str
    bytes: int


# --------------------------------------------------------------------------- #
# Audit history (spec 69)
# --------------------------------------------------------------------------- #


class AuditLogResponse(BaseModel):
    """One audit entry.

    ``old_value``/``new_value`` are already redacted on write, so this returns
    them as stored rather than redacting again at read time — redacting twice
    would hide the fact that something was not redacted on the way in.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    occurred_at: datetime
    tenant_id: uuid.UUID | None
    user_id: uuid.UUID | None
    user_email: str | None
    action: str
    resource_type: str
    resource_id: str | None
    old_value: dict | None
    new_value: dict | None

    #: The column is PostgreSQL ``INET``, so the driver hands back an
    #: ``IPv4Address``/``IPv6Address`` object, not a string. Typing this ``str``
    #: made every read of the trail fail validation.
    ip_address: IPvAnyAddress | None
    request_id: str | None

    #: Resolved for display. Null when the tenant was deleted after the fact,
    #: which is exactly when an audit row matters most.
    tenant_name: str | None = None


# --------------------------------------------------------------------------- #
# Capacity and infrastructure (spec 48)
# --------------------------------------------------------------------------- #


class ComponentHealth(BaseModel):
    """One dependency's state, as actually probed."""

    name: str
    reachable: bool

    #: Round trip in milliseconds. Null when the probe failed, rather than 0,
    #: which would read as "instant".
    latency_ms: float | None = None
    detail: str | None = None


class ResourceUsage(BaseModel):
    """One scraped resource reading. ``value`` is null when the probe has no gauge."""

    source: str
    value: float | None = None
    unit: str
    detail: str | None = None


class ProviderHealthStatus(BaseModel):
    """Catalog row plus optional worker circuit-breaker overlay (spec 48)."""

    slug: str
    kind: str
    display_name: str
    status: str
    credentialed_tenants: int
    detail: str | None = None


class CapacityResponse(BaseModel):
    """Platform capacity (spec 48).

    Inventory counts come from PostgreSQL. Fleet figures are probed from
    worker ``/ready`` and Prometheus metrics. A probe that does not answer
    leaves the field null rather than inventing a percentage.
    """

    tenant_count: int
    active_tenant_count: int
    agent_count: int
    published_agent_count: int
    phone_number_count: int
    sip_trunk_count: int

    active_calls: int
    calls_last_24h: int

    #: Sum of every tenant's ``max_concurrent_calls``. Null when at least one
    #: tenant is uncapped, because the sum would then be a floor presented as
    #: a ceiling.
    licensed_concurrent_calls: int | None = None

    #: Licensed concurrency when every tenant is capped; otherwise the sum of
    #: worker ``/ready`` capacities. Null when neither is known.
    total_capacity: int | None = None
    available_capacity: int | None = None

    livekit_rooms: int | None = None
    livekit_nodes: int | None = None
    sip_nodes: int | None = None
    ai_workers: int | None = None
    worker_utilization: float | None = None
    cpu: ResourceUsage | None = None
    memory: ResourceUsage | None = None
    network: ResourceUsage | None = None
    providers: list[ProviderHealthStatus] = Field(default_factory=list)
    components: list[ComponentHealth] = Field(default_factory=list)


class LiveKitSyncActionResponse(BaseModel):
    """Immediate ack after queueing Synchronize / Retry / Repair (spec 12, 80)."""

    kind: str
    id: uuid.UUID
    action: str
    sync_status: str


# --------------------------------------------------------------------------- #
# LiveKit administration (spec 12, 46)
# --------------------------------------------------------------------------- #


class DriftedResource(BaseModel):
    """A row whose LiveKit mirror does not match PostgreSQL (spec 46)."""

    kind: str
    id: uuid.UUID
    tenant_id: uuid.UUID | None
    tenant_name: str | None
    name: str
    sync_status: str
    livekit_resource_id: str | None
    sync_error: str | None
    sync_attempts: int
    last_synced_at: datetime | None


class OrphanedResource(BaseModel):
    """A LiveKit object no PostgreSQL row names (the other drift direction)."""

    kind: str
    name: str
    livekit_resource_id: str
    reason: str


class LiveKitOverview(BaseModel):
    """LiveKit's state as the Control Plane sees it.

    PostgreSQL is the source of truth and LiveKit is the mirror (spec 12), so
    this reports the mirror's agreement with it rather than reading LiveKit's
    own view as authoritative. The interesting number is the disagreement.
    """

    url: str
    sip_uri: str
    reachable: bool
    room_count: int | None = None
    detail: str | None = None

    #: Counts by ``sync_status`` across every tenant, per resource kind.
    trunk_sync: dict[str, int] = Field(default_factory=dict)
    dispatch_rule_sync: dict[str, int] = Field(default_factory=dict)

    #: Only the rows that need attention. A fully synced platform returns an
    #: empty list, which is the answer worth seeing at a glance.
    needs_attention: list[DriftedResource] = Field(default_factory=list)

    #: Last scheduled or on-demand compare. Null until the first check in
    #: this process; row-level ``DRIFTED`` still shows in ``needs_attention``.
    last_drift_check_at: datetime | None = None
    drift_check_reachable: bool | None = None
    orphans: list[OrphanedResource] = Field(default_factory=list)
    configuration_drift_detected: bool = False

    #: Spec 11 administration topics. Tenant configuration is linked; cluster
    #: topology, TURN and port ranges are reported read-only (spec 13).
    sections: list["LiveKitAdminSection"] = Field(default_factory=list)
    rooms: list["LiveKitRoom"] = Field(default_factory=list)
    participant_count: int = 0
    agent_dispatch_names: list[str] = Field(default_factory=list)
    public_url: str | None = None


class LiveKitAdminSection(BaseModel):
    """One topic from the spec-11 administration section."""

    id: str
    title: str
    summary: str
    #: ``tenant`` is configured in the tenant console; ``observe`` is live
    #: state; ``infra`` is platform/DevOps and never editable here.
    scope: str
    href: str | None = None


class LiveKitRoom(BaseModel):
    name: str
    sid: str
    num_participants: int
    created_at: datetime | None = None
    metadata: str = ""


class DriftCheckResponse(BaseModel):
    """One compare pass (spec 46). Detection only — nothing is repaired."""

    checked_at: datetime
    livekit_reachable: bool
    trunks_compared: int
    rules_compared: int
    drifted: int
    orphan_count: int
    configuration_drift_detected: bool
    error: str | None = None
    orphans: list[OrphanedResource] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Platform staff (spec 8, 60)
# --------------------------------------------------------------------------- #


class PlatformUserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    full_name: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=12, max_length=72)

    #: Platform roles only. A platform account with a tenant role would hold
    #: permissions inside a tenant it does not belong to.
    role: PlatformRole


class PlatformUserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    role: PlatformRole | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=12, max_length=72)


class PlatformUserResponse(TimestampedResponse):
    email: str
    full_name: str
    is_active: bool
    last_login_at: datetime | None
    roles: list[str] = Field(default_factory=list)
    sessions_revoked: bool = False


# --------------------------------------------------------------------------- #
# Effective configuration (spec 60 "Settings")
# --------------------------------------------------------------------------- #


class SettingEntry(BaseModel):
    """One effective configuration value.

    ``value`` is null for a secret. The name and the fact that it is set are
    useful — "is CREDENTIAL_ENCRYPTION_KEY configured?" is a real question —
    but the value itself must never leave the process (spec 70).
    """

    name: str
    value: str | None
    is_secret: bool = False
    #: True when a secret is configured, so the console can distinguish a
    #: redacted value from an unset one.
    is_set: bool = True
    note: str | None = None


class PlatformSettingsResponse(BaseModel):
    """What the running process is actually configured with.

    Read-only, and it says why: these come from the environment, so changing
    one means changing ``.env`` or the compose file and restarting. An editable
    screen here would write somewhere that the next restart overwrites.
    """

    environment: str
    editable: bool = False
    source: str = (
        "Environment variables read at process start. "
        "Change .env or the compose file and restart the service."
    )
    groups: dict[str, list[SettingEntry]] = Field(default_factory=dict)
