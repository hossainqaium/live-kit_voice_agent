"""Voice preview synthesis (spec 27, Plan 4b.5).

The ``sample_object_key`` column already existed. This suite covers the new
synthesis helpers and the Test endpoint contract — without calling a live
TTS provider or MinIO.
"""

from __future__ import annotations

import inspect
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from app.schemas.platform import VoiceTestRequest, VoiceTestResponse
from app.services import voice_preview


_VOICES_PAGE = (
    Path(__file__).parent.parent.parent.parent
    / "services"
    / "frontend"
    / "app"
    / "platform"
    / "voices"
    / "page.tsx"
)


class TestSpeechEndpoint:
    def test_openai_compatible_default_url(self) -> None:
        url = voice_preview.speech_endpoint(
            "openai_compatible", base_url=None, provider_voice_id="alloy"
        )
        assert url.endswith("/audio/speech")

    def test_openai_compatible_uses_provider_base_url(self) -> None:
        url = voice_preview.speech_endpoint(
            "openai_compatible",
            base_url="http://speaches:8000/v1",
            provider_voice_id="af_heart",
        )
        assert url == "http://speaches:8000/v1/audio/speech"

    def test_elevenlabs_url_includes_voice_id(self) -> None:
        url = voice_preview.speech_endpoint(
            "elevenlabs", base_url=None, provider_voice_id="21m00Tcm4TlvDq8ikWAM"
        )
        assert url.endswith("/v1/text-to-speech/21m00Tcm4TlvDq8ikWAM")

    def test_elevenlabs_does_not_double_v1(self) -> None:
        url = voice_preview.speech_endpoint(
            "elevenlabs",
            base_url="https://api.elevenlabs.io/v1",
            provider_voice_id="abc",
        )
        assert url == "https://api.elevenlabs.io/v1/text-to-speech/abc"

    def test_unknown_adapter_is_refused(self) -> None:
        with pytest.raises(voice_preview.PreviewError) as exc:
            voice_preview.speech_endpoint(
                "deepgram", base_url=None, provider_voice_id="x"
            )
        assert exc.value.status_code == 400


class TestSampleObjectKey:
    def test_key_is_stable_per_voice(self) -> None:
        voice_id = uuid.uuid4()
        assert voice_preview.sample_object_key(voice_id) == (
            f"voice-samples/{voice_id}.mp3"
        )

    def test_key_does_not_embed_the_provider_voice_id(self) -> None:
        """The catalog UUID is the object name — not the vendor voice id —
        so two providers can share a vendor id without colliding."""
        key = voice_preview.sample_object_key(uuid.uuid4())
        assert key.startswith("voice-samples/")
        assert "alloy" not in key


class TestVoiceTestSchema:
    def test_empty_body_is_valid(self) -> None:
        VoiceTestRequest()

    def test_forbids_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            VoiceTestRequest.model_validate({"text": "hi", "secret": "x"})

    def test_response_has_storage_fields(self) -> None:
        fields = VoiceTestResponse.model_fields
        assert "sample_object_key" in fields
        assert "content_type" in fields
        assert "bytes" in fields


class TestSynthesize:
    @pytest.mark.asyncio
    async def test_posts_to_the_speech_endpoint(self) -> None:
        response = MagicMock()
        response.status_code = 200
        response.content = b"ID3fake-mp3"
        response.headers = {"content-type": "audio/mpeg"}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.voice_preview.httpx.AsyncClient", return_value=mock_client):
            audio, content_type = await voice_preview.synthesize(
                adapter="openai_compatible",
                provider_voice_id="alloy",
                text="Hello",
                api_key="sk-test",
                base_url="https://api.openai.com/v1",
                model="tts-1",
            )

        assert audio == b"ID3fake-mp3"
        assert content_type == "audio/mpeg"
        called_url = mock_client.post.call_args[0][0]
        assert called_url.endswith("/audio/speech")
        headers = mock_client.post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk-test"

    @pytest.mark.asyncio
    async def test_provider_error_does_not_echo_the_body(self) -> None:
        response = MagicMock()
        response.status_code = 401
        response.content = b'{"error":"sk-secret-in-body"}'
        response.headers = {}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.voice_preview.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(voice_preview.PreviewError) as exc:
                await voice_preview.synthesize(
                    adapter="openai_compatible",
                    provider_voice_id="alloy",
                    text="Hello",
                    api_key="sk-secret-in-body",
                    base_url=None,
                    model=None,
                )
        assert "sk-secret" not in exc.value.detail
        assert "401" in exc.value.detail


class TestTestEndpointContract:
    def test_handler_audits_without_the_key(self) -> None:
        from app.api.v1 import platform as platform_api

        src = inspect.getsource(platform_api.test_voice)
        assert "audit.record" in src
        assert "voice.previewed" in src
        assert "payload.api_key" not in src.split("audit.record")[1]

    def test_handler_requires_a_key_when_the_provider_does(self) -> None:
        from app.api.v1 import platform as platform_api

        src = inspect.getsource(platform_api.test_voice)
        assert "requires_credential" in src

    def test_sample_route_streams_stored_audio(self) -> None:
        from app.api.v1 import platform as platform_api

        src = inspect.getsource(platform_api.get_voice_sample)
        assert "StreamingResponse" in src
        assert "fetch_sample" in src


class TestVoiceLibraryUi:
    def test_test_action_is_on_the_page(self) -> None:
        if not _VOICES_PAGE.exists():
            pytest.skip("services/frontend is not mounted in this container")
        src = _VOICES_PAGE.read_text()
        assert ">Test<" in src or "Test</Button>" in src or "setTesting" in src
        assert "voices.test" in src
        assert "voices.sample" in src
        assert "type=\"password\"" in src
