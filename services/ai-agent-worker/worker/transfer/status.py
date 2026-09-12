"""Persist warm-transfer status and timings (spec 41, CR-1)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from shared.models import TRANSFER_STATUS_TRANSITIONS, TransferStatus

_UPDATE_SQL = text(
    """
    UPDATE calls SET
        transfer_status = :status,
        transfer_destination_id = COALESCE(:destination_id, transfer_destination_id),
        transfer_announcement_started_at = COALESCE(
            :announcement_started_at, transfer_announcement_started_at
        ),
        transfer_agent_dialed_at = COALESCE(:agent_dialed_at, transfer_agent_dialed_at),
        transfer_agent_answered_at = COALESCE(
            :agent_answered_at, transfer_agent_answered_at
        ),
        transfer_whisper_seconds = COALESCE(:whisper_seconds, transfer_whisper_seconds),
        transfer_bridged_at = COALESCE(:bridged_at, transfer_bridged_at),
        transfer_fallback_taken = COALESCE(:fallback_taken, transfer_fallback_taken),
        transfer_summary = CASE
            WHEN CAST(:summary AS text) IS NULL THEN transfer_summary
            ELSE CAST(:summary AS jsonb)
        END,
        updated_at = :now
    WHERE id = :id
      AND tenant_id = :tenant_id
    """
)


class IllegalTransferTransition(RuntimeError):
    """A transfer_status write the state machine does not allow."""


def assert_legal(from_status: TransferStatus, to_status: TransferStatus) -> None:
    allowed = TRANSFER_STATUS_TRANSITIONS.get(from_status, frozenset())
    if to_status not in allowed:
        raise IllegalTransferTransition(
            f"{from_status} -> {to_status} is not a legal transfer transition; "
            f"allowed: {sorted(s.value for s in allowed)}"
        )


async def advance(
    factory: async_sessionmaker,
    *,
    call_row_id: uuid.UUID,
    tenant_id: uuid.UUID,
    from_status: TransferStatus,
    to_status: TransferStatus,
    destination_id: uuid.UUID | None = None,
    announcement_started_at: datetime | None = None,
    agent_dialed_at: datetime | None = None,
    agent_answered_at: datetime | None = None,
    whisper_seconds: float | None = None,
    bridged_at: datetime | None = None,
    fallback_taken: str | None = None,
    summary: dict[str, Any] | None = None,
) -> TransferStatus:
    """Write ``to_status`` after checking ``TRANSFER_STATUS_TRANSITIONS``."""
    assert_legal(from_status, to_status)
    now = datetime.now(UTC)
    async with factory() as session:
        await session.execute(
            _UPDATE_SQL,
            {
                "id": call_row_id,
                "tenant_id": tenant_id,
                "status": to_status.value,
                "destination_id": destination_id,
                "announcement_started_at": announcement_started_at,
                "agent_dialed_at": agent_dialed_at,
                "agent_answered_at": agent_answered_at,
                "whisper_seconds": whisper_seconds,
                "bridged_at": bridged_at,
                "fallback_taken": fallback_taken,
                "summary": json.dumps(summary) if summary is not None else None,
                "now": now,
            },
        )
        await session.commit()
    return to_status


async def patch(
    factory: async_sessionmaker,
    *,
    call_row_id: uuid.UUID,
    tenant_id: uuid.UUID,
    destination_id: uuid.UUID | None = None,
    announcement_started_at: datetime | None = None,
    agent_dialed_at: datetime | None = None,
    agent_answered_at: datetime | None = None,
    whisper_seconds: float | None = None,
    bridged_at: datetime | None = None,
    fallback_taken: str | None = None,
    summary: dict[str, Any] | None = None,
    status: TransferStatus | None = None,
) -> None:
    """Write timings / summary without moving ``transfer_status``.

    Used when the summary finishes after ``REQUESTED``, or when a fallback
    destination is dialled while still in ``DIALING_AGENT``.
    """
    now = datetime.now(UTC)
    async with factory() as session:
        current = status.value if status is not None else None
        if current is None:
            row = (
                await session.execute(
                    text(
                        "SELECT transfer_status FROM calls "
                        "WHERE id = :id AND tenant_id = :tenant_id"
                    ),
                    {"id": call_row_id, "tenant_id": tenant_id},
                )
            ).first()
            current = row[0] if row is not None else TransferStatus.NOT_REQUESTED.value
        await session.execute(
            _UPDATE_SQL,
            {
                "id": call_row_id,
                "tenant_id": tenant_id,
                "status": current,
                "destination_id": destination_id,
                "announcement_started_at": announcement_started_at,
                "agent_dialed_at": agent_dialed_at,
                "agent_answered_at": agent_answered_at,
                "whisper_seconds": whisper_seconds,
                "bridged_at": bridged_at,
                "fallback_taken": fallback_taken,
                "summary": json.dumps(summary) if summary is not None else None,
                "now": now,
            },
        )
        await session.commit()
