"""Filler audio on its own track, outside the agent's turn-taking (spec 29).

A caller who asks a question and hears nothing assumes the line dropped. The
obvious way to cover that — ``AgentSession.say`` — cannot work on
livekit-agents 1.8, and the reasons are worth stating because they are what
this module exists to avoid:

* ``say`` has no priority argument and the reply is queued the moment the turn
  commits, so a filler started after that plays **after** the answer.
* Before the turn commits is the only earlier window, and an agent that speaks
  there destroys the caller's pending turn. Measured: three consecutive calls
  produced a greeting, a filler, and zero caller transcript segments.

So this does not speak through the session at all. It publishes a **second
audio track** and writes frames to it directly. LiveKit mixes the room's tracks
for the caller — verified against livekit-sip, which reported
``track_subscribes: 2`` and ``mixer.tracks_total: 2`` with both mixed — so the
filler is audible without ``AgentSession`` ever knowing it happened.

Two things follow from that design, both deliberate:

* **Stopping is just ceasing to write frames.** No interrupt, so nothing can
  cancel the pending reply.
* **The clips are synthesised with the call's own TTS**, not shipped as fixed
  files. A stock WAV would speak in a different voice from the agent, which is
  worse than silence for anyone paying attention.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from typing import TYPE_CHECKING, Any

from shared.logging import get_logger

if TYPE_CHECKING:
    from livekit import rtc

logger = get_logger(__name__)

#: How long the caller may wait before hearing an acknowledgement.
#:
#: Measured rather than chosen: on this pipeline the transcript lands around
#: 1.2 s and the reply's audio starts near 2 s after the caller stops. 700 ms
#: sits inside that gap with room for the clip to play out before the answer
#: arrives, and is late enough that a quick turn stays silent.
FILLER_AFTER_S = 0.7

#: Phrases, synthesised once per voice and then replayed from memory.
#:
#: Short, because the answer lands immediately after. Varied in rhythm as well
#: as wording — identical cadence every time is what makes a voice sound
#: synthetic even when the words differ.
FILLER_PHRASES: tuple[str, ...] = (
    "One moment.",
    "Let me check that.",
    "Give me a second.",
    "Looking into that now.",
    "Just a moment, please.",
    "Okay, checking.",
    "Bear with me a second.",
    "Right, let me look.",
    "Thanks, checking that for you.",
    "Hold on a moment, please.",
    "Let me pull that up.",
    "One second while I check.",
)

#: Synthesised clips, keyed by (voice identity, phrase), shared across calls in
#: this process. The first call using a voice pays for synthesis; every later
#: call replays from memory, which is the point — a filler that waited on TTS
#: would be the delay it set out to cover.
_CLIPS: dict[tuple[str, str], list[Any]] = {}


class FillerTrack:
    """Publishes filler audio on a dedicated track, independent of the session.

    Lifecycle mirrors the call: ``start`` publishes the track and warms the
    clips, ``arm`` schedules a phrase, ``stop`` silences it, ``aclose`` tidies
    up. Every method is safe to call when the previous one failed — a
    decoration on a wait must never be able to end the call it decorates.
    """

    def __init__(
        self,
        room: Any,
        tts: Any,
        *,
        voice_key: str,
        after_seconds: float = FILLER_AFTER_S,
        phrases: tuple[str, ...] = FILLER_PHRASES,
    ) -> None:
        self._room = room
        self._tts = tts
        self._voice_key = voice_key
        self._after = after_seconds
        self._phrases = phrases

        self._source: rtc.AudioSource | None = None
        self._publication: Any = None
        self._task: asyncio.Task[None] | None = None
        self._warming: asyncio.Task[None] | None = None
        self._playing: asyncio.Task[None] | None = None
        self._last: str | None = None
        self._generation = 0
        self.spoken = 0

    # -- lifecycle ------------------------------------------------------- #

    async def start(self) -> None:
        """Publish the track and synthesise the clips for this voice.

        Synthesis runs in the background: it must not delay the greeting, and
        the first turn is usually far enough away that the clips are ready.
        """
        from livekit import rtc

        # 24 kHz mono matches what the TTS returns here, so frames are written
        # without resampling. LiveKit resamples for the SIP leg anyway.
        self._source = rtc.AudioSource(24000, 1)
        track = rtc.LocalAudioTrack.create_audio_track("agent-filler", self._source)
        self._publication = await self._room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        logger.info("filler_track_published", extra={"voice": self._voice_key})

        # Held, so it is not garbage collected mid-synthesis and so aclose
        # can cancel it if the call ends first.
        self._warming = asyncio.create_task(self._warm())

    async def _warm(self) -> None:
        """Synthesise any phrases this voice has not produced before."""
        missing = [p for p in self._phrases if (self._voice_key, p) not in _CLIPS]
        if not missing:
            return
        for phrase in missing:
            try:
                frames = [a.frame async for a in self._tts.synthesize(phrase)]
                _CLIPS[(self._voice_key, phrase)] = frames
            except Exception:
                logger.warning("filler_clip_synthesis_failed", extra={"phrase": phrase})
        logger.info(
            "filler_clips_ready",
            extra={"voice": self._voice_key, "count": len(missing)},
        )

    async def aclose(self) -> None:
        self.stop()
        if self._warming is not None and not self._warming.done():
            self._warming.cancel()
        if self._publication is not None:
            with contextlib.suppress(Exception):
                await self._room.local_participant.unpublish_track(
                    self._publication.sid
                )
        self._publication = None
        self._source = None

    # -- playback -------------------------------------------------------- #

    def arm(self) -> None:
        """Schedule a filler. Cancels any previously scheduled one."""
        self.stop()
        self._generation += 1
        generation = self._generation
        self._task = asyncio.create_task(self._after_delay(generation))

    def stop(self) -> None:
        """Silence immediately.

        Cancelling the writer is the whole stop mechanism — there is no
        interrupt, so nothing here can cancel the caller's pending reply.
        """
        self._generation += 1
        for task in (self._task, self._playing):
            if task is not None and not task.done():
                task.cancel()
        self._task = None
        self._playing = None

    def _choose(self) -> str | None:
        ready = [
            p
            for p in self._phrases
            if (self._voice_key, p) in _CLIPS and p != self._last
        ]
        if not ready:
            return None
        return random.choice(ready)  # noqa: S311 — a pleasantry, not a secret

    async def _after_delay(self, generation: int) -> None:
        try:
            await asyncio.sleep(self._after)
            if generation != self._generation:
                return

            phrase = self._choose()
            if phrase is None:
                logger.debug("filler_no_clip_ready")
                return

            self._last = phrase
            self.spoken += 1
            logger.info("filler_played", extra={"phrase": phrase})
            self._playing = asyncio.create_task(self._write(phrase, generation))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("filler_schedule_failed", exc_info=True)

    async def _write(self, phrase: str, generation: int) -> None:
        source = self._source
        if source is None:
            return
        try:
            for frame in _CLIPS.get((self._voice_key, phrase), []):
                # Checked per frame so `stop` takes effect within one frame of
                # the real answer starting, rather than at the end of the clip.
                if generation != self._generation:
                    return
                await source.capture_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("filler_write_failed", exc_info=True)
