"""Deepgram speech-to-text adapter (spec 24, 25)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from worker.providers._livekit import optional_kwargs, recognize_stt
from worker.providers.base import ProviderConfig, STTProvider, Transcript


class DeepgramSTT(STTProvider):
    slug = "deepgram"

    def build_livekit_component(self) -> Any:
        from livekit.plugins import deepgram as lk_deepgram

        return lk_deepgram.STT(
            **optional_kwargs(
                model=self.config.model or "nova-3",
                language=self.config.language or "en-US",
                api_key=self.config.api_key,
                base_url=self.config.base_url,
            )
        )

    async def transcribe(
        self, audio: AsyncIterator[bytes], *, sample_rate: int = 16000
    ) -> AsyncIterator[Transcript]:
        async for item in recognize_stt(
            self.build_livekit_component(),
            audio,
            sample_rate=sample_rate,
            language=self.config.language,
        ):
            yield item


def build(config: ProviderConfig) -> DeepgramSTT:
    return DeepgramSTT(config)
