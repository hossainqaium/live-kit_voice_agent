"""ElevenLabs TTS adapter tests (spec 24, 25 — Plan 4b.6).

Tests that the ElevenLabs adapter:
  1. Is registered under the ``elevenlabs`` slug and resolves via build_tts().
  2. build_livekit_component() passes the expected kwargs and inactivity timeout.
  3. Falls back to the default voice when voice_id is absent.
  4. Passes api_key / base_url / language only when set (conditional kwargs).
  5. synthesize() yields AudioChunk from the LiveKit component stream.
  6. build_tts() raises ProviderUnavailableError for an unrecognised slug.

LiveKit components are not imported; source inspection and lightweight mocks
keep the test suite runnable without a real ElevenLabs credential.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.models import ProviderKind
from worker.providers.base import ProviderConfig, ProviderUnavailableError
from worker.providers.tts.elevenlabs import (
    ElevenLabsTTS,
    _DEFAULT_VOICE_ID,
    _INACTIVITY_TIMEOUT,
    build,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _config(**kwargs) -> ProviderConfig:
    defaults = {
        "kind": ProviderKind.TTS,
        "provider": "elevenlabs",
        "model": None,
        "api_key": None,
        "base_url": None,
        "voice_id": None,
        "language": None,
    }
    defaults.update(kwargs)
    return ProviderConfig(**defaults)


# --------------------------------------------------------------------------- #
# 1. Registry
# --------------------------------------------------------------------------- #


class TestRegistry:
    def test_elevenlabs_slug_is_registered(self):
        from worker.providers.registry import available_slugs

        assert "elevenlabs" in available_slugs(ProviderKind.TTS)

    def test_build_tts_resolves_elevenlabs(self):
        from worker.providers.registry import build_tts

        config = _config(api_key="test-key")
        adapter = build_tts(config)
        assert isinstance(adapter, ElevenLabsTTS)

    def test_unknown_slug_raises(self):
        from worker.providers.registry import build_tts

        config = _config(provider="nonexistent_vendor")
        with pytest.raises(ProviderUnavailableError):
            build_tts(config)

    def test_elevenlabs_not_in_stt_registry(self):
        """ElevenLabs is TTS-only; registering it under STT would be wrong."""
        from worker.providers.registry import available_slugs

        assert "elevenlabs" not in available_slugs(ProviderKind.STT)


# --------------------------------------------------------------------------- #
# 2. build() and slug
# --------------------------------------------------------------------------- #


class TestBuild:
    def test_build_returns_elevenlabs_tts(self):
        adapter = build(_config())
        assert isinstance(adapter, ElevenLabsTTS)

    def test_slug(self):
        assert ElevenLabsTTS.slug == "elevenlabs"

    def test_kind_mismatch_raises(self):
        with pytest.raises(ValueError):
            ElevenLabsTTS(_config(kind=ProviderKind.STT))


# --------------------------------------------------------------------------- #
# 3 & 4. build_livekit_component kwargs (source inspection + mock)
# --------------------------------------------------------------------------- #


class TestBuildLivekitComponent:
    def test_inactivity_timeout_is_set(self):
        src = inspect.getsource(ElevenLabsTTS.build_livekit_component)
        assert "inactivity_timeout" in src
        assert "_INACTIVITY_TIMEOUT" in src

    def test_default_model_is_turbo(self):
        src = inspect.getsource(ElevenLabsTTS.build_livekit_component)
        assert "eleven_turbo_v2_5" in src

    def test_default_voice_is_applied(self):
        src = inspect.getsource(ElevenLabsTTS.build_livekit_component)
        assert "_DEFAULT_VOICE_ID" in src

    def test_api_key_passed_when_set(self):
        src = inspect.getsource(ElevenLabsTTS.build_livekit_component)
        assert "api_key" in src

    def test_base_url_passed_conditionally(self):
        src = inspect.getsource(ElevenLabsTTS.build_livekit_component)
        assert "base_url" in src

    def test_language_passed_conditionally(self):
        src = inspect.getsource(ElevenLabsTTS.build_livekit_component)
        assert "language" in src

    def test_component_built_with_correct_model(self):
        """Verify the kwargs forwarded to lk_elevenlabs.TTS via mock."""
        adapter = build(_config(model="eleven_flash_v2_5", api_key="sk-test"))
        captured: dict[str, Any] = {}

        class _FakeTTS:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        with patch("livekit.plugins.elevenlabs.TTS", _FakeTTS):
            adapter.build_livekit_component()

        assert captured["model"] == "eleven_flash_v2_5"
        assert captured["inactivity_timeout"] == _INACTIVITY_TIMEOUT

    def test_component_built_with_voice_id(self):
        adapter = build(_config(voice_id="custom-voice-123"))
        captured: dict[str, Any] = {}

        class _FakeTTS:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        with patch("livekit.plugins.elevenlabs.TTS", _FakeTTS):
            adapter.build_livekit_component()

        assert captured["voice_id"] == "custom-voice-123"

    def test_default_voice_used_when_none(self):
        adapter = build(_config(voice_id=None))
        captured: dict[str, Any] = {}

        class _FakeTTS:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        with patch("livekit.plugins.elevenlabs.TTS", _FakeTTS):
            adapter.build_livekit_component()

        assert captured["voice_id"] == _DEFAULT_VOICE_ID

    def test_api_key_omitted_when_none(self):
        adapter = build(_config(api_key=None))
        captured: dict[str, Any] = {}

        class _FakeTTS:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        with patch("livekit.plugins.elevenlabs.TTS", _FakeTTS):
            adapter.build_livekit_component()

        assert "api_key" not in captured

    def test_base_url_omitted_when_none(self):
        adapter = build(_config(base_url=None))
        captured: dict[str, Any] = {}

        class _FakeTTS:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        with patch("livekit.plugins.elevenlabs.TTS", _FakeTTS):
            adapter.build_livekit_component()

        assert "base_url" not in captured

    def test_language_forwarded_when_set(self):
        adapter = build(_config(language="de"))
        captured: dict[str, Any] = {}

        class _FakeTTS:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        with patch("livekit.plugins.elevenlabs.TTS", _FakeTTS):
            adapter.build_livekit_component()

        assert captured.get("language") == "de"

    def test_language_omitted_when_none(self):
        adapter = build(_config(language=None))
        captured: dict[str, Any] = {}

        class _FakeTTS:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        with patch("livekit.plugins.elevenlabs.TTS", _FakeTTS):
            adapter.build_livekit_component()

        assert "language" not in captured


# --------------------------------------------------------------------------- #
# 5. synthesize() yields AudioChunks
# --------------------------------------------------------------------------- #


class TestSynthesize:
    @pytest.mark.asyncio
    async def test_string_input_yields_chunks(self):
        from worker.providers.base import AudioChunk

        import numpy as np

        fake_frame = MagicMock()
        fake_frame.data = bytes(b"\x00\x01" * 800)
        fake_frame.sample_rate = 24000
        fake_frame.num_channels = 1

        fake_event = MagicMock()
        fake_event.frame = fake_frame

        async def _fake_synthesize(text):
            class _Stream:
                def __aiter__(self):
                    return self

                async def __anext__(self):
                    raise StopAsyncIteration

            return _Stream()

        async def _gen():
            yield fake_event

        class _FakeTTS:
            def __init__(self, **kwargs):
                pass

            def synthesize(self, text):
                return _gen()

        adapter = build(_config(api_key="sk-test"))
        chunks = []

        with patch("livekit.plugins.elevenlabs.TTS", _FakeTTS):
            async for chunk in adapter.synthesize("Hello, world!"):
                chunks.append(chunk)

        assert len(chunks) == 1
        assert isinstance(chunks[0], AudioChunk)
        assert chunks[0].sample_rate == 24000
        assert chunks[0].num_channels == 1

    @pytest.mark.asyncio
    async def test_events_without_frame_are_skipped(self):
        """Events with frame=None (e.g. text alignment events) must be ignored."""
        frame_event = MagicMock()
        frame_event.frame = None  # no audio frame

        async def _gen():
            yield frame_event

        class _FakeTTS:
            def __init__(self, **kwargs):
                pass

            def synthesize(self, text):
                return _gen()

        adapter = build(_config())
        chunks = []

        with patch("livekit.plugins.elevenlabs.TTS", _FakeTTS):
            async for chunk in adapter.synthesize("test"):
                chunks.append(chunk)

        assert chunks == []

    @pytest.mark.asyncio
    async def test_async_text_input_uses_stream_api(self):
        """Streaming text input must call component.stream() + push_text."""
        push_calls: list[str] = []
        end_called = False

        class _FakeStream:
            def push_text(self, text):
                push_calls.append(text)

            def end_input(self):
                nonlocal end_called
                end_called = True

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        class _FakeTTS:
            def __init__(self, **kwargs):
                pass

            def stream(self):
                return _FakeStream()

        async def _text_iter():
            yield "Hello"
            yield ", world!"

        adapter = build(_config())

        with patch("livekit.plugins.elevenlabs.TTS", _FakeTTS):
            async for _ in adapter.synthesize(_text_iter()):
                pass

        import asyncio

        await asyncio.sleep(0)  # let _feed task run

        assert "Hello" in push_calls
        assert ", world!" in push_calls
        assert end_called
