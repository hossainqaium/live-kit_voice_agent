"""ElevenLabs text-to-speech adapter (spec 24, 25 — Plan 4b.6).

Wraps LiveKit's ``livekit.plugins.elevenlabs.TTS`` so that any agent version
configured with provider ``elevenlabs`` can serve real calls.  The catalog has
offered ElevenLabs as a selectable provider since Phase 4c; without this
adapter a call that selects it ends with a :class:`ProviderUnavailableError`
before any audio is produced.

Architecture note
-----------------
The ElevenLabs LiveKit plugin uses a **WebSocket** for streaming synthesis,
backed by ``aiohttp`` rather than httpx.  This means the httpx-based
:func:`~worker.resilience.make_resilient_client` circuit breaker cannot be
injected via a ``client=`` parameter as it is for the OpenAI adapters.

Resilience is provided instead by:

* ``inactivity_timeout=30`` — closes a stuck WebSocket if no audio frames
  are received for 30 s, preventing a silent hang from outlasting the turn.
* ``FallbackAdapter(attempt_timeout=...)`` in ``_build_session`` — if this
  adapter is part of a multi-tier TTS chain, the latency trip-wire trips
  at 10 s and switches to the next tier.

The plugin manages the lifecycle of its own aiohttp session; the adapter
does not create or close one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from worker.providers.base import AudioChunk, ProviderConfig, TTSProvider

#: ElevenLabs "Rachel" — broadly available across API plans and a sensible
#: default when no ``voice_id`` is configured on the agent version.
_DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"

#: Seconds without a WebSocket audio frame before the connection is dropped.
#: The plugin's own default (WS_INACTIVITY_TIMEOUT) is 300 s — far too long
#: to silently stall a voice call.
_INACTIVITY_TIMEOUT = 30


class ElevenLabsTTS(TTSProvider):
    """Speech synthesis over the ElevenLabs streaming WebSocket API."""

    #: Adapter key. Matches ``providers.slug`` (and ``providers.adapter``) in
    #: the database for the ElevenLabs catalog row.
    slug = "elevenlabs"

    def build_livekit_component(self) -> Any:
        from livekit.plugins import elevenlabs as lk_elevenlabs

        kwargs: dict[str, Any] = {
            "model": self.config.model or "eleven_turbo_v2_5",
            "voice_id": self.config.voice_id or _DEFAULT_VOICE_ID,
            "inactivity_timeout": _INACTIVITY_TIMEOUT,
        }
        # Conditional kwargs: pass only when set, so the plugin can apply its
        # own defaults and fall back to environment variables where appropriate.
        if self.config.api_key:
            kwargs["api_key"] = self.config.api_key
        if self.config.base_url:
            kwargs["base_url"] = self.config.base_url
        if self.config.language:
            kwargs["language"] = self.config.language

        return lk_elevenlabs.TTS(**kwargs)

    async def synthesize(
        self, text: str | AsyncIterator[str], *, voice_id: str | None = None
    ) -> AsyncIterator[AudioChunk]:
        """Stream synthesised audio from ElevenLabs.

        Accepting an async iterator of text lets synthesis start from the
        first LLM tokens rather than waiting for a finished sentence, which
        is most of the time-to-first-audio budget (spec 56).
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

            asyncio.create_task(_feed())  # noqa: RUF006 - lifetime bound to stream

        async for event in stream:
            frame = getattr(event, "frame", None)
            if frame is None:
                continue
            yield AudioChunk(
                data=bytes(frame.data),
                sample_rate=frame.sample_rate,
                num_channels=frame.num_channels,
            )


def build(config: ProviderConfig) -> ElevenLabsTTS:
    return ElevenLabsTTS(config)
