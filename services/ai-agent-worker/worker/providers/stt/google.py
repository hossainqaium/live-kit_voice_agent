"""Google Cloud speech-to-text adapter (spec 24, 25)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from worker.providers._livekit import optional_kwargs, recognize_stt
from worker.providers.base import ProviderConfig, STTProvider, Transcript


class GoogleSTT(STTProvider):
    slug = "google_stt"

    def build_livekit_component(self) -> Any:
        from livekit.plugins import google as lk_google

        options = dict(self.config.options or {})
        return lk_google.STT(
            **optional_kwargs(
                model=self.config.model,
                languages=self.config.language or options.get("languages"),
                credentials_info=options.get("credentials_info"),
                credentials_file=options.get("credentials_file"),
                api_key=self.config.api_key,
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


def build(config: ProviderConfig) -> GoogleSTT:
    return GoogleSTT(config)
