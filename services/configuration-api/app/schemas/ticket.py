"""Support ticket schemas (Phase 6.0)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field
from shared.models import TicketPriority, TicketSource, TicketStatus

from app.schemas.common import TimestampedResponse


class TicketCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=8000)
    priority: TicketPriority = TicketPriority.NORMAL
    caller_number: str | None = Field(default=None, max_length=64)


class TicketUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=8000)
    status: TicketStatus | None = None
    priority: TicketPriority | None = None
    caller_number: str | None = Field(default=None, max_length=64)


class TicketResponse(TimestampedResponse):
    ticket_number: str
    title: str
    description: str
    status: TicketStatus
    priority: TicketPriority
    source: TicketSource
    caller_number: str | None
    agent_id: uuid.UUID | None
    call_id: uuid.UUID | None
    agent_name: str | None = None
