"""Plan 6.11: remaining §25 STT / LLM / TTS adapters.

Each vendor that is not OpenAI-compatible has its own module plus one registry
entry. Plugins are mocked so the suite does not need vendor credentials or
optional Google/Azure packages in every environment.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from shared.models import ProviderKind
from worker.providers.base import ProviderConfig
from worker.providers.registry import available_slugs, build_llm, build_stt, build_tts


def _config(kind: ProviderKind, provider: str, **kwargs) -> ProviderConfig:
    defaults = {
        "kind": kind,
        "provider": provider,
        "model": None,
        "api_key": "test-key",
        "base_url": None,
        "voice_id": None,
        "language": None,
    }
    defaults.update(kwargs)
    return ProviderConfig(**defaults)


class TestSection25Registry:
    def test_stt_covers_the_required_vendors(self) -> None:
        slugs = set(available_slugs(ProviderKind.STT))
        assert {
            "openai_compatible",
            "deepgram",
            "elevenlabs",
            "google_stt",
            "azure",
        } <= slugs

    def test_llm_covers_the_required_vendors(self) -> None:
        slugs = set(available_slugs(ProviderKind.LLM))
        assert {"openai_compatible", "anthropic"} <= slugs

    def test_tts_covers_the_required_vendors(self) -> None:
        slugs = set(available_slugs(ProviderKind.TTS))
        assert {
            "openai_compatible",
            "elevenlabs",
            "cartesia",
            "deepgram",
            "google",
            "azure",
        } <= slugs


class TestBuildResolves:
    def test_deepgram_stt(self) -> None:
        from worker.providers.stt.deepgram import DeepgramSTT

        assert isinstance(build_stt(_config(ProviderKind.STT, "deepgram")), DeepgramSTT)

    def test_elevenlabs_stt(self) -> None:
        from worker.providers.stt.elevenlabs import ElevenLabsSTT

        assert isinstance(build_stt(_config(ProviderKind.STT, "elevenlabs")), ElevenLabsSTT)

    def test_google_stt(self) -> None:
        from worker.providers.stt.google import GoogleSTT

        assert isinstance(build_stt(_config(ProviderKind.STT, "google_stt")), GoogleSTT)

    def test_azure_stt(self) -> None:
        from worker.providers.stt.azure import AzureSTT

        assert isinstance(build_stt(_config(ProviderKind.STT, "azure")), AzureSTT)

    def test_anthropic_llm(self) -> None:
        from worker.providers.llm.anthropic import AnthropicLLM

        assert isinstance(build_llm(_config(ProviderKind.LLM, "anthropic")), AnthropicLLM)

    def test_cartesia_tts(self) -> None:
        from worker.providers.tts.cartesia import CartesiaTTS

        assert isinstance(build_tts(_config(ProviderKind.TTS, "cartesia")), CartesiaTTS)

    def test_deepgram_tts(self) -> None:
        from worker.providers.tts.deepgram import DeepgramTTS

        assert isinstance(build_tts(_config(ProviderKind.TTS, "deepgram")), DeepgramTTS)

    def test_google_tts(self) -> None:
        from worker.providers.tts.google import GoogleTTS

        assert isinstance(build_tts(_config(ProviderKind.TTS, "google")), GoogleTTS)

    def test_azure_tts(self) -> None:
        from worker.providers.tts.azure import AzureTTS

        assert isinstance(build_tts(_config(ProviderKind.TTS, "azure")), AzureTTS)


class TestLivekitKwargs:
    def test_deepgram_stt_forwards_model_and_language(self) -> None:
        from worker.providers.stt.deepgram import build

        captured: dict[str, Any] = {}

        class _Fake:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        adapter = build(_config(ProviderKind.STT, "deepgram", model="nova-3", language="en-US"))
        with patch("livekit.plugins.deepgram.STT", _Fake):
            adapter.build_livekit_component()
        assert captured["model"] == "nova-3"
        assert captured["language"] == "en-US"
        assert captured["api_key"] == "test-key"

    def test_anthropic_llm_forwards_model(self) -> None:
        from worker.providers.llm.anthropic import build

        captured: dict[str, Any] = {}

        class _Fake:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        adapter = build(_config(ProviderKind.LLM, "anthropic", model="claude-sonnet-4-6"))
        with patch("livekit.plugins.anthropic.LLM", _Fake):
            adapter.build_livekit_component()
        assert captured["model"] == "claude-sonnet-4-6"
        assert captured["api_key"] == "test-key"

    def test_cartesia_tts_forwards_voice(self) -> None:
        from worker.providers.tts.cartesia import build

        captured: dict[str, Any] = {}

        class _Fake:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        adapter = build(_config(ProviderKind.TTS, "cartesia", voice_id="voice-1"))
        with patch("livekit.plugins.cartesia.TTS", _Fake):
            adapter.build_livekit_component()
        assert captured["voice"] == "voice-1"

    def test_azure_tts_maps_api_key_to_speech_key(self) -> None:
        import sys
        from types import SimpleNamespace

        from worker.providers.tts.azure import build

        captured: dict[str, Any] = {}

        class _Fake:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        adapter = build(
            _config(ProviderKind.TTS, "azure", voice_id="en-US-JennyNeural")
        )
        fake = SimpleNamespace(TTS=_Fake)
        with patch.dict(sys.modules, {"livekit.plugins.azure": fake}):
            adapter.build_livekit_component()
        assert captured["speech_key"] == "test-key"
        assert captured["voice"] == "en-US-JennyNeural"

    def test_google_stt_omits_none_credentials(self) -> None:
        import sys
        from types import SimpleNamespace

        from worker.providers.stt.google import build

        captured: dict[str, Any] = {}

        class _Fake:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        adapter = build(_config(ProviderKind.STT, "google_stt"))
        fake = SimpleNamespace(STT=_Fake)
        with patch.dict(sys.modules, {"livekit.plugins.google": fake}):
            adapter.build_livekit_component()
        assert "credentials_info" not in captured
        assert "credentials_file" not in captured
        assert captured["api_key"] == "test-key"
