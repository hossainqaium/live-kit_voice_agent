"""OpenAI-compatible speech-to-text adapter (spec 24, 25).

One adapter covers every server that speaks the OpenAI audio API: OpenAI
itself, and self-hosted stacks such as speaches or faster-whisper behind an
OpenAI-compatible gateway. Spec 25 requires local/self-hosted support, and
sharing one code path means a tenant can move between them by changing a base
URL rather than waiting for a new adapter.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import openai as _openai
from livekit.plugins import openai as lk_openai

from worker.providers.base import ProviderConfig, STTProvider, Transcript
from worker.resilience import make_resilient_client

#: Sent when the endpoint needs no credential. Self-hosted servers usually
#: ignore the header, but the OpenAI client library refuses to construct
#: without one, so a placeholder is required rather than optional.
_NO_CREDENTIAL_PLACEHOLDER = "not-required"


class OpenAICompatibleSTT(STTProvider):
    """Transcription over the OpenAI ``/v1/audio/transcriptions`` API."""

    #: Adapter key. Matches ``providers.slug`` in the database.
    slug = "openai_compatible"

    def build_livekit_component(self) -> Any:
        http_client = make_resilient_client(
            provider_key=self.config.base_url or self.config.provider,
            kind="stt",
        )
        openai_client = _openai.AsyncOpenAI(
            api_key=self.config.api_key or _NO_CREDENTIAL_PLACEHOLDER,
            base_url=self.config.base_url,
            http_client=http_client,
            max_retries=0,
        )
        return lk_openai.STT(
            model=self.config.model or "whisper-1",
            language=self.config.language or "en",
            client=openai_client,
        )

    async def transcribe(
        self, audio: AsyncIterator[bytes], *, sample_rate: int = 16000
    ) -> AsyncIterator[Transcript]:
        """Transcribe buffered audio.

        The realtime path does not come through here — it uses the LiveKit
        component directly so audio never leaves the streaming pipeline
        (spec 28). This exists for the platform interface and for tests, and
        it buffers because the HTTP transcription API is not streaming.
        """
        component = self.build_livekit_component()
        chunks = [chunk async for chunk in audio]
        if not chunks:
            return

        from livekit import rtc

        frame = rtc.AudioFrame(
            data=b"".join(chunks),
            sample_rate=sample_rate,
            num_channels=1,
            samples_per_channel=len(b"".join(chunks)) // 2,
        )
        event = await component.recognize(buffer=frame, language=self.config.language)
        for alternative in event.alternatives:
            yield Transcript(
                text=alternative.text,
                is_final=True,
                confidence=getattr(alternative, "confidence", None),
                language=getattr(alternative, "language", None) or self.config.language,
            )


def build(config: ProviderConfig) -> OpenAICompatibleSTT:
    return OpenAICompatibleSTT(config)
