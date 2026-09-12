"""ElevenLabs speech-to-text adapter (spec 24, 25)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from worker.providers._livekit import optional_kwargs, recognize_stt
from worker.providers.base import ProviderConfig, STTProvider, Transcript


class ElevenLabsSTT(STTProvider):
    slug = "elevenlabs"

    def build_livekit_component(self) -> Any:
        from livekit.plugins import elevenlabs as lk_elevenlabs

        return lk_elevenlabs.STT(
            **optional_kwargs(
                model=self.config.model,
                api_key=self.config.api_key,
                base_url=self.config.base_url,
                language_code=self.config.language,
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


def build(config: ProviderConfig) -> ElevenLabsSTT:
    return ElevenLabsSTT(config)
