"""Thinking filler and the endpointing window (spec 29, Plan 2b.7).

The filler exists to stop a slow reply sounding like a dropped line. These
tests pin the two ways it could make things worse instead: speaking when the
answer was about to arrive, and repeating itself.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from worker.endpointing import (
    MAX_ENDPOINTING_DELAY_S,
    MIN_ENDPOINTING_DELAY_S,
    OBSERVED_IN_CALL_STT_S,
)
from worker.filler import FILLER_PHRASES, ThinkingFiller


class FakeSession:
    """Enough AgentSession to drive the filler."""

    def __init__(self, state: str = "thinking") -> None:
        self.agent_state = state
        #: The speech handle. Not None means a reply is already being spoken,
        #: which is the signal that arrives before `agent_state` catches up.
        self.current_speech: Any = None
        self.said: list[str] = []
        self._handlers: dict[str, Any] = {}

    def on(self, event: str, handler: Any) -> None:
        self._handlers[event] = handler

    async def say(self, text: str, **_: Any) -> None:
        self.said.append(text)

    def emit(self, new_state: str) -> None:
        handler = self._handlers.get("agent_state_changed")
        if handler:
            handler(type("Event", (), {"new_state": new_state})())


class TestTheWindowClearsMeasuredTranscription:
    """The 2b.7 failure was a turn committing before its transcript arrived."""

    def test_min_delay_exceeds_the_worst_in_call_transcription(self) -> None:
        assert MIN_ENDPOINTING_DELAY_S > OBSERVED_IN_CALL_STT_S, (
            "the turn would commit before the transcript lands, which is "
            "exactly the 2b.7 symptom: the caller speaks and is ignored"
        )

    def test_the_margin_is_not_so_thin_that_variance_breaks_it(self) -> None:
        """A 400 ms margin is the deliberate trade. Below ~250 ms, ordinary
        jitter starts eating it and the failure is silent."""
        assert MIN_ENDPOINTING_DELAY_S - OBSERVED_IN_CALL_STT_S >= 0.25

    def test_max_delay_still_exceeds_min(self) -> None:
        assert MAX_ENDPOINTING_DELAY_S > MIN_ENDPOINTING_DELAY_S


class TestFillerOnlyCoversRealWaits:
    @pytest.mark.asyncio
    async def test_a_fast_answer_produces_no_filler(self) -> None:
        """The answer arrived during the delay, so nothing is spoken.

        This is the case that matters most: a filler here would talk over the
        reply and add a turn rather than cover one.
        """
        session = FakeSession()
        ThinkingFiller(session, after_seconds=0.05).attach()

        session.emit("thinking")
        session.agent_state = "speaking"
        session.emit("speaking")
        await asyncio.sleep(0.15)

        assert session.said == []

    @pytest.mark.asyncio
    async def test_a_slow_answer_gets_one_filler(self) -> None:
        session = FakeSession()
        ThinkingFiller(session, after_seconds=0.05).attach()

        session.emit("thinking")
        await asyncio.sleep(0.15)

        assert len(session.said) == 1
        assert session.said[0] in FILLER_PHRASES

    @pytest.mark.asyncio
    async def test_no_filler_once_the_reply_has_started_speaking(self) -> None:
        """The regression this was written for.

        On a real call the reply had begun while `agent_state` still read
        "thinking", so the state check passed and the filler was queued behind
        the answer — the caller heard "How can I assist you today?" followed by
        "Let me pull that up." The speech handle is the earlier signal.
        """
        session = FakeSession()
        ThinkingFiller(session, after_seconds=0.03).attach()

        session.emit("thinking")
        session.current_speech = object()  # reply under way, state not yet updated
        await asyncio.sleep(0.1)

        assert session.said == []

    @pytest.mark.asyncio
    async def test_it_does_not_repeat_the_previous_phrase(self) -> None:
        """Saying "one moment" twice running sounds broken in a way silence
        does not."""
        session = FakeSession()
        filler = ThinkingFiller(session, after_seconds=0.02)
        filler.attach()

        for _ in range(6):
            session.agent_state = "thinking"
            session.emit("thinking")
            await asyncio.sleep(0.06)

        assert len(session.said) == 6
        # Not strict=True: a list zipped with its own tail is one shorter by
        # construction, which is the point of the pairing.
        for earlier, later in zip(session.said, session.said[1:]):  # noqa: B905
            assert earlier != later

    @pytest.mark.asyncio
    async def test_a_failing_say_does_not_end_the_call(self) -> None:
        """A decoration on a wait must never be able to end the call."""
        session = FakeSession()

        async def boom(*_: Any, **__: Any) -> None:
            raise RuntimeError("tts unavailable")

        session.say = boom  # type: ignore[method-assign]
        ThinkingFiller(session, after_seconds=0.02).attach()

        session.emit("thinking")
        await asyncio.sleep(0.08)  # must not raise


class TestThePhrasesThemselves:
    def test_there_are_enough_to_vary(self) -> None:
        assert len(FILLER_PHRASES) >= 8
        assert len(set(FILLER_PHRASES)) == len(FILLER_PHRASES)

    @pytest.mark.parametrize("phrase", FILLER_PHRASES)
    def test_each_is_short_enough_to_stay_out_of_the_way(self, phrase: str) -> None:
        """Longer than about a second of speech and the filler becomes the
        delay it was meant to cover. Six words is roughly that."""
        assert len(phrase.split()) <= 6, phrase
        assert phrase.strip() == phrase
