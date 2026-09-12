"""Cartesia text-to-speech adapter (spec 24, 25)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from worker.providers._livekit import optional_kwargs, stream_tts
from worker.providers.base import AudioChunk, ProviderConfig, TTSProvider


class CartesiaTTS(TTSProvider):
    slug = "cartesia"

    def build_livekit_component(self) -> Any:
        from livekit.plugins import cartesia as lk_cartesia

        return lk_cartesia.TTS(
            **optional_kwargs(
                model=self.config.model or "sonic-3",
                api_key=self.config.api_key,
                base_url=self.config.base_url,
                voice=self.config.voice_id,
                language=self.config.language or "en",
            )
        )

    async def synthesize(
        self, text: str | AsyncIterator[str], *, voice_id: str | None = None
    ) -> AsyncIterator[AudioChunk]:
        async for chunk in stream_tts(self.build_livekit_component(), text):
            yield chunk


def build(config: ProviderConfig) -> CartesiaTTS:
    return CartesiaTTS(config)
