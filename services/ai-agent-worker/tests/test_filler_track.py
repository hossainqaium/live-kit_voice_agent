"""Filler audio on its own track (spec 29).

The properties here are the ones that failed when the filler went through
``AgentSession.say``: it must not be able to arrive after the answer, and it
must not be able to cost the caller their turn.
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Any

import pytest

from worker.filler_track import _CLIPS, FILLER_PHRASES, FillerTrack


class FakeSource:
    def __init__(self) -> None:
        self.frames: list[Any] = []

    async def capture_frame(self, frame: Any) -> None:
        self.frames.append(frame)
        await asyncio.sleep(0.005)  # pace, as the real source does


def _track(voice: str = "test:voice", after: float = 0.02) -> FillerTrack:
    ft = FillerTrack(object(), object(), voice_key=voice, after_seconds=after)
    ft._source = FakeSource()  # type: ignore[assignment]
    return ft


def _clip(voice: str, phrase: str, frames: int = 4) -> None:
    _CLIPS[(voice, phrase)] = [f"frame-{i}" for i in range(frames)]


class TestItPlaysWithoutTouchingTheSession:
    @pytest.mark.asyncio
    async def test_it_writes_frames_after_the_delay(self) -> None:
        voice = "t1"
        for p in FILLER_PHRASES[:3]:
            _clip(voice, p)
        ft = _track(voice)

        ft.arm()
        await asyncio.sleep(0.15)

        assert ft.spoken == 1
        assert ft._source.frames, "no audio written"  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_stop_silences_within_a_frame(self) -> None:
        """Stopping is ceasing to write, so the real answer is never talked
        over for more than one frame — and nothing is interrupted, so the
        reply cannot be cancelled."""
        voice = "t2"
        for p in FILLER_PHRASES[:3]:
            _clip(voice, p, frames=200)
        ft = _track(voice)

        ft.arm()
        await asyncio.sleep(0.08)
        written = len(ft._source.frames)  # type: ignore[union-attr]
        ft.stop()
        await asyncio.sleep(0.08)

        after = len(ft._source.frames)  # type: ignore[union-attr]
        assert after - written <= 1, "kept writing after stop"
        assert after < 200, "played the whole clip despite stop"

    @pytest.mark.asyncio
    async def test_a_quick_answer_produces_no_audio(self) -> None:
        """Armed then stopped before the delay elapses: silence."""
        voice = "t3"
        for p in FILLER_PHRASES[:3]:
            _clip(voice, p)
        ft = _track(voice, after=0.2)

        ft.arm()
        ft.stop()
        await asyncio.sleep(0.3)

        assert ft.spoken == 0
        assert ft._source.frames == []  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_it_does_not_repeat_the_previous_phrase(self) -> None:
        voice = "t4"
        for p in FILLER_PHRASES:
            _clip(voice, p, frames=1)
        ft = _track(voice)

        chosen: list[str] = []
        for _ in range(6):
            ft.arm()
            await asyncio.sleep(0.06)
            chosen.append(ft._last or "")

        for earlier, later in itertools.pairwise(chosen):
            assert earlier != later

    @pytest.mark.asyncio
    async def test_no_clips_means_silence_not_an_error(self) -> None:
        """A voice whose synthesis failed must degrade to the old behaviour,
        not to a broken call."""
        ft = _track("voice-with-no-clips")

        ft.arm()
        await asyncio.sleep(0.1)

        assert ft.spoken == 0
        assert ft._source.frames == []  # type: ignore[union-attr]


class TestThePhrases:
    def test_there_are_enough_to_vary(self) -> None:
        assert len(FILLER_PHRASES) >= 8
        assert len(set(FILLER_PHRASES)) == len(FILLER_PHRASES)

    @pytest.mark.parametrize("phrase", FILLER_PHRASES)
    def test_each_is_short_enough_to_stay_out_of_the_way(self, phrase: str) -> None:
        """Longer than about a second of speech and the filler becomes the
        delay it was meant to cover."""
        assert len(phrase.split()) <= 6, phrase
        assert phrase.strip() == phrase
