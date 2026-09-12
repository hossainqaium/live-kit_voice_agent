"""Google Cloud text-to-speech adapter (spec 24, 25)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from worker.providers._livekit import optional_kwargs, stream_tts
from worker.providers.base import AudioChunk, ProviderConfig, TTSProvider


class GoogleTTS(TTSProvider):
    slug = "google"

    def build_livekit_component(self) -> Any:
        from livekit.plugins import google as lk_google

        options = dict(self.config.options or {})
        return lk_google.TTS(
            **optional_kwargs(
                language=self.config.language,
                voice_name=self.config.voice_id,
                credentials_info=options.get("credentials_info"),
                credentials_file=options.get("credentials_file"),
                api_key=self.config.api_key,
            )
        )

    async def synthesize(
        self, text: str | AsyncIterator[str], *, voice_id: str | None = None
    ) -> AsyncIterator[AudioChunk]:
        async for chunk in stream_tts(self.build_livekit_component(), text):
            yield chunk


def build(config: ProviderConfig) -> GoogleTTS:
    return GoogleTTS(config)
