"""Azure text-to-speech adapter (spec 24, 25)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from worker.providers._livekit import optional_kwargs, stream_tts
from worker.providers.base import AudioChunk, ProviderConfig, TTSProvider


class AzureTTS(TTSProvider):
    slug = "azure"

    def build_livekit_component(self) -> Any:
        from livekit.plugins import azure as lk_azure

        options = dict(self.config.options or {})
        return lk_azure.TTS(
            **optional_kwargs(
                speech_key=self.config.api_key,
                speech_region=options.get("region") or options.get("speech_region"),
                voice=self.config.voice_id,
                language=self.config.language,
            )
        )

    async def synthesize(
        self, text: str | AsyncIterator[str], *, voice_id: str | None = None
    ) -> AsyncIterator[AudioChunk]:
        async for chunk in stream_tts(self.build_livekit_component(), text):
            yield chunk


def build(config: ProviderConfig) -> AzureTTS:
    return AzureTTS(config)
