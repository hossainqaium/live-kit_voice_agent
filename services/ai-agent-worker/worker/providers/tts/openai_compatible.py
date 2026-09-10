"""OpenAI-compatible text-to-speech adapter (spec 24, 25).

Covers OpenAI's ``/v1/audio/speech`` and self-hosted servers implementing it,
such as speaches with Kokoro.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from livekit.plugins import openai as lk_openai

from worker.providers.base import AudioChunk, ProviderConfig, TTSProvider

_NO_CREDENTIAL_PLACEHOLDER = "not-required"


class OpenAICompatibleTTS(TTSProvider):
    """Speech synthesis over the OpenAI audio API shape."""

    slug = "openai_compatible"

    def build_livekit_component(self) -> Any:
        return lk_openai.TTS(
            model=self.config.model or "tts-1",
            voice=self.config.voice_id or "alloy",
            base_url=self.config.base_url,
            api_key=self.config.api_key or _NO_CREDENTIAL_PLACEHOLDER,
        )

    async def synthesize(
        self, text: str | AsyncIterator[str], *, voice_id: str | None = None
    ) -> AsyncIterator[AudioChunk]:
        """Stream synthesised audio.

        Accepting an async iterator of text lets synthesis start from the first
        LLM tokens rather than waiting for a finished sentence, which is most
        of the time-to-first-audio budget (spec 56).
        """
        component = self.build_livekit_component()

        if isinstance(text, str):
            stream = component.synthesize(text)
        else:
            stream = component.stream()

            async def _feed() -> None:
                async for piece in text:  # type: ignore[union-attr]
                    stream.push_text(piece)
                stream.end_input()

            import asyncio

            asyncio.create_task(_feed())  # noqa: RUF006 - lifetime bound to the stream

        async for event in stream:
            frame = getattr(event, "frame", None)
            if frame is None:
                continue
            yield AudioChunk(
                data=bytes(frame.data),
                sample_rate=frame.sample_rate,
                num_channels=frame.num_channels,
            )


def build(config: ProviderConfig) -> OpenAICompatibleTTS:
    return OpenAICompatibleTTS(config)
