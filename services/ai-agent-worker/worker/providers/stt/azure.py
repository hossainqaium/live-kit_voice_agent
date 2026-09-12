"""Azure speech-to-text adapter (spec 24, 25)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from worker.providers._livekit import optional_kwargs, recognize_stt
from worker.providers.base import ProviderConfig, STTProvider, Transcript


class AzureSTT(STTProvider):
    slug = "azure"

    def build_livekit_component(self) -> Any:
        from livekit.plugins import azure as lk_azure

        options = dict(self.config.options or {})
        return lk_azure.STT(
            **optional_kwargs(
                speech_key=self.config.api_key,
                speech_region=options.get("region") or options.get("speech_region"),
                language=self.config.language,
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


def build(config: ProviderConfig) -> AzureSTT:
    return AzureSTT(config)
