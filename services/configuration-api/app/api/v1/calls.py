"""Call history endpoints (spec 41, 40, 67).

Read-only. A call record is produced by the worker as the call happens; the
Control Plane never edits one, because an editable call history is not a
record.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from app.core.dependencies import CurrentTenant, require_permission
from app.db.models import (
    Agent,
    AgentVersion,
    Call,
    CallEvent,
    CallRecording,
    CallTranscript,
    CallTranscriptSegment,
)
from app.db.repository import TenantRepository
from app.db.util import as_lookup
from app.schemas.call import (
    CallDetailResponse,
    CallEventResponse,
    CallResponse,
    TranscriptSegmentResponse,
)
from app.schemas.common import Page
from shared.logging import get_logger
from shared.models import CallState, Permission

logger = get_logger(__name__)

router = APIRouter(prefix="/calls", tags=["calls"])


@router.get(
    "",
    response_model=Page[CallResponse],
    summary="List calls, most recent first",
    dependencies=[Depends(require_permission(Permission.CALLS_READ))],
)
async def list_calls(
    tenant: CurrentTenant,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    state: Annotated[CallState | None, Query(description="Filter by call state")] = None,
    agent_id: Annotated[uuid.UUID | None, Query()] = None,
) -> Page[CallResponse]:
    """A page of calls.

    Ordered by start time descending, which matches the index on
    (tenant_id, start_time) — the ordering the call list almost always wants.
    """
    repository = TenantRepository(tenant.session, tenant.tenant_id)

    statement = repository.scoped(Call)
    if state is not None:
        statement = statement.where(Call.state == state)
    if agent_id is not None:
        statement = statement.where(Call.agent_id == agent_id)

    rows = list(
        (
            await tenant.session.execute(
                statement.order_by(Call.start_time.desc().nullslast()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )

    from sqlalchemy import func

    count_statement = (
        select(func.count()).select_from(Call).where(Call.tenant_id == tenant.tenant_id)
    )
    if state is not None:
        count_statement = count_statement.where(Call.state == state)
    if agent_id is not None:
        count_statement = count_statement.where(Call.agent_id == agent_id)
    total = int((await tenant.session.execute(count_statement)).scalar_one())

    return Page(items=await _decorate(tenant, rows), total=total, limit=limit, offset=offset)


async def _decorate(tenant: CurrentTenant, rows: list[Call]) -> list[CallResponse]:
    """Attach agent names and artefact flags.

    The flags are separate existence queries rather than joins because a call
    row carries denormalised `recording_id` and `transcript_id` pointers that
    are only set once the artefact lands — checking the tables is what tells
    the truth while a call is still in progress.
    """
    agent_ids = {row.agent_id for row in rows if row.agent_id}
    version_ids = {row.agent_version_id for row in rows if row.agent_version_id}
    call_ids = [row.id for row in rows]

    agent_names: dict[uuid.UUID, str] = {}
    if agent_ids:
        agent_names = as_lookup(
            (
                await tenant.session.execute(
                    select(Agent.id, Agent.name).where(
                        Agent.tenant_id == tenant.tenant_id, Agent.id.in_(agent_ids)
                    )
                )
            ).all()
        )

    version_numbers: dict[uuid.UUID, int] = {}
    if version_ids:
        version_numbers = as_lookup(
            (
                await tenant.session.execute(
                    select(AgentVersion.id, AgentVersion.version_number).where(
                        AgentVersion.tenant_id == tenant.tenant_id,
                        AgentVersion.id.in_(version_ids),
                    )
                )
            ).all()
        )

    with_transcript: set[uuid.UUID] = set()
    with_recording: set[uuid.UUID] = set()
    if call_ids:
        with_transcript = set(
            (
                await tenant.session.execute(
                    select(CallTranscript.call_id).where(
                        CallTranscript.tenant_id == tenant.tenant_id,
                        CallTranscript.call_id.in_(call_ids),
                        CallTranscript.segment_count > 0,
                    )
                )
            )
            .scalars()
            .all()
        )
        with_recording = set(
            (
                await tenant.session.execute(
                    select(CallRecording.call_id).where(
                        CallRecording.tenant_id == tenant.tenant_id,
                        CallRecording.call_id.in_(call_ids),
                    )
                )
            )
            .scalars()
            .all()
        )

    return [
        CallResponse.model_validate(row, from_attributes=True).model_copy(
            update={
                "agent_name": agent_names.get(row.agent_id) if row.agent_id else None,
                "agent_version_number": version_numbers.get(row.agent_version_id)
                if row.agent_version_id
                else None,
                "has_transcript": row.id in with_transcript,
                "has_recording": row.id in with_recording,
            }
        )
        for row in rows
    ]


@router.get(
    "/{call_id}",
    response_model=CallDetailResponse,
    summary="Fetch one call with its transcript and events",
    dependencies=[Depends(require_permission(Permission.CALLS_READ))],
)
async def get_call(call_id: uuid.UUID, tenant: CurrentTenant) -> CallDetailResponse:
    """One call, with everything needed to reconstruct what happened.

    The transcript and the event history come back together because the
    question this screen answers is "what happened on this call", and
    assembling that timeline from three requests is harder than it needs to be.
    """
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(Call, call_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no call with id {call_id}"
        )

    base = (await _decorate(tenant, [row]))[0]

    transcript = (
        await tenant.session.execute(
            repository.scoped(CallTranscript).where(CallTranscript.call_id == call_id)
        )
    ).scalar_one_or_none()

    segments: list[TranscriptSegmentResponse] = []
    if transcript is not None:
        rows = (
            (
                await tenant.session.execute(
                    repository.scoped(CallTranscriptSegment)
                    .where(CallTranscriptSegment.call_transcript_id == transcript.id)
                    .order_by(CallTranscriptSegment.sequence)
                )
            )
            .scalars()
            .all()
        )
        segments = [
            TranscriptSegmentResponse.model_validate(segment, from_attributes=True)
            for segment in rows
        ]

    event_rows = (
        (
            await tenant.session.execute(
                repository.scoped(CallEvent)
                .where(CallEvent.call_id == call_id)
                .order_by(CallEvent.occurred_at)
            )
        )
        .scalars()
        .all()
    )

    return CallDetailResponse(
        **base.model_dump(),
        segments=segments,
        events=[
            CallEventResponse.model_validate(event, from_attributes=True) for event in event_rows
        ],
        summary=transcript.summary if transcript else None,
    )
