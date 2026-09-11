"""Call, transcript and event schemas (spec 40, 41, 42)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from shared.models import (
    CallDirection,
    CallState,
    HangupReason,
    SpeakerType,
    TransferStatus,
)


class CallResponse(BaseModel):
    """A call record (spec 41)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    call_id: str
    agent_id: uuid.UUID | None
    agent_version_id: uuid.UUID | None
    did: str | None
    room_id: str | None
    caller_number: str | None
    destination_number: str | None
    direction: CallDirection

    start_time: datetime | None
    answer_time: datetime | None
    end_time: datetime | None
    duration_seconds: int | None

    state: CallState
    hangup_reason: HangupReason | None
    failure_detail: str | None

    transfer_status: TransferStatus

    #: Resolved for display.
    agent_name: str | None = None
    agent_version_number: int | None = None
    has_transcript: bool = False
    has_recording: bool = False


class TranscriptSegmentResponse(BaseModel):
    """One utterance (spec 40)."""

    model_config = ConfigDict(from_attributes=True)

    sequence: int
    speaker: SpeakerType
    spoken_at: datetime
    text: str
    confidence: float | None
    offset_seconds: float | None

    #: True for the transfer summary whispered to a human agent. Part of the
    #: audit trail rather than the conversation, and never audible to the
    #: caller (CR-1 TR-6).
    is_private_to_agent: bool


class CallEventResponse(BaseModel):
    """A timestamped event during a call (spec 42, 58)."""

    model_config = ConfigDict(from_attributes=True)

    event_type: str
    occurred_at: datetime
    from_state: CallState | None
    to_state: CallState | None
    payload: dict = Field(default_factory=dict)


class CallDetailResponse(CallResponse):
    """A call with its transcript and event history.

    Returned together because the question being asked of this screen is "what
    happened on this call", and answering it from three separate requests makes
    the timeline harder to assemble than it needs to be.
    """

    segments: list[TranscriptSegmentResponse] = Field(default_factory=list)
    events: list[CallEventResponse] = Field(default_factory=list)
    summary: str | None = None
