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


class _Speech:
    """A SpeechHandle stand-in. `scheduled` is the property that separates
    "generating a reply" from "audio is going out"."""

    def __init__(self, *, scheduled: bool) -> None:
        self.scheduled = scheduled


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
        """An agent_state_changed event."""
        handler = self._handlers.get("agent_state_changed")
        if handler:
            handler(type("Event", (), {"new_state": new_state})())

    def emit_user(self, new_state: str, old_state: str = "speaking") -> None:
        """A user_state_changed event — what actually arms the filler.

        The filler cannot arm on "thinking": by then LiveKit has queued the
        reply and `session.say` has no priority argument, so the filler would
        play after the answer.
        """
        handler = self._handlers.get("user_state_changed")
        if handler:
            handler(
                type("Event", (), {"new_state": new_state, "old_state": old_state})()
            )


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


class TestFillerIsQueuedAheadOfTheReply:
    """The filler arms when the caller stops speaking, not when the agent
    starts thinking. By "thinking" the reply is already queued and the filler
    would land after the answer — which is what happened on a real call."""

    @pytest.mark.asyncio
    async def test_it_speaks_when_the_caller_stops(self) -> None:
        session = FakeSession()
        ThinkingFiller(session, after_seconds=0.05).attach()

        session.emit_user("listening")
        await asyncio.sleep(0.15)

        assert len(session.said) == 1
        assert session.said[0] in FILLER_PHRASES

    @pytest.mark.asyncio
    async def test_the_caller_resuming_cancels_it(self) -> None:
        """A pause is not the end of a turn. If they carry on, say nothing."""
        session = FakeSession()
        ThinkingFiller(session, after_seconds=0.05).attach()

        session.emit_user("listening")
        session.emit_user("speaking")
        await asyncio.sleep(0.15)

        assert session.said == []

    @pytest.mark.asyncio
    async def test_it_stays_quiet_once_the_agent_is_speaking(self) -> None:
        """Never talk over the answer."""
        session = FakeSession()
        ThinkingFiller(session, after_seconds=0.05).attach()

        session.emit_user("listening")
        session.emit("speaking")
        await asyncio.sleep(0.15)

        assert session.said == []

    @pytest.mark.asyncio
    async def test_it_does_not_repeat_the_previous_phrase(self) -> None:
        """Saying "one moment" twice running sounds broken in a way silence
        does not."""
        session = FakeSession()
        ThinkingFiller(session, after_seconds=0.02).attach()

        for _ in range(6):
            session.emit_user("speaking")
            session.emit_user("listening")
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

        session.emit_user("listening")
        await asyncio.sleep(0.08)  # must not raise


class TestItOnlyFiresAfterTheCallerActuallySpoke:
    """The failure that lost the caller's question entirely."""

    @pytest.mark.asyncio
    async def test_silence_after_the_greeting_does_not_arm_it(self) -> None:
        """"listening" is the state for "not speaking", which is true right
        after the greeting too. Arming there fired a filler into the opening
        silence; the caller's question then arrived mid-phrase and was
        discarded, so the agent said "One moment." and never answered.
        """
        session = FakeSession()
        ThinkingFiller(session, after_seconds=0.02).attach()

        # No speech yet: the session settles into listening from idle.
        session.emit_user("listening", old_state="away")
        await asyncio.sleep(0.08)

        assert session.said == []

    @pytest.mark.asyncio
    async def test_only_speaking_to_listening_arms_it(self) -> None:
        session = FakeSession()
        ThinkingFiller(session, after_seconds=0.02).attach()

        session.emit_user("listening", old_state="speaking")
        await asyncio.sleep(0.08)

        assert len(session.said) == 1


class TestTheFillerCannotCostTheAnswer:
    """The regression that mattered most: the agent said "let me check" and
    then nothing at all."""

    @pytest.mark.asyncio
    async def test_the_filler_is_not_interruptible(self) -> None:
        """It plays before the turn commits, so the tail of the caller's
        utterance can land on it. An interruptible filler is interrupted, and
        LiveKit cancels the pending reply along with it.

        A second of overlap is the price; a lost answer is not.
        """
        session = FakeSession()
        captured: dict[str, Any] = {}

        async def record(text: str, **kwargs: Any) -> None:
            captured.update(kwargs)
            session.said.append(text)

        session.say = record  # type: ignore[method-assign]
        ThinkingFiller(session, after_seconds=0.02).attach()

        session.emit_user("listening")
        await asyncio.sleep(0.08)

        assert session.said, "filler did not speak"
        assert captured.get("allow_interruptions") is False

    @pytest.mark.asyncio
    async def test_the_filler_stays_out_of_the_conversation_history(self) -> None:
        """Feeding "One moment." back teaches the agent that filler is part of
        its own voice."""
        session = FakeSession()
        captured: dict[str, Any] = {}

        async def record(text: str, **kwargs: Any) -> None:
            captured.update(kwargs)
            session.said.append(text)

        session.say = record  # type: ignore[method-assign]
        ThinkingFiller(session, after_seconds=0.02).attach()

        session.emit_user("listening")
        await asyncio.sleep(0.08)

        assert captured.get("add_to_chat_ctx") is False


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
