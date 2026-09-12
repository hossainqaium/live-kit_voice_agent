"""Isolated TTS tracks for the caller hold path and the agent whisper (TR-2–TR-6)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from shared.logging import get_logger
from worker.transfer.isolation import HOLD_TRACK_NAME, WHISPER_TRACK_NAME

logger = get_logger(__name__)


class IsolatedTrack:
    """Publish synthesised speech on a named track, outside AgentSession.

    Used for the caller announcement/hold and the human-agent whisper so those
    two paths never share a mixer input. Stopping is ceasing to write frames.
    """

    def __init__(
        self,
        room: Any,
        tts: Any,
        *,
        name: str,
    ) -> None:
        self._room = room
        self._tts = tts
        self._name = name
        self._source: Any = None
        self._publication: Any = None
        self._generation = 0
        self._playing: asyncio.Task[None] | None = None

    @property
    def sid(self) -> str | None:
        if self._publication is None:
            return None
        return getattr(self._publication, "sid", None)

    async def start(self) -> None:
        if self._source is not None:
            return
        from livekit import rtc

        self._source = rtc.AudioSource(24000, 1)
        track = rtc.LocalAudioTrack.create_audio_track(self._name, self._source)
        self._publication = await self._room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        logger.info("transfer_track_published", extra={"track": self._name})

    async def play(self, text: str, *, max_seconds: float | None = None) -> float:
        """Speak ``text`` once. Returns how many seconds of audio were written."""
        await self.start()
        if not text.strip() or self._tts is None:
            return 0.0
        self.stop()
        self._generation += 1
        generation = self._generation
        started = time.monotonic()
        deadline = started + max_seconds if max_seconds is not None else None
        try:
            async for audio in self._tts.synthesize(text):
                if generation != self._generation:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
                frame = getattr(audio, "frame", audio)
                if self._source is not None:
                    await self._source.capture_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("transfer_track_play_failed", extra={"track": self._name}, exc_info=True)
        return max(0.0, time.monotonic() - started)

    async def loop(self, text: str, *, interval_s: float = 8.0) -> None:
        """Keep the caller from hearing silence (TR-4)."""
        await self.start()
        self.stop()
        self._generation += 1
        generation = self._generation

        async def _run() -> None:
            while generation == self._generation:
                await self.play(text)
                if generation != self._generation:
                    return
                await asyncio.sleep(interval_s)

        self._playing = asyncio.create_task(_run(), name=f"transfer-loop-{self._name}")

    def stop(self) -> None:
        self._generation += 1
        if self._playing is not None and not self._playing.done():
            self._playing.cancel()
        self._playing = None

    async def aclose(self) -> None:
        self.stop()
        if self._publication is not None:
            with contextlib.suppress(Exception):
                await self._room.local_participant.unpublish_track(self._publication.sid)
        self._publication = None
        self._source = None


def hold_track(room: Any, tts: Any) -> IsolatedTrack:
    return IsolatedTrack(room, tts, name=HOLD_TRACK_NAME)


def whisper_track(room: Any, tts: Any) -> IsolatedTrack:
    return IsolatedTrack(room, tts, name=WHISPER_TRACK_NAME)
