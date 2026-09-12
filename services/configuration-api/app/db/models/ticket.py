"""Support tickets filed from the console or by an agent (Phase 6.0)."""

from __future__ import annotations

import uuid

from shared.models import TicketPriority, TicketSource, TicketStatus
from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    Base,
    TenantOwnedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    enum_column,
)


class Ticket(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin):
    """One support ticket owned by a tenant.

    ``ticket_number`` is the human handle (``TCK-0001``). The row UUID stays
    internal so a caller can be told a number that is not a database key.
    """

    __tablename__ = "tickets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "ticket_number", name="uq_tickets_tenant_number"),
    )

    ticket_number: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    status: Mapped[TicketStatus] = enum_column(
        TicketStatus, nullable=False, default=TicketStatus.OPEN, index=True
    )
    priority: Mapped[TicketPriority] = enum_column(
        TicketPriority, nullable=False, default=TicketPriority.NORMAL
    )
    source: Mapped[TicketSource] = enum_column(
        TicketSource, nullable=False, default=TicketSource.MANUAL
    )

    caller_number: Mapped[str | None] = mapped_column(String(64))

    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="SET NULL"),
        index=True,
    )
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("calls.id", ondelete="SET NULL"),
        index=True,
    )

    def __repr__(self) -> str:
        return f"<Ticket {self.ticket_number}>"
