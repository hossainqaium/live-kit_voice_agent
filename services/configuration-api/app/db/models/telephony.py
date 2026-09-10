"""PBXs, SIP trunks, phone numbers and LiveKit dispatch rules.

Spec 14 (PBX management), 15 (SIP trunks), 17 (DIDs) and 21 (dispatch rules).
Trunks and dispatch rules are mirrored into LiveKit, so they carry
``LiveKitSyncMixin``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    Base,
    LiveKitSyncMixin,
    TenantOwnedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    enum_column,
    json_column,
)
from shared.models import (
    ConnectionTestResult,
    PbxType,
    ResourceStatus,
    RoomStrategy,
    SipTransport,
    TrunkDirection,
)


class Pbx(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin):
    """A tenant's PBX (spec 14).

    Not managed by the platform — this is a record of something the tenant
    already operates, so the platform knows where to send transferred calls
    and which trunks belong together.
    """

    __tablename__ = "pbxs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_pbxs_tenant_name"),
        CheckConstraint("port > 0 AND port <= 65535", name="port_in_range"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    pbx_type: Mapped[PbxType] = enum_column(PbxType, nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=5060)
    transport: Mapped[SipTransport] = enum_column(
        SipTransport, nullable=False, default=SipTransport.UDP
    )
    status: Mapped[ResourceStatus] = enum_column(
        ResourceStatus, nullable=False, default=ResourceStatus.ACTIVE, index=True
    )

    # --- Connection test (spec 14) ---------------------------------------- #
    last_test_result: Mapped[ConnectionTestResult] = enum_column(
        ConnectionTestResult, nullable=False, default=ConnectionTestResult.UNTESTED
    )
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_detail: Mapped[str | None] = mapped_column(Text)

    description: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<Pbx {self.name} {self.host}:{self.port}>"


class SipTrunk(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin, LiveKitSyncMixin):
    """A SIP trunk between a tenant's PBX and LiveKit SIP (spec 15).

    Creating one here creates the corresponding LiveKit SIP resource through
    the LiveKit API; ``livekit_resource_id`` holds the returned trunk ID
    (spec 12, 15).
    """

    __tablename__ = "sip_trunks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_sip_trunks_tenant_name"),
        CheckConstraint("port > 0 AND port <= 65535", name="port_in_range"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    pbx_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("pbxs.id", ondelete="SET NULL"), index=True
    )
    direction: Mapped[TrunkDirection] = enum_column(
        TrunkDirection, nullable=False, default=TrunkDirection.INBOUND
    )

    sip_host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=5060)
    transport: Mapped[SipTransport] = enum_column(
        SipTransport, nullable=False, default=SipTransport.UDP
    )

    # --- Security (spec 15, 53) ------------------------------------------- #
    #: Source addresses permitted to send calls on this trunk. An empty list
    #: means no IP restriction, which is why the API requires it to be set
    #: explicitly rather than defaulting to open.
    allowed_ips: Mapped[list[str]] = json_column(nullable=False, default=list)

    auth_username: Mapped[str | None] = mapped_column(String(255))
    media_encryption_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # --- Media (spec 15) --------------------------------------------------- #
    #: Ordered codec preference, e.g. ["PCMU", "OPUS"].
    codecs: Mapped[list[str]] = json_column(nullable=False, default=list)
    dtmf_mode: Mapped[str | None] = mapped_column(String(32))

    status: Mapped[ResourceStatus] = enum_column(
        ResourceStatus, nullable=False, default=ResourceStatus.ACTIVE, index=True
    )

    last_test_result: Mapped[ConnectionTestResult] = enum_column(
        ConnectionTestResult, nullable=False, default=ConnectionTestResult.UNTESTED
    )
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_detail: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<SipTrunk {self.name}>"


class SipCredential(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin):
    """A trunk's SIP password, encrypted at rest (spec 15, 53).

    Kept out of ``sip_trunks`` so that the common read path — listing trunks in
    the UI — never loads ciphertext, and so access to secrets is a separate,
    auditable query rather than a side effect of rendering a table.
    """

    __tablename__ = "sip_credentials"
    __table_args__ = (UniqueConstraint("sip_trunk_id", name="uq_sip_credentials_sip_trunk_id"),)

    sip_trunk_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("sip_trunks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    username: Mapped[str] = mapped_column(String(255), nullable=False)

    #: Ciphertext. Never a plaintext password, and never logged (spec 54).
    password_ciphertext: Mapped[bytes] = mapped_column(postgresql.BYTEA, nullable=False)

    #: Which envelope key encrypted this value, so keys can be rotated without
    #: re-encrypting everything at once.
    encryption_key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LiveKitDispatchRule(
    Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin, LiveKitSyncMixin
):
    """A long-lived LiveKit SIP dispatch rule (spec 21).

    Dispatch rules are configuration objects, created once and reused. A rule
    per call would leave thousands of stale rules in LiveKit and add an admin
    API round-trip to the call setup path, where the latency is most visible.
    """

    __tablename__ = "livekit_dispatch_rules"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_livekit_dispatch_rules_tenant_name"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    sip_trunk_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("sip_trunks.id", ondelete="CASCADE"), index=True
    )

    #: One room per call is what a single-caller AI conversation needs
    #: (spec 22).
    room_strategy: Mapped[RoomStrategy] = enum_column(
        RoomStrategy, nullable=False, default=RoomStrategy.INDIVIDUAL
    )

    #: Prefix for generated room names, so rooms are attributable to a tenant
    #: when inspecting LiveKit directly.
    room_prefix: Mapped[str | None] = mapped_column(String(128))

    #: The agent-dispatch identity LiveKit should assign the call to. Matches
    #: the worker's registered agent name.
    agent_dispatch_name: Mapped[str] = mapped_column(String(128), nullable=False)

    #: DIDs this rule matches. Empty means it matches any number on the trunk.
    matched_numbers: Mapped[list[str]] = json_column(nullable=False, default=list)

    status: Mapped[ResourceStatus] = enum_column(
        ResourceStatus, nullable=False, default=ResourceStatus.ACTIVE, index=True
    )

    def __repr__(self) -> str:
        return f"<LiveKitDispatchRule {self.name}>"


class PhoneNumber(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin):
    """A DID (spec 17)."""

    __tablename__ = "phone_numbers"
    __table_args__ = (
        # A number can only belong to one tenant at a time; two tenants
        # claiming the same DID would make inbound routing ambiguous.
        UniqueConstraint("number", name="uq_phone_numbers_number"),
    )

    #: E.164, e.g. +8801700000000. Normalised on write so that lookups during
    #: call setup are an index hit rather than a scan over format variants.
    number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    pbx_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("pbxs.id", ondelete="SET NULL"), index=True
    )
    sip_trunk_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("sip_trunks.id", ondelete="SET NULL"), index=True
    )

    #: Agent answering calls to this number. Nullable so a DID can be
    #: registered before its agent exists.
    inbound_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    routing_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("routing_rules.id", ondelete="SET NULL")
    )
    business_hours_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("business_hours.id", ondelete="SET NULL")
    )

    label: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[ResourceStatus] = enum_column(
        ResourceStatus, nullable=False, default=ResourceStatus.ACTIVE, index=True
    )

    def __repr__(self) -> str:
        return f"<PhoneNumber {self.number}>"
