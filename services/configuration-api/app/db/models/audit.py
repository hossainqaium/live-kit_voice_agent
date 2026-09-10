"""Audit log (spec 69).

Every configuration change is recorded with who, when, what, and the before
and after values. An authorization model without an audit trail cannot be
reviewed after an incident.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, json_column


class AuditLog(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One recorded action.

    ``tenant_id`` is nullable rather than using ``TenantOwnedMixin``, because
    platform-level actions (creating a tenant, editing the provider catalog)
    belong to no tenant but must still be audited.

    Rows are append-only. Nothing in the application updates or deletes them:
    an audit trail an operator can edit is not evidence.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        # The two questions actually asked of this table: "what happened in
        # this tenant recently" and "what happened to this resource".
        Index("ix_audit_logs_tenant_occurred", "tenant_id", "occurred_at"),
        Index("ix_audit_logs_resource", "resource_type", "resource_id"),
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="SET NULL"),
        index=True,
    )

    #: SET NULL rather than CASCADE: deleting a user must not erase the record
    #: of what they did.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    #: Denormalised so the trail stays readable after the account is gone.
    user_email: Mapped[str | None] = mapped_column(String(320))

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    #: Dotted action name, e.g. ``agent.published`` or ``credential.rotated``
    #: (spec 69).
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64))

    #: Values before and after. Secret-bearing fields are redacted before they
    #: are written — an audit row must never become a way to read a
    #: credential (spec 54).
    old_value: Mapped[dict | None] = json_column()
    new_value: Mapped[dict | None] = json_column()

    ip_address: Mapped[str | None] = mapped_column(postgresql.INET)
    user_agent: Mapped[str | None] = mapped_column(Text)

    #: Ties an audited change to the HTTP request that made it.
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)

    def __repr__(self) -> str:
        return f"<AuditLog {self.action} {self.resource_type}>"
