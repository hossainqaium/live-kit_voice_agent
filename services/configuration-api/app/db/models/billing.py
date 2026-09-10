"""Usage metering, billing records and subscriptions (spec 68).

Usage records only — no payment processing. Rows here feed an external biller
and the tenant's Usage view (spec 61).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
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
from shared.models import SubscriptionStatus


class Usage(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin):
    """Daily usage rollup per tenant (spec 47, 61).

    A row per tenant-day rather than per call, because the limits that matter
    are daily calls and monthly minutes (spec 47) and enforcing them by
    aggregating the calls table on every inbound call would put a growing scan
    on the call-setup path.
    """

    __tablename__ = "usage"
    __table_args__ = (
        UniqueConstraint("tenant_id", "usage_date", name="uq_usage_tenant_date"),
        CheckConstraint("call_count >= 0", name="call_count_non_negative"),
        CheckConstraint("total_seconds >= 0", name="total_seconds_non_negative"),
        Index("ix_usage_tenant_date", "tenant_id", "usage_date"),
    )

    #: Date in the tenant's own timezone, so a "daily" limit means the
    #: tenant's day rather than UTC's.
    usage_date: Mapped[date] = mapped_column(Date, nullable=False)

    call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    answered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    transferred_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    total_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Billable AI time, which is not the same as call duration: hold and
    #: post-bridge human conversation are not AI minutes (spec 57 "AI minutes").
    ai_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Per-provider counters, e.g. {"stt": {"deepgram": 1200}}. JSONB because
    #: the provider set changes without a schema change.
    provider_usage: Mapped[dict] = json_column(nullable=False, default=dict)

    def __repr__(self) -> str:
        return f"<Usage {self.usage_date} calls={self.call_count}>"


class Subscription(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin):
    """A tenant's plan (spec 68)."""

    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint(
            "current_period_end IS NULL OR current_period_start IS NULL "
            "OR current_period_end > current_period_start",
            name="period_is_forward",
        ),
    )

    plan_code: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[SubscriptionStatus] = enum_column(
        SubscriptionStatus, nullable=False, default=SubscriptionStatus.TRIALING, index=True
    )

    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Identifier in the external billing system. The platform meters usage;
    #: it does not process payments.
    external_reference: Mapped[str | None] = mapped_column(String(255), index=True)

    #: Plan allowances, which the tenant's own limits (spec 47) must not
    #: exceed.
    included_minutes: Mapped[int | None] = mapped_column(Integer)
    included_concurrent_calls: Mapped[int | None] = mapped_column(Integer)

    def __repr__(self) -> str:
        return f"<Subscription {self.plan_code} {self.status}>"


class Billing(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin):
    """A billing-period statement built from usage (spec 68)."""

    __tablename__ = "billing"
    __table_args__ = (
        UniqueConstraint("tenant_id", "period_start", name="uq_billing_tenant_period"),
        CheckConstraint("period_end > period_start", name="period_is_forward"),
    )

    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("subscriptions.id", ondelete="SET NULL")
    )

    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)

    total_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ai_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Numeric, never float. Binary floating point cannot represent decimal
    #: currency exactly, and rounding drift in money is not acceptable.
    amount_due: Mapped[object | None] = mapped_column(Numeric(14, 4))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")

    #: Line-item breakdown as computed, so a statement can be explained later
    #: even after pricing changes.
    line_items: Mapped[list] = json_column(nullable=False, default=list)

    notes: Mapped[str | None] = mapped_column(Text)
