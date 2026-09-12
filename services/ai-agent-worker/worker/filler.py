"""Spoken filler while the agent is thinking (spec 29).

A caller who asks a question and hears nothing assumes the line is dead. They
say "hello?", which arrives as a new turn and makes the wait longer. A short
acknowledgement costs nothing and stops that loop.

**It is not a latency fix and must not be mistaken for one.** The gap is real;
this only stops it sounding like a fault. The measured components are in Plan
§12.1, and shortening them is 2b.9 and 2b.10 work.

Two rules keep it from making things worse:

* **Only speak if the wait is already long enough to notice.** Below
  ``FILLER_AFTER_S`` a filler would arrive on top of the answer and add a turn
  rather than cover one.
* **Never repeat the previous phrase.** A voice that says "one moment" twice in
  a row sounds broken in a way silence does not.
"""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING, Any

from shared.logging import get_logger

if TYPE_CHECKING:
    from livekit.agents import AgentSession

logger = get_logger(__name__)

#: How long the agent may think silently before it acknowledges the caller.
#:
#: Measured, not guessed: the LLM answers in roughly 800 ms on a normal turn,
#: so a 600 ms threshold fired on almost every turn and lost the race — the
#: filler landed *after* the answer, giving the caller "Thanks! How can I help?"
#: followed by "Let me pull that up." That is worse than the silence it set out
#: to cover.
#:
#: 1.2 s clears an ordinary turn and leaves the filler for the waits that are
#: genuinely long: a tool call, a slow provider, a retry. Those are the only
#: ones a caller notices.
FILLER_AFTER_S = 1.2

#: Phrases spoken while the answer is still being produced.
#:
#: Short, because the answer should land immediately after — anything longer
#: than about a second and the filler becomes the delay it was meant to cover.
#: Varied in shape as well as wording: identical rhythm every time is what makes
#: a voice sound synthetic, even when the words differ.
FILLER_PHRASES: tuple[str, ...] = (
    "One moment.",
    "Let me check that.",
    "Give me a second.",
    "Looking into that now.",
    "Just a moment, please.",
    "Okay, checking.",
    "Bear with me a second.",
    "Right, let me look.",
    "Thanks — checking that for you.",
    "Hold on a moment, please.",
    "Let me pull that up.",
    "One second while I check.",
)


class ThinkingFiller:
    """Speaks a short phrase when a reply is taking long enough to notice.

    Attached to the session's state changes: ``thinking`` arms a timer,
    anything else disarms it. The timer is cancelled rather than allowed to
    fire late, so a fast answer produces no filler at all.
    """

    def __init__(
        self,
        session: AgentSession,
        *,
        after_seconds: float = FILLER_AFTER_S,
        phrases: tuple[str, ...] = FILLER_PHRASES,
    ) -> None:
        self._session = session
        self._after = after_seconds
        self._phrases = phrases
        self._task: asyncio.Task[None] | None = None
        self._last: str | None = None
        self._spoken = 0

    @property
    def spoken(self) -> int:
        """How many fillers this call needed. Reported in the call summary."""
        return self._spoken

    def attach(self) -> None:
        self._session.on("agent_state_changed", self._on_state)

    def _on_state(self, event: Any) -> None:
        if getattr(event, "new_state", None) == "thinking":
            self._arm()
        else:
            self._disarm()

    def _arm(self) -> None:
        self._disarm()
        self._task = asyncio.create_task(self._speak_after_delay())

    def _disarm(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    def _choose(self) -> str:
        """A phrase that is not the one used last."""
        if len(self._phrases) < 2:
            return self._phrases[0]
        options = [p for p in self._phrases if p != self._last]
        # This picks a pleasantry to say out loud. Nothing about it is a
        # secret, so a CSPRNG here would be cargo cult.
        return random.choice(options)  # noqa: S311

    async def _speak_after_delay(self) -> None:
        try:
            await asyncio.sleep(self._after)

            # Re-checked after the wait, not before it: the answer usually
            # arrives during this sleep, and speaking then would talk over it.
            if self._session.agent_state != "thinking":
                return

            # `agent_state` lags the speech handle. On a real call the reply had
            # already begun while the state still read "thinking", so the state
            # check alone passed and the filler was queued *behind* the answer.
            # The handle is the earlier signal, so it is the one that decides.
            if getattr(self._session, "current_speech", None) is not None:
                logger.debug("filler_skipped_reply_already_started")
                return

            phrase = self._choose()
            self._last = phrase
            self._spoken += 1

            logger.info("filler_spoken", extra={"phrase": phrase, "after_s": self._after})

            # Interruptible: if the caller carries on talking, the filler must
            # give way like any other agent speech (spec 29).
            await self._session.say(phrase, allow_interruptions=True)
        except asyncio.CancelledError:
            # The answer arrived first. Nothing to clean up and nothing worth
            # logging — this is the common case.
            raise
        except Exception:
            # A filler that fails must never end a call. It is decoration on a
            # wait, and the answer is still coming.
            logger.warning("filler_failed", exc_info=True)
