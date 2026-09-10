"""Tenant model (spec 6, 47)."""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, enum_column
from shared.models import TenantStatus


class Tenant(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A customer of the platform.

    The root of every ownership chain: every tenant-owned table carries
    ``tenant_id`` referencing this table (spec 6).
    """

    __tablename__ = "tenants"
    __table_args__ = (
        # Limits are counts, not sentinels. Null means "no limit"; zero would
        # mean "no calls allowed", so a negative value is the only nonsense.
        CheckConstraint(
            "max_concurrent_calls IS NULL OR max_concurrent_calls >= 0",
            name="max_concurrent_calls_non_negative",
        ),
        CheckConstraint(
            "max_daily_calls IS NULL OR max_daily_calls >= 0",
            name="max_daily_calls_non_negative",
        ),
        CheckConstraint(
            "max_monthly_minutes IS NULL OR max_monthly_minutes >= 0",
            name="max_monthly_minutes_non_negative",
        ),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    #: URL-safe identifier, unique platform-wide. Used in admin URLs and log
    #: lines where a UUID would be unreadable.
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)

    status: Mapped[TenantStatus] = enum_column(
        TenantStatus, nullable=False, default=TenantStatus.ACTIVE, index=True
    )

    #: IANA timezone name. Business hours (spec 37) are meaningless without it,
    #: and storing it per tenant avoids interpreting a tenant's 09:00 in the
    #: server's timezone.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")

    default_language: Mapped[str] = mapped_column(String(16), nullable=False, default="en")

    # --- Call limits, enforced before a call is accepted (spec 47) --------- #
    max_concurrent_calls: Mapped[int | None] = mapped_column(Integer)
    max_daily_calls: Mapped[int | None] = mapped_column(Integer)
    max_monthly_minutes: Mapped[int | None] = mapped_column(Integer)

    notes: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<Tenant {self.slug}>"
