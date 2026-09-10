"""AI provider interfaces (spec 24).

Three interfaces, one rule: **provider-specific code stays inside provider
adapters**. Nothing above this layer may branch on which vendor is configured.

The realtime pipeline is built on LiveKit Agents, which has its own component
types, so each adapter has two responsibilities:

1. Implement the platform interface below, which is what tests and non-realtime
   callers use, and what keeps the contract vendor-neutral.
2. Build the corresponding LiveKit Agents component via ``build_livekit_component``
   so the streaming voice pipeline can consume it natively (spec 28).

Doing only (2) would leak vendor types into the pipeline the moment anything
needed a non-streaming transcription; doing only (1) would force audio through
an extra hop and cost latency the spec-56 budget cannot spare.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from shared.models import ProviderKind

# --------------------------------------------------------------------------- #
# Configuration passed to an adapter at call start
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Resolved provider configuration for one call.

    Built by the config loader from the agent version record plus the tenant's
    decrypted credential. Frozen because a call must use one consistent
    configuration for its whole lifetime (spec 45).
    """

    kind: ProviderKind
    provider: str
    model: str | None = None
    #: Decrypted at call start and never logged (spec 54).
    api_key: str | None = None
    base_url: str | None = None
    language: str | None = None
    #: TTS only.
    voice_id: str | None = None
    #: LLM only.
    temperature: float | None = None
    #: Provider-specific extras that do not deserve a first-class field.
    options: dict[str, Any] = field(default_factory=dict)

    def redacted(self) -> dict[str, Any]:
        """Log-safe view with the credential removed."""
        return {
            "kind": str(self.kind),
            "provider": self.provider,
            "model": self.model,
            "language": self.language,
            "voice_id": self.voice_id,
            "has_credential": self.api_key is not None,
        }


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    is_final: bool
    confidence: float | None = None
    language: str | None = None


@dataclass(frozen=True, slots=True)
class Message:
    """One turn of conversation."""

    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    tool_call_id: str | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Completion:
    """An LLM response chunk or its final aggregate."""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finished: bool = False


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """Synthesised audio."""

    data: bytes
    sample_rate: int
    num_channels: int = 1


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ProviderError(RuntimeError):
    """Base class for provider failures.

    Carries whether a retry could plausibly succeed, which is what the
    resilience layer needs to decide between retrying and failing over
    (spec 55).
    """

    def __init__(self, message: str, *, provider: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable


class ProviderTimeoutError(ProviderError):
    def __init__(self, message: str, *, provider: str) -> None:
        super().__init__(message, provider=provider, retryable=True)


class ProviderRateLimitError(ProviderError):
    def __init__(self, message: str, *, provider: str, retry_after: float | None = None) -> None:
        super().__init__(message, provider=provider, retryable=True)
        self.retry_after = retry_after


class ProviderAuthError(ProviderError):
    """Bad or missing credential. Retrying cannot fix this."""

    def __init__(self, message: str, *, provider: str) -> None:
        super().__init__(message, provider=provider, retryable=False)


class ProviderUnavailableError(ProviderError):
    """Provider is reachable but refusing work; fail over rather than retry."""

    def __init__(self, message: str, *, provider: str) -> None:
        super().__init__(message, provider=provider, retryable=False)


# --------------------------------------------------------------------------- #
# Interfaces
# --------------------------------------------------------------------------- #


class BaseProvider(abc.ABC):
    """Common shape for every adapter."""

    kind: ProviderKind

    def __init__(self, config: ProviderConfig) -> None:
        if config.kind is not self.kind:
            raise ValueError(
                f"{type(self).__name__} expects {self.kind} configuration, got {config.kind}"
            )
        self.config = config

    @property
    def name(self) -> str:
        return self.config.provider

    @abc.abstractmethod
    def build_livekit_component(self) -> Any:
        """Return the LiveKit Agents component for the streaming pipeline."""

    async def aclose(self) -> None:
        """Release adapter resources. Overridden where a client is held open."""
        return None


class STTProvider(BaseProvider):
    """Speech to text."""

    kind = ProviderKind.STT

    @abc.abstractmethod
    async def transcribe(
        self, audio: AsyncIterator[bytes], *, sample_rate: int = 16000
    ) -> AsyncIterator[Transcript]:
        """Transcribe streaming audio, yielding interim and final transcripts."""


class LLMProvider(BaseProvider):
    """Language model."""

    kind = ProviderKind.LLM

    @abc.abstractmethod
    async def generate(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[Completion]:
        """Stream a completion, emitting text deltas and any tool calls."""


class TTSProvider(BaseProvider):
    """Text to speech."""

    kind = ProviderKind.TTS

    @abc.abstractmethod
    async def synthesize(
        self, text: str | AsyncIterator[str], *, voice_id: str | None = None
    ) -> AsyncIterator[AudioChunk]:
        """Stream synthesised audio.

        Accepting an async iterator of text lets an adapter begin synthesis
        from the first LLM tokens instead of waiting for a finished sentence,
        which is most of the time-to-first-audio budget (spec 56).
        """

    @property
    def supports_streaming(self) -> bool:
        """Whether this adapter streams audio as it is generated (spec 28)."""
        return True
