"""Calls, events, transcripts and recordings.

Spec 41 (call database), 42 (state machine), 43 (correlation ID), 39
(recording), 40 (transcription), plus the warm-transfer trail from CR-1.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
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
from shared.models import (
    CallDirection,
    CallState,
    HangupReason,
    SpeakerType,
    TransferStatus,
)


class Call(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin):
    """One call (spec 41).

    ``agent_version_id`` is recorded alongside ``agent_id`` because a call is
    executed by a specific published version, and publishing a new one must
    not retroactively change what this call did (spec 19, 45).
    """

    __tablename__ = "calls"
    __table_args__ = (
        # The correlation ID is unique platform-wide, since it is the key used
        # to join logs, recordings and transcripts across every service
        # (spec 43).
        UniqueConstraint("call_id", name="uq_calls_call_id"),
        CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds >= 0",
            name="duration_non_negative",
        ),
        CheckConstraint(
            "answer_time IS NULL OR start_time IS NULL OR answer_time >= start_time",
            name="answer_after_start",
        ),
        CheckConstraint(
            "end_time IS NULL OR start_time IS NULL OR end_time >= start_time",
            name="end_after_start",
        ),
        # The call list is almost always "this tenant, most recent first".
        Index("ix_calls_tenant_started", "tenant_id", "start_time"),
        # Concurrency enforcement counts a tenant's non-terminal calls before
        # accepting a new one (spec 47), which is this exact lookup.
        Index("ix_calls_tenant_state", "tenant_id", "state"),
    )

    #: Human-readable correlation ID, propagated through PBX, SIP, LiveKit,
    #: agent, STT, LLM, TTS, logs, recording and transcript (spec 43).
    #: Distinct from the row's UUID primary key so it can appear in a log line
    #: or a support ticket without being mistaken for an internal handle.
    call_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    agent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("agent_versions.id", ondelete="SET NULL"),
        index=True,
    )
    pbx_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("pbxs.id", ondelete="SET NULL")
    )
    sip_trunk_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("sip_trunks.id", ondelete="SET NULL")
    )
    routing_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("routing_rules.id", ondelete="SET NULL")
    )

    #: Dialled number as an E.164 string rather than a phone_numbers FK: the
    #: call record must survive the DID being deleted or reassigned.
    did: Mapped[str | None] = mapped_column(String(32), index=True)

    #: LiveKit room. Unique per call under the INDIVIDUAL strategy (spec 21).
    room_id: Mapped[str | None] = mapped_column(String(255), index=True)

    #: LiveKit's own call identifier, kept so a call can be found from the
    #: LiveKit side during an incident.
    livekit_call_id: Mapped[str | None] = mapped_column(String(255), index=True)

    caller_number: Mapped[str | None] = mapped_column(String(64), index=True)
    destination_number: Mapped[str | None] = mapped_column(String(64))
    direction: Mapped[CallDirection] = enum_column(
        CallDirection, nullable=False, default=CallDirection.INBOUND
    )

    # --- Timing (spec 41) -------------------------------------------------- #
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    answer_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Stored rather than derived, because billing and analytics read it
    #: constantly and a call in flight has no end time to subtract from.
    duration_seconds: Mapped[int | None] = mapped_column(Integer)

    # --- Outcome (spec 41, 42) -------------------------------------------- #
    state: Mapped[CallState] = enum_column(
        CallState, nullable=False, default=CallState.NEW, index=True
    )
    hangup_reason: Mapped[HangupReason | None] = enum_column(HangupReason)

    #: Set when a call fails, so an operator does not have to reconstruct the
    #: cause from logs.
    failure_detail: Mapped[str | None] = mapped_column(Text)

    # --- Warm transfer (spec 35, 36; CR-1) -------------------------------- #
    transfer_status: Mapped[TransferStatus] = enum_column(
        TransferStatus, nullable=False, default=TransferStatus.NOT_REQUESTED, index=True
    )
    transfer_destination_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("transfer_destinations.id", ondelete="SET NULL")
    )

    #: Per-stage timings, so "the transfer felt slow" becomes investigable:
    #: when the caller announcement started, when the agent answered, how long
    #: the summary whisper ran, when the legs were bridged (CR-1).
    transfer_announcement_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    transfer_agent_dialed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    transfer_agent_answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    transfer_whisper_seconds: Mapped[float | None] = mapped_column(Float)
    transfer_bridged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Which fallback branch ran when the human agent could not take the call
    #: (spec 38, CR-1 TR-10).
    transfer_fallback_taken: Mapped[str | None] = mapped_column(String(64))

    #: The generated summary (spec 36). Kept on the call so it is available
    #: after the fact even when the whisper failed to play.
    transfer_summary: Mapped[dict] = json_column(nullable=False, default=dict)

    # --- Artefacts --------------------------------------------------------- #
    #: Denormalised pointers. Convenient for the call detail view, and null
    #: until the artefact exists.
    recording_id: Mapped[uuid.UUID | None] = mapped_column(postgresql.UUID(as_uuid=True))
    transcript_id: Mapped[uuid.UUID | None] = mapped_column(postgresql.UUID(as_uuid=True))

    #: Which worker handled the call, for correlating a bad call with a bad
    #: pod during an incident.
    worker_id: Mapped[str | None] = mapped_column(String(128))

    def __repr__(self) -> str:
        return f"<Call {self.call_id} {self.state}>"


class CallEvent(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin):
    """A timestamped event during a call (spec 42, 58).

    The append-only history behind the state machine. Without it, a completed
    call shows only its final state and there is no way to tell whether it
    reached the agent, how long each stage took, or where it went wrong.
    """

    __tablename__ = "call_events"
    __table_args__ = (Index("ix_call_events_call_occurred", "call_id", "occurred_at"),)

    call_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("calls.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: Matches the ``event`` field in structured logs (spec 58), so a log line
    #: and a database row describe the same thing by the same name.
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    #: Populated for state transitions, null for other events.
    from_state: Mapped[CallState | None] = enum_column(CallState)
    to_state: Mapped[CallState | None] = enum_column(CallState)

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: Event-specific detail: provider name, latency, error, DTMF digit.
    payload: Mapped[dict] = json_column(nullable=False, default=dict)

    def __repr__(self) -> str:
        return f"<CallEvent {self.event_type}>"


class CallTranscript(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin):
    """Transcript header for one call (spec 40)."""

    __tablename__ = "call_transcripts"
    __table_args__ = (UniqueConstraint("call_id", name="uq_call_transcripts_call_id"),)

    call_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("calls.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    language: Mapped[str | None] = mapped_column(String(16))
    segment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Conversation summary (spec 34). Also the source for the transfer
    #: whisper when one is needed (CR-1).
    summary: Mapped[str | None] = mapped_column(Text)

    #: Full plain-text rendering, for search and export. The segments below
    #: remain the authoritative record.
    full_text: Mapped[str | None] = mapped_column(Text)


class CallTranscriptSegment(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin):
    """One utterance (spec 40).

    Speaker types are Caller, AI and Human Agent — the last covering the
    post-bridge portion of a warm transfer, so a single transcript spans the
    whole call across both the AI and human segments (CR-1).
    """

    __tablename__ = "call_transcript_segments"
    __table_args__ = (
        UniqueConstraint(
            "call_transcript_id", "sequence", name="uq_call_transcript_segments_transcript_sequence"
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence_in_range",
        ),
        Index("ix_call_transcript_segments_transcript_seq", "call_transcript_id", "sequence"),
    )

    call_transcript_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("call_transcripts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker: Mapped[SpeakerType] = enum_column(SpeakerType, nullable=False, index=True)
    spoken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)

    #: Offset from call start, so a segment can be located in the recording
    #: without comparing wall-clock timestamps across services.
    offset_seconds: Mapped[float | None] = mapped_column(Float)

    #: True for the transfer summary whispered to the human agent. Part of the
    #: audit trail, not part of the conversation, and never audible to the
    #: caller (CR-1 TR-6).
    is_private_to_agent: Mapped[bool] = mapped_column(
        postgresql.BOOLEAN, nullable=False, default=False
    )


class CallRecording(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin):
    """Recording metadata (spec 39).

    Metadata only. The audio lives in object storage — spec 3 and 39 both
    forbid putting recordings in PostgreSQL.
    """

    __tablename__ = "call_recordings"
    __table_args__ = (
        CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds >= 0",
            name="duration_non_negative",
        ),
    )

    call_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("calls.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: Bucket and key in object storage.
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)

    content_type: Mapped[str | None] = mapped_column(String(64))
    byte_size: Mapped[int | None] = mapped_column(postgresql.BIGINT)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)

    #: LiveKit egress job, for chasing a recording that never landed.
    livekit_egress_id: Mapped[str | None] = mapped_column(String(255), index=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: When retention deletes this object. Null means retention is not yet
    #: configured, which is a decision the tenant must make rather than a
    #: default the platform should assume.
    delete_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    def __repr__(self) -> str:
        return f"<CallRecording {self.object_key}>"
