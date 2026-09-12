"""Voice preview synthesis and sample storage (spec 27, 4b.5).

A Test click is a one-shot admin action, not a call. The worker's LiveKit
TTS adapters are the wrong tool: they expect a session. This module talks
to the provider over HTTP, stores the audio in the same object bucket as
recordings, and leaves ``voices.sample_object_key`` pointing at it.

The API key is accepted only for the request. It is never persisted, logged,
or written to an audit row (spec 54).
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from app.core.settings import get_settings
from shared.logging import get_logger

logger = get_logger(__name__)

DEFAULT_PREVIEW_TEXT = "Hello, this is a preview of this voice."
_SYNTH_TIMEOUT_SECONDS = 20.0
_ADAPTER_OPENAI = "openai_compatible"
_ADAPTER_ELEVENLABS = "elevenlabs"


class PreviewError(Exception):
    """A provider or storage failure that should become an HTTP 4xx/5xx."""

    def __init__(self, detail: str, *, status_code: int = 502) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def sample_object_key(voice_row_id: uuid.UUID) -> str:
    """Stable key so a re-test overwrites the previous sample."""
    return f"voice-samples/{voice_row_id}.mp3"


def speech_endpoint(
    adapter: str, *, base_url: str | None, provider_voice_id: str
) -> str:
    """Build the provider URL. Kept pure so tests can assert it without I/O."""
    if adapter == _ADAPTER_ELEVENLABS:
        root = (base_url or "https://api.elevenlabs.io").rstrip("/")
        if root.endswith("/v1"):
            return f"{root}/text-to-speech/{provider_voice_id}"
        return f"{root}/v1/text-to-speech/{provider_voice_id}"

    if adapter == _ADAPTER_OPENAI:
        root = (base_url or "https://api.openai.com/v1").rstrip("/")
        return f"{root}/audio/speech"

    raise PreviewError(
        f"preview is not implemented for adapter {adapter!r}",
        status_code=400,
    )


def _headers(adapter: str, api_key: str | None) -> dict[str, str]:
    if adapter == _ADAPTER_ELEVENLABS:
        headers = {"Accept": "audio/mpeg", "Content-Type": "application/json"}
        if api_key:
            headers["xi-api-key"] = api_key
        return headers
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _payload(
    adapter: str, *, text: str, provider_voice_id: str, model: str | None
) -> dict[str, Any]:
    if adapter == _ADAPTER_ELEVENLABS:
        body: dict[str, Any] = {"text": text}
        if model:
            body["model_id"] = model
        return body
    return {
        "model": model or "tts-1",
        "voice": provider_voice_id,
        "input": text,
        "response_format": "mp3",
    }


async def synthesize(
    *,
    adapter: str,
    provider_voice_id: str,
    text: str,
    api_key: str | None,
    base_url: str | None,
    model: str | None,
) -> tuple[bytes, str]:
    """Call the TTS provider once. Returns ``(audio_bytes, content_type)``."""
    url = speech_endpoint(adapter, base_url=base_url, provider_voice_id=provider_voice_id)
    try:
        async with httpx.AsyncClient(timeout=_SYNTH_TIMEOUT_SECONDS) as client:
            response = await client.post(
                url,
                headers=_headers(adapter, api_key),
                json=_payload(
                    adapter,
                    text=text,
                    provider_voice_id=provider_voice_id,
                    model=model,
                ),
            )
    except httpx.HTTPError as exc:
        raise PreviewError(
            f"could not reach the TTS provider: {type(exc).__name__}"
        ) from exc

    if response.status_code >= 400:
        # Do not echo the body: some providers repeat the Authorization header
        # or the key in error JSON.
        raise PreviewError(
            f"the TTS provider answered {response.status_code}",
            status_code=502 if response.status_code >= 500 else 400,
        )

    content_type = response.headers.get("content-type", "audio/mpeg").split(";")[0]
    audio = response.content
    if not audio:
        raise PreviewError("the TTS provider returned an empty body")
    return audio, content_type


async def store_sample(key: str, data: bytes, content_type: str) -> None:
    """Put the sample in the recordings bucket (same MinIO as call egress)."""
    import aioboto3

    settings = get_settings()
    session = aioboto3.Session()
    try:
        async with session.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key_id.get_secret_value(),
            aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
            region_name=settings.s3_region,
        ) as s3:
            await s3.put_object(
                Bucket=settings.s3_bucket_recordings,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
    except Exception as exc:
        logger.exception("voice_sample_upload_failed")
        raise PreviewError("could not store the sample in object storage") from exc


async def fetch_sample(key: str) -> tuple[bytes, str]:
    """Read a stored sample back for playback through the API."""
    import aioboto3

    settings = get_settings()
    session = aioboto3.Session()
    try:
        async with session.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key_id.get_secret_value(),
            aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
            region_name=settings.s3_region,
        ) as s3:
            obj = await s3.get_object(Bucket=settings.s3_bucket_recordings, Key=key)
            body = await obj["Body"].read()
            content_type = obj.get("ContentType") or "audio/mpeg"
            return body, content_type
    except Exception as exc:
        logger.exception("voice_sample_fetch_failed")
        raise PreviewError("the stored sample could not be read", status_code=404) from exc
