"""Tests for the provider abstraction (spec 24).

These assert the *contract*, not any vendor's behaviour. The point of the
abstraction is that nothing above the adapter layer can tell which provider is
configured, so these tests must pass for every adapter added later.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from shared.models import ProviderKind
from worker.providers import (
    AudioChunk,
    Completion,
    LLMProvider,
    Message,
    ProviderAuthError,
    ProviderConfig,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    STTProvider,
    Transcript,
    TTSProvider,
)


class FakeSTT(STTProvider):
    def build_livekit_component(self) -> Any:
        return object()

    async def transcribe(
        self, audio: AsyncIterator[bytes], *, sample_rate: int = 16000
    ) -> AsyncIterator[Transcript]:
        async for _ in audio:
            yield Transcript(text="hello", is_final=True, confidence=0.97)


class FakeLLM(LLMProvider):
    def build_livekit_component(self) -> Any:
        return object()

    async def generate(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[Completion]:
        yield Completion(text="hi ")
        yield Completion(text="there", finished=True)


class FakeTTS(TTSProvider):
    def build_livekit_component(self) -> Any:
        return object()

    async def synthesize(
        self, text: str | AsyncIterator[str], *, voice_id: str | None = None
    ) -> AsyncIterator[AudioChunk]:
        yield AudioChunk(data=b"\x00\x01", sample_rate=24000)


class TestProviderConfig:
    def test_credential_is_absent_from_the_log_safe_view(self) -> None:
        """Spec 54: credentials must never reach the logs."""
        config = ProviderConfig(
            kind=ProviderKind.TTS,
            provider="elevenlabs",
            api_key="sk-super-secret",
            voice_id="voice-1",
        )
        redacted = config.redacted()
        assert "sk-super-secret" not in str(redacted)
        assert redacted["has_credential"] is True
        assert redacted["voice_id"] == "voice-1"

    def test_missing_credential_is_reported_honestly(self) -> None:
        config = ProviderConfig(kind=ProviderKind.LLM, provider="openai")
        assert config.redacted()["has_credential"] is False

    def test_config_is_immutable(self) -> None:
        """A call must use one consistent configuration throughout (spec 45)."""
        config = ProviderConfig(kind=ProviderKind.STT, provider="deepgram")
        with pytest.raises((AttributeError, TypeError)):
            config.provider = "whisper"  # type: ignore[misc]


class TestKindEnforcement:
    def test_adapter_rejects_configuration_for_another_kind(self) -> None:
        """A TTS config reaching an STT adapter is a wiring bug, not a fallback."""
        with pytest.raises(ValueError, match="expects"):
            FakeSTT(ProviderConfig(kind=ProviderKind.TTS, provider="elevenlabs"))

    @pytest.mark.parametrize(
        ("adapter", "kind"),
        [(FakeSTT, ProviderKind.STT), (FakeLLM, ProviderKind.LLM), (FakeTTS, ProviderKind.TTS)],
    )
    def test_adapter_accepts_its_own_kind(self, adapter: type, kind: ProviderKind) -> None:
        instance = adapter(ProviderConfig(kind=kind, provider="fake"))
        assert instance.kind is kind
        assert instance.name == "fake"


class TestInterfaces:
    @pytest.mark.asyncio
    async def test_stt_streams_transcripts(self) -> None:
        async def audio() -> AsyncIterator[bytes]:
            yield b"frame"

        stt = FakeSTT(ProviderConfig(kind=ProviderKind.STT, provider="fake"))
        results = [t async for t in stt.transcribe(audio())]
        assert results[0].text == "hello"
        assert results[0].is_final

    @pytest.mark.asyncio
    async def test_llm_streams_deltas_and_signals_completion(self) -> None:
        llm = FakeLLM(ProviderConfig(kind=ProviderKind.LLM, provider="fake"))
        chunks = [c async for c in llm.generate([Message(role="user", content="hi")])]
        assert "".join(c.text for c in chunks) == "hi there"
        assert chunks[-1].finished

    @pytest.mark.asyncio
    async def test_tts_streams_audio(self) -> None:
        tts = FakeTTS(ProviderConfig(kind=ProviderKind.TTS, provider="fake"))
        chunks = [c async for c in tts.synthesize("hello")]
        assert chunks[0].sample_rate == 24000

    def test_every_adapter_exposes_a_livekit_component(self) -> None:
        """The streaming pipeline consumes LiveKit components directly (spec 28)."""
        for adapter, kind in (
            (FakeSTT, ProviderKind.STT),
            (FakeLLM, ProviderKind.LLM),
            (FakeTTS, ProviderKind.TTS),
        ):
            instance = adapter(ProviderConfig(kind=kind, provider="fake"))
            assert instance.build_livekit_component() is not None

    def test_abstract_methods_cannot_be_skipped(self) -> None:
        class Incomplete(STTProvider):
            def build_livekit_component(self) -> Any:
                return None

        with pytest.raises(TypeError):
            Incomplete(ProviderConfig(kind=ProviderKind.STT, provider="x"))  # type: ignore[abstract]


class TestErrorClassification:
    """The resilience layer decides retry vs failover from these flags (spec 55)."""

    @pytest.mark.parametrize(
        ("error", "retryable"),
        [
            (ProviderTimeoutError("slow", provider="p"), True),
            (ProviderRateLimitError("429", provider="p"), True),
            (ProviderAuthError("bad key", provider="p"), False),
            (ProviderUnavailableError("503", provider="p"), False),
        ],
    )
    def test_retryability_is_explicit(self, error: ProviderError, retryable: bool) -> None:
        assert error.retryable is retryable

    def test_auth_errors_are_never_retried(self) -> None:
        """Retrying a bad credential just burns the latency budget."""
        assert ProviderAuthError("bad key", provider="openai").retryable is False

    def test_rate_limit_carries_retry_after(self) -> None:
        error = ProviderRateLimitError("429", provider="deepgram", retry_after=2.5)
        assert error.retry_after == 2.5

    def test_every_error_names_its_provider(self) -> None:
        """Without this, a failover log line cannot say what failed."""
        for error in (
            ProviderTimeoutError("t", provider="a"),
            ProviderRateLimitError("r", provider="b"),
            ProviderAuthError("a", provider="c"),
            ProviderUnavailableError("u", provider="d"),
        ):
            assert error.provider


class TestEchoModel:
    """The development stand-in must actually produce a turn.

    It silently failed for a while: the greeting is spoken by session.say()
    rather than by the model, so a call connected and sounded correct right up
    until someone spoke, at which point every turn raised AttributeError.
    """

    def _config(self) -> ProviderConfig:
        return ProviderConfig(kind=ProviderKind.LLM, provider="echo_dev", model="echo")

    @pytest.mark.asyncio
    async def test_it_replies_with_what_it_heard(self) -> None:
        from worker.providers.llm.echo import build

        llm = build(self._config())
        chunks = [c async for c in llm.generate([Message(role="user", content="hello there")])]
        text = "".join(c.text for c in chunks)
        assert "hello there" in text
        assert chunks[-1].finished

    @pytest.mark.asyncio
    async def test_it_replies_even_with_no_user_turn(self) -> None:
        from worker.providers.llm.echo import build

        llm = build(self._config())
        chunks = [c async for c in llm.generate([])]
        assert "".join(c.text for c in chunks).strip()

    @pytest.mark.asyncio
    async def test_the_livekit_component_carries_connect_options(self) -> None:
        """Regression: conn_options=None made every turn raise AttributeError.

        Async because constructing the stream schedules a task, so it needs a
        running loop.
        """
        from livekit.agents import llm as lk_llm

        from worker.providers.llm.echo import build

        component = build(self._config()).build_livekit_component()
        stream = component.chat(chat_ctx=lk_llm.ChatContext(), tools=[])
        try:
            assert stream._conn_options is not None
            assert stream._conn_options.max_retry >= 0
        finally:
            await stream.aclose()

    def test_it_is_refused_outside_development(self, monkeypatch) -> None:
        """A canned-response model must never answer a production call."""
        from worker.providers.llm.echo import build

        monkeypatch.setenv("ENVIRONMENT", "production")
        with pytest.raises(ProviderUnavailableError, match="development stand-in"):
            build(self._config())
