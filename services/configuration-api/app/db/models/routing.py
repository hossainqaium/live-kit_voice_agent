"""Routing rules, business hours and transfer destinations.

Spec 20 (agent routing), 37 (business hours), 38 (fallback), 35 (transfer
destinations).
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    Base,
    TenantOwnedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    enum_column,
    json_column,
)
from shared.models import (
    DayOfWeek,
    FallbackAction,
    ResourceStatus,
    TransferDestinationKind,
)


class BusinessHours(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin):
    """A named opening-hours schedule (spec 37)."""

    __tablename__ = "business_hours"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_business_hours_tenant_name"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    #: Overrides the tenant timezone when a tenant operates in more than one.
    timezone: Mapped[str | None] = mapped_column(String(64))

    #: Dates that override the weekly schedule, e.g.
    #: [{"date": "2026-12-25", "closed": true, "label": "Christmas"}].
    #: JSONB because holidays are sparse and never joined against.
    holidays: Mapped[list] = json_column(nullable=False, default=list)

    def __repr__(self) -> str:
        return f"<BusinessHours {self.name}>"


class BusinessHoursInterval(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin):
    """One open interval on one weekday.

    A row per interval rather than open/close columns on a day, so a split
    schedule — open 09:00-13:00 and 14:00-18:00 — is representable without a
    second table or a JSON blob.
    """

    __tablename__ = "business_hours_intervals"
    __table_args__ = (CheckConstraint("opens_at < closes_at", name="interval_is_forward"),)

    business_hours_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("business_hours.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    day_of_week: Mapped[DayOfWeek] = enum_column(DayOfWeek, nullable=False, index=True)
    opens_at: Mapped[object] = mapped_column(Time, nullable=False)
    closes_at: Mapped[object] = mapped_column(Time, nullable=False)


class RoutingRule(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin):
    """Decides which agent answers a call (spec 20).

    Evaluated in ``priority`` order. Conditions are stored as JSONB rather than
    columns because spec 20 lists nine possible dimensions and tenants use
    different subsets; a column per dimension would be mostly nulls and still
    not extensible.
    """

    __tablename__ = "routing_rules"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_routing_rules_tenant_name"),
        CheckConstraint("priority >= 0", name="priority_non_negative"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    #: Lower runs first. Ties are broken by specificity — more conditions wins
    #: — so that adding a broad catch-all rule cannot shadow a precise one.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100, index=True)

    #: Match conditions from spec 20: pbx_id, sip_trunk_id, did, caller_number,
    #: destination_number, business_hours_id, campaign. An empty object matches
    #: every call, which is what a tenant-wide default rule needs.
    conditions: Mapped[dict] = json_column(nullable=False, default=dict)

    # --- Target ------------------------------------------------------------ #
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    business_hours_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("business_hours.id", ondelete="SET NULL")
    )

    # --- Fallback chain (spec 38) ----------------------------------------- #
    fallback_action: Mapped[FallbackAction | None] = enum_column(FallbackAction)
    fallback_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL")
    )
    fallback_transfer_destination_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("transfer_destinations.id", ondelete="SET NULL")
    )

    #: Where calls go outside business hours. Separate from the failure
    #: fallback: "we are closed" and "the agent could not be reached" deserve
    #: different treatment.
    closed_action: Mapped[FallbackAction | None] = enum_column(FallbackAction)
    closed_transfer_destination_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("transfer_destinations.id", ondelete="SET NULL")
    )

    status: Mapped[ResourceStatus] = enum_column(
        ResourceStatus, nullable=False, default=ResourceStatus.ACTIVE, index=True
    )

    def __repr__(self) -> str:
        return f"<RoutingRule {self.name} p{self.priority}>"


class TransferDestination(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin):
    """Where a call can be handed to a human (spec 35).

    A named row rather than a free-text field on the agent, so the same
    destination can be reused, validated before publish (spec 63), and changed
    in one place when an extension moves.
    """

    __tablename__ = "transfer_destinations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_transfer_destinations_tenant_name"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[TransferDestinationKind] = enum_column(
        TransferDestinationKind, nullable=False, index=True
    )

    #: The extension, queue name, E.164 number or SIP URI, depending on kind.
    target: Mapped[str] = mapped_column(String(512), nullable=False)

    pbx_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("pbxs.id", ondelete="SET NULL"), index=True
    )
    sip_trunk_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("sip_trunks.id", ondelete="SET NULL")
    )

    #: Whether the AI summary is whispered to the receiving agent before the
    #: legs are bridged (CR-1 TR-5). A queue that announces its own greeting
    #: may want this off.
    whisper_summary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: How long to ring before treating it as no-answer and running the
    #: fallback chain (CR-1 TR-10).
    ring_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30)

    status: Mapped[ResourceStatus] = enum_column(
        ResourceStatus, nullable=False, default=ResourceStatus.ACTIVE, index=True
    )

    def __repr__(self) -> str:
        return f"<TransferDestination {self.name} {self.kind}>"
