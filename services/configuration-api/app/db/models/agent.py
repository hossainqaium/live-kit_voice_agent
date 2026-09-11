"""AI agents and their versions.

Spec 18 (agent management), 19 (versioning), 62 (agent builder), 63 (validation).

The split matters: ``agents`` is stable identity, ``agent_versions`` is an
immutable configuration snapshot. A call records both, so publishing a new
version cannot change a conversation already in progress (spec 19, 45).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
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
from shared.models import AgentVersionState, ResourceStatus


class Agent(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin):
    """A named AI voice agent (spec 18).

    Holds no behaviour of its own. Everything the worker executes lives on a
    version, which is what makes rollback a pointer change rather than a
    data migration.
    """

    __tablename__ = "agents"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_agents_tenant_name"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    #: The version new calls use. Null means nothing is published yet, so the
    #: agent cannot answer — which is the correct state for a draft, and is
    #: why routing must check this rather than assume a version exists.
    published_version_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        # No FK: agent_versions references agents, and a mutual FK would make
        # both inserts impossible without deferred constraints. Integrity is
        # enforced by the publish operation, which is the only writer.
        index=True,
    )

    status: Mapped[ResourceStatus] = enum_column(
        ResourceStatus, nullable=False, default=ResourceStatus.ACTIVE, index=True
    )

    def __repr__(self) -> str:
        return f"<Agent {self.name}>"


class AgentVersion(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin):
    """An immutable snapshot of an agent's configuration (spec 18, 19, 62).

    Once published, a version's fields must not change. A running call holds
    this row's ID and reloads nothing, so editing a published version in place
    would alter behaviour mid-conversation (spec 45).
    """

    __tablename__ = "agent_versions"
    __table_args__ = (
        UniqueConstraint("agent_id", "version_number", name="uq_agent_versions_agent_version"),
        CheckConstraint("version_number >= 1", name="version_number_positive"),
        CheckConstraint(
            "temperature IS NULL OR (temperature >= 0 AND temperature <= 2)",
            name="temperature_in_range",
        ),
        CheckConstraint(
            "max_call_duration_seconds IS NULL OR max_call_duration_seconds > 0",
            name="max_call_duration_positive",
        ),
        CheckConstraint(
            "silence_timeout_seconds IS NULL OR silence_timeout_seconds > 0",
            name="silence_timeout_positive",
        ),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[AgentVersionState] = enum_column(
        AgentVersionState, nullable=False, default=AgentVersionState.DRAFT, index=True
    )

    # --- Identity and conversation (spec 18) ------------------------------ #
    language: Mapped[str] = mapped_column(String(16), nullable=False, default="en")
    greeting: Mapped[str | None] = mapped_column(Text)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # --- Speech to text --------------------------------------------------- #
    stt_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("providers.id", ondelete="RESTRICT")
    )
    stt_model_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("models.id", ondelete="RESTRICT")
    )

    # --- Language model --------------------------------------------------- #
    llm_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("providers.id", ondelete="RESTRICT")
    )
    llm_model_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("models.id", ondelete="RESTRICT")
    )
    temperature: Mapped[float | None] = mapped_column(Float)

    # --- Text to speech --------------------------------------------------- #
    tts_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("providers.id", ondelete="RESTRICT")
    )
    tts_model_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("models.id", ondelete="RESTRICT")
    )
    voice_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("voices.id", ondelete="RESTRICT")
    )

    # --- Provider fallback chain (spec 55) -------------------------------- #
    #: §55 requires a fallback provider for every AI provider. It is modelled
    #: as two further tiers rather than an ordered child table because a
    #: version is an immutable snapshot copied on every edit, and copying a
    #: child collection is where that guarantee usually breaks.
    #:
    #: The tiers are tried in order: primary, then fallback, then local. Each
    #: is optional, and a version with only a primary behaves exactly as
    #: before — which is what keeps this change backward compatible with every
    #: version already published.
    stt_fallback_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("providers.id", ondelete="RESTRICT")
    )
    stt_fallback_model_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("models.id", ondelete="RESTRICT")
    )

    llm_fallback_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("providers.id", ondelete="RESTRICT")
    )
    llm_fallback_model_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("models.id", ondelete="RESTRICT")
    )

    tts_fallback_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("providers.id", ondelete="RESTRICT")
    )
    tts_fallback_model_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("models.id", ondelete="RESTRICT")
    )
    tts_fallback_voice_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("voices.id", ondelete="RESTRICT")
    )

    # --- Local last resort (spec 25, 55) ---------------------------------- #
    #: A self-hosted endpoint kept as the final tier, so a call still has a
    #: voice when every hosted provider is unreachable. Deliberately separate
    #: from the fallback tier: an operator choosing "one more cloud vendor" and
    #: an operator choosing "something on our own network" are making different
    #: decisions about what failure they are insuring against, and collapsing
    #: the two into an anonymous ordered list hides that.
    #:
    #: LLM has no local tier. A self-hosted language model is a deployment
    #: decision with its own hardware, not a fallback a tenant can switch on,
    #: and offering the control without that would be a promise the platform
    #: cannot keep.
    stt_local_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("providers.id", ondelete="RESTRICT")
    )
    stt_local_model_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("models.id", ondelete="RESTRICT")
    )

    tts_local_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("providers.id", ondelete="RESTRICT")
    )
    tts_local_model_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("models.id", ondelete="RESTRICT")
    )
    tts_local_voice_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("voices.id", ondelete="RESTRICT")
    )

    # --- Conversation behaviour (spec 18, 29) ----------------------------- #
    interruption_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: How much caller speech counts as a real interruption rather than a
    #: cough. Too low and the agent stops constantly; too high and barge-in
    #: feels unresponsive (spec 29).
    interruption_min_words: Mapped[int] = mapped_column(Integer, nullable=False, default=2)

    silence_timeout_seconds: Mapped[int | None] = mapped_column(Integer, default=10)
    max_call_duration_seconds: Mapped[int | None] = mapped_column(Integer, default=1800)

    # --- Capture (spec 39, 40) -------------------------------------------- #
    recording_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    transcription_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # --- Escalation (spec 35, 36; clarification CR-1) --------------------- #
    transfer_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: What the caller hears when a transfer starts. Rendered with the agent's
    #: own TTS voice and language (CR-1 TR-2, TR-3).
    transfer_announcement_text: Mapped[str | None] = mapped_column(
        Text, default="Your call is being transferred to a human agent. Please wait."
    )

    #: Audio that continues while the human agent is being reached. Silence
    #: here reads as a dropped call to the caller (CR-1 TR-4).
    hold_media_object_key: Mapped[str | None] = mapped_column(String(512))

    #: Template for the summary whispered to the human agent before the legs
    #: are bridged. Substitution variables are the seven spec-36 fields
    #: (CR-1 TS-1).
    transfer_summary_template: Mapped[str | None] = mapped_column(Text)

    #: Caps how long the caller waits while the summary plays (CR-1 TR-12).
    transfer_summary_max_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30)

    #: Lets the human agent skip the whisper and take the call immediately
    #: (CR-1 TR-9).
    transfer_skip_dtmf: Mapped[str | None] = mapped_column(String(1), default="1")

    # --- Business rules --------------------------------------------------- #
    #: Free-form tenant rules the prompt builder injects. JSONB rather than a
    #: table because the shape is tenant-defined and never queried by the
    #: platform.
    business_rules: Mapped[dict] = json_column(nullable=False, default=dict)

    knowledge_base_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="SET NULL")
    )

    # --- Publication trail (spec 19, 69) ---------------------------------- #
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    change_note: Mapped[str | None] = mapped_column(Text)

    #: Outcome of the last pre-publish validation (spec 63). Stored so the UI
    #: can show why publishing is blocked without re-running every check.
    validation_errors: Mapped[list] = json_column(nullable=False, default=list)

    def __repr__(self) -> str:
        return f"<AgentVersion v{self.version_number} {self.state}>"


class AgentTool(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin):
    """Which tools an agent version may call (spec 30, 32).

    An explicit allow-list. Absence of a row is a denial, so a tool added to
    the tenant's library is not silently available to every agent.
    """

    __tablename__ = "agent_tools"
    __table_args__ = (
        UniqueConstraint("agent_version_id", "tool_id", name="uq_agent_tools_version_tool"),
    )

    agent_version_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("agent_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tool_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("tools.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AgentVoice(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin):
    """Additional voices available to an agent version (spec 68 ``agent_voices``).

    The primary voice is ``agent_versions.voice_id``. This table covers the
    multi-voice case — a different voice per language, or a distinct voice for
    the transfer announcement.
    """

    __tablename__ = "agent_voices"
    __table_args__ = (
        UniqueConstraint("agent_version_id", "voice_id", name="uq_agent_voices_version_voice"),
    )

    agent_version_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("agent_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    voice_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("voices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: What this voice is for, e.g. "primary", "announcement", "es".
    purpose: Mapped[str] = mapped_column(String(64), nullable=False, default="primary")
