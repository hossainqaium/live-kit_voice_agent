"""Shared request and response shapes.

``tenant_id`` never appears on any schema in this package. Spec 7 forbids
trusting a browser-supplied tenant, and the router-wide guard rejects one;
leaving it off the schemas means a handler has nothing to accidentally read.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

ItemT = TypeVar("ItemT")


class PaginationParams(BaseModel):
    """Offset pagination.

    Offset rather than cursor: the admin lists this serves are small and sorted
    by name or creation time, and a cursor would add complexity the UI does not
    need. Call and transcript listings, which do grow, get cursors when they
    are added.
    """

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class Page(BaseModel, Generic[ItemT]):
    """A page of results plus the total, so a UI can render a count."""

    items: list[ItemT]
    total: int
    limit: int
    offset: int


class TimestampedResponse(BaseModel):
    """Fields every persisted resource returns."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class LiveKitSyncStatus(BaseModel):
    """Synchronisation state of a mirrored LiveKit resource (spec 12, 46).

    Surfaced on every resource that has a LiveKit counterpart so an operator
    can see drift without opening the LiveKit admin section.
    """

    model_config = ConfigDict(from_attributes=True)

    sync_status: str
    livekit_resource_id: str | None
    last_synced_at: datetime | None
    sync_error: str | None
    sync_attempts: int


class OperationResult(BaseModel):
    """Result of an action that is not a create, read, update or delete."""

    ok: bool
    detail: str | None = None
