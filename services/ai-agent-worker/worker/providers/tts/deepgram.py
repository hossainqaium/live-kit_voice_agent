"""Deepgram Aura text-to-speech adapter (spec 24, 25)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from worker.providers._livekit import optional_kwargs, stream_tts
from worker.providers.base import AudioChunk, ProviderConfig, TTSProvider


class DeepgramTTS(TTSProvider):
    slug = "deepgram"

    def build_livekit_component(self) -> Any:
        from livekit.plugins import deepgram as lk_deepgram

        return lk_deepgram.TTS(
            **optional_kwargs(
                model=self.config.model or "aura-2-andromeda-en",
                api_key=self.config.api_key,
                base_url=self.config.base_url,
            )
        )

    async def synthesize(
        self, text: str | AsyncIterator[str], *, voice_id: str | None = None
    ) -> AsyncIterator[AudioChunk]:
        async for chunk in stream_tts(self.build_livekit_component(), text):
            yield chunk


def build(config: ProviderConfig) -> DeepgramTTS:
    return DeepgramTTS(config)
