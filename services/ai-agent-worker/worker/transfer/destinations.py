"""Transfer destinations frozen onto a call (spec 35, 45)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import FallbackAction, TransferDestinationKind

_DESTINATIONS_SQL = text(
    """
    SELECT
        d.id,
        d.name,
        d.kind,
        d.target,
        d.pbx_id,
        d.sip_trunk_id,
        d.whisper_summary,
        d.ring_timeout_seconds,
        COALESCE(dst.livekit_resource_id, call_st.livekit_resource_id) AS livekit_trunk_id
    FROM transfer_destinations d
    LEFT JOIN sip_trunks dst
           ON dst.id = d.sip_trunk_id AND dst.tenant_id = d.tenant_id
    LEFT JOIN sip_trunks call_st
           ON call_st.id = CAST(:call_sip_trunk_id AS uuid)
          AND call_st.tenant_id = d.tenant_id
    WHERE d.tenant_id = :tenant_id
      AND d.status = 'ACTIVE'
    ORDER BY d.name ASC
    """
)


@dataclass(frozen=True, slots=True)
class TransferDestinationInfo:
    """One place a call can be handed to a human."""

    id: uuid.UUID
    name: str
    kind: TransferDestinationKind
    target: str
    pbx_id: uuid.UUID | None
    sip_trunk_id: uuid.UUID | None
    livekit_trunk_id: str | None
    whisper_summary: bool
    ring_timeout_seconds: int


@dataclass(frozen=True, slots=True)
class TransferFallback:
    """Where the caller goes when the human leg fails (spec 38, TR-10)."""

    action: FallbackAction | None
    agent_id: uuid.UUID | None
    destination_id: uuid.UUID | None


async def load_destinations(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    call_sip_trunk_id: uuid.UUID | None,
) -> tuple[TransferDestinationInfo, ...]:
    """Load the tenant's active destinations once, at call start (spec 45)."""
    rows = (
        await session.execute(
            _DESTINATIONS_SQL,
            {"tenant_id": tenant_id, "call_sip_trunk_id": call_sip_trunk_id},
        )
    ).mappings().all()
    loaded: list[TransferDestinationInfo] = []
    for row in rows:
        try:
            kind = TransferDestinationKind(row["kind"])
        except ValueError:
            continue
        loaded.append(
            TransferDestinationInfo(
                id=row["id"],
                name=row["name"],
                kind=kind,
                target=row["target"],
                pbx_id=row["pbx_id"],
                sip_trunk_id=row["sip_trunk_id"],
                livekit_trunk_id=row["livekit_trunk_id"],
                whisper_summary=bool(row["whisper_summary"]),
                ring_timeout_seconds=int(row["ring_timeout_seconds"] or 30),
            )
        )
    return tuple(loaded)


def resolve_destination(
    destinations: tuple[TransferDestinationInfo, ...],
    *,
    name: str | None = None,
    destination_id: uuid.UUID | None = None,
) -> TransferDestinationInfo | None:
    """Pick a destination by id, then name, then the first active row."""
    if destination_id is not None:
        for dest in destinations:
            if dest.id == destination_id:
                return dest
    if name:
        wanted = name.strip().lower()
        for dest in destinations:
            if dest.name.lower() == wanted:
                return dest
        for dest in destinations:
            if wanted in dest.name.lower() or wanted in dest.target.lower():
                return dest
    return destinations[0] if destinations else None
