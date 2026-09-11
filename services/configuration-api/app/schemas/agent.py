"""Agent and agent-version schemas (spec 18, 19, 62, 63)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import TimestampedResponse
from shared.models import AgentVersionState, ResourceStatus


class AgentCreate(BaseModel):
    """Create an agent.

    An agent holds no behaviour of its own — everything the worker executes
    lives on a version, which is what makes rollback a pointer change (spec 19).
    Creating an agent therefore also creates its first draft version.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)


class AgentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    status: ResourceStatus | None = None


class AgentVersionConfig(BaseModel):
    """The configuration a worker executes (spec 18, 62).

    Every field is optional so a draft can be saved half-finished; the
    pre-publish validation in spec 63 is what insists on completeness.
    """

    model_config = ConfigDict(extra="forbid")

    language: str | None = Field(default=None, max_length=16)
    greeting: str | None = None
    system_prompt: str | None = None

    stt_provider_id: uuid.UUID | None = None
    stt_model_id: uuid.UUID | None = None
    llm_provider_id: uuid.UUID | None = None
    llm_model_id: uuid.UUID | None = None
    tts_provider_id: uuid.UUID | None = None
    tts_model_id: uuid.UUID | None = None
    voice_id: uuid.UUID | None = None

    # --- Fallback tier (spec 55) ------------------------------------------ #
    stt_fallback_provider_id: uuid.UUID | None = None
    stt_fallback_model_id: uuid.UUID | None = None
    llm_fallback_provider_id: uuid.UUID | None = None
    llm_fallback_model_id: uuid.UUID | None = None
    tts_fallback_provider_id: uuid.UUID | None = None
    tts_fallback_model_id: uuid.UUID | None = None
    tts_fallback_voice_id: uuid.UUID | None = None

    # --- Local last resort (spec 25, 55) ---------------------------------- #
    stt_local_provider_id: uuid.UUID | None = None
    stt_local_model_id: uuid.UUID | None = None
    tts_local_provider_id: uuid.UUID | None = None
    tts_local_model_id: uuid.UUID | None = None
    tts_local_voice_id: uuid.UUID | None = None

    temperature: float | None = Field(default=None, ge=0, le=2)

    # --- Conversation behaviour (spec 29) --------------------------------- #
    interruption_enabled: bool | None = None
    interruption_min_words: int | None = Field(default=None, ge=0, le=20)
    silence_timeout_seconds: int | None = Field(default=None, ge=1, le=600)
    max_call_duration_seconds: int | None = Field(default=None, ge=10, le=86400)

    # --- Capture (spec 39, 40) ------------------------------------------- #
    recording_enabled: bool | None = None
    transcription_enabled: bool | None = None

    # --- Escalation (spec 35, 36; CR-1) ---------------------------------- #
    transfer_enabled: bool | None = None
    transfer_announcement_text: str | None = None
    transfer_summary_template: str | None = None
    transfer_summary_max_seconds: int | None = Field(default=None, ge=5, le=120)
    transfer_skip_dtmf: str | None = Field(default=None, max_length=1)

    knowledge_base_id: uuid.UUID | None = None
    embedding_provider_id: uuid.UUID | None = None
    embedding_model_id: uuid.UUID | None = None
    change_note: str | None = Field(default=None, max_length=1000)


class AgentVersionResponse(TimestampedResponse):
    agent_id: uuid.UUID
    version_number: int
    state: AgentVersionState

    language: str
    greeting: str | None
    system_prompt: str

    stt_provider_id: uuid.UUID | None
    stt_model_id: uuid.UUID | None
    llm_provider_id: uuid.UUID | None
    llm_model_id: uuid.UUID | None
    tts_provider_id: uuid.UUID | None
    tts_model_id: uuid.UUID | None
    voice_id: uuid.UUID | None

    stt_fallback_provider_id: uuid.UUID | None = None
    stt_fallback_model_id: uuid.UUID | None = None
    llm_fallback_provider_id: uuid.UUID | None = None
    llm_fallback_model_id: uuid.UUID | None = None
    tts_fallback_provider_id: uuid.UUID | None = None
    tts_fallback_model_id: uuid.UUID | None = None
    tts_fallback_voice_id: uuid.UUID | None = None

    stt_local_provider_id: uuid.UUID | None = None
    stt_local_model_id: uuid.UUID | None = None
    tts_local_provider_id: uuid.UUID | None = None
    tts_local_model_id: uuid.UUID | None = None
    tts_local_voice_id: uuid.UUID | None = None

    temperature: float | None

    interruption_enabled: bool
    interruption_min_words: int
    silence_timeout_seconds: int | None
    max_call_duration_seconds: int | None

    recording_enabled: bool
    transcription_enabled: bool

    transfer_enabled: bool
    transfer_announcement_text: str | None
    transfer_summary_template: str | None
    transfer_summary_max_seconds: int
    transfer_skip_dtmf: str | None

    knowledge_base_id: uuid.UUID | None
    embedding_provider_id: uuid.UUID | None = None
    embedding_model_id: uuid.UUID | None = None

    published_at: datetime | None
    change_note: str | None
    validation_errors: list = Field(default_factory=list)

    #: Resolved names for display, so the builder can show "OpenAI / gpt-4o-mini"
    #: without a request per foreign key.
    stt_label: str | None = None
    llm_label: str | None = None
    tts_label: str | None = None
    voice_label: str | None = None

    #: Same idea for the other two tiers, so the builder can render a chain
    #: without resolving twelve more foreign keys itself.
    stt_fallback_label: str | None = None
    llm_fallback_label: str | None = None
    tts_fallback_label: str | None = None
    stt_local_label: str | None = None
    tts_local_label: str | None = None


class AgentResponse(TimestampedResponse):
    name: str
    description: str | None
    status: ResourceStatus
    published_version_id: uuid.UUID | None

    #: Summary counts for the list view.
    version_count: int = 0
    published_version_number: int | None = None
    latest_draft_version_number: int | None = None


class ValidationIssue(BaseModel):
    """One reason a version cannot be published (spec 63).

    ``field`` lets the builder highlight the control rather than only showing
    a message, which is the difference between an error a non-developer can act
    on and one they cannot.
    """

    field: str
    message: str
    severity: str = "error"


class ValidationReport(BaseModel):
    """Result of pre-publish validation (spec 63, 64)."""

    publishable: bool
    issues: list[ValidationIssue] = Field(default_factory=list)

    #: The dependency tree from spec 64, flattened for rendering: each entry is
    #: a requirement with whether it is satisfied and why not.
    dependencies: list[ValidationIssue] = Field(default_factory=list)


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    change_note: str | None = Field(default=None, max_length=1000)


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The version to publish instead. Explicit rather than "the previous one":
    #: after several rollbacks, "previous" is ambiguous.
    version_number: int = Field(ge=1)
