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
#: Measured on live calls, and both obvious values were wrong:
#:
#: * **1.2 s never fires.** The thinking phase lasts only about a second —
#:   LLM first token at 988 ms — so the state leaves "thinking" before the
#:   timer expires. A filler nobody ever hears.
#: * **0.6 s fires but leaves no room.** The reply's audio starts around 2 s
#:   after the turn commits, so a filler beginning at 600 ms has to synthesise
#:   and play inside 1.4 s or it collides with the answer.
#:
#: 350 ms starts the filler while the LLM is still producing its first token,
#: giving roughly 1.6 s of room before the answer's audio is ready — enough for
#: any phrase here. The `scheduled` guard below is what keeps it off audio that
#: is already playing.
FILLER_AFTER_S = 0.35

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
        self._loop: asyncio.AbstractEventLoop | None = None
        #: State as reported by the events themselves. ``session.agent_state``
        #: did not agree with them: the timer armed on a "thinking" event, and
        #: 350 ms later the property already read something else while the
        #: session went on thinking for another two seconds. The event stream
        #: is the signal that armed the timer, so it is the one that decides
        #: whether to speak.
        self._state: str | None = None
        #: Incremented on every arm and disarm. Task creation is deferred
        #: through ``call_soon_threadsafe``, so ``_disarm`` can run before the
        #: task exists and would have nothing to cancel — the caller resumes
        #: mid-pause and gets a filler anyway. The token makes a stale creation
        #: a no-op instead.
        self._generation = 0
        self._last: str | None = None
        self._spoken = 0

    @property
    def spoken(self) -> int:
        """How many fillers this call needed. Reported in the call summary."""
        return self._spoken

    def attach(self) -> None:
        # Both events. ``user_state_changed`` is the one that can still get a
        # word in — see ``_on_user_state``. ``agent_state_changed`` is kept
        # only to disarm, so a filler never lands after the answer.
        # Captured here, where we are certainly on the session's loop.
        # ``agent_state_changed`` is dispatched from a synchronous callback that
        # is not guaranteed to run on it, and ``asyncio.create_task`` there
        # raises "no running event loop" — an exception the emitter swallows,
        # so the filler simply never spoke and logged nothing at all. That cost
        # three rounds of looking in the wrong place.
        self._loop = asyncio.get_running_loop()
        self._session.on("user_state_changed", self._on_user_state)
        self._session.on("agent_state_changed", self._on_state)

    def _on_state(self, event: Any) -> None:
        logger.debug(
            "filler_saw_state",
            extra={
                "old": getattr(event, "old_state", None),
                "new": getattr(event, "new_state", None),
            },
        )
        self._state = getattr(event, "new_state", None)
        # Never arm on "thinking": by then LiveKit has already queued the reply,
        # `session.say` has no priority argument, and the filler plays *after*
        # the answer. Measured — the reply's handle reads `scheduled` within
        # 350 ms of the turn committing, two seconds before its audio starts.
        if self._state != "thinking":
            self._disarm()

    def _on_user_state(self, event: Any) -> None:
        """Arm when the caller stops speaking, before the turn is committed.

        This is the only window where a filler can still be queued ahead of the
        reply: the endpointing window (Plan 2b.7) is over a second long, and the
        reply is not created until it closes.

        It means the filler can begin while the caller is only pausing. That is
        what a person does — "mm-hm, let me check" over the end of a sentence —
        and it is interruptible, so a caller who carries on wins.
        """
        old_state = getattr(event, "old_state", None)
        new_state = getattr(event, "new_state", None)

        # **The transition, not the state.** "listening" means the caller is
        # not speaking, which is also true right after the greeting, before
        # they have said anything at all. Arming on the state alone fired a
        # filler into that silence, and the caller's actual question then
        # arrived while the agent was mid-phrase and was discarded — the agent
        # said "One moment." and never answered, because there was no longer a
        # question to answer.
        #
        # speaking -> listening is the only transition that means "they have
        # just finished saying something".
        if old_state == "speaking" and new_state == "listening":
            self._arm()
        else:
            self._disarm()

    def _arm(self) -> None:
        self._disarm()
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        self._generation += 1
        generation = self._generation

        def _create() -> None:
            if generation != self._generation:
                # Disarmed between scheduling and running: the caller carried
                # on, or the agent started answering.
                return
            self._task = loop.create_task(self._speak_after_delay(generation))
            logger.debug("filler_armed", extra={"after_s": self._after})

        # Thread-safe, because the callback may not be on the session's loop.
        loop.call_soon_threadsafe(_create)

    def _disarm(self) -> None:
        self._generation += 1
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

    async def _speak_after_delay(self, generation: int) -> None:
        try:
            logger.debug("filler_timer_running", extra={"after_s": self._after})
            await asyncio.sleep(self._after)

            if generation != self._generation:
                return

            # Re-checked after the wait, not before it: the answer usually
            # arrives during this sleep, and speaking then would talk over it.
            if self._state == "speaking":
                logger.debug("filler_skipped_agent_already_speaking")
                return

            # `agent_state` lags the reply. On a real call the answer had
            # already begun while the state still read "thinking", so the state
            # check alone passed and the filler was queued *behind* it.
            #
            # But the mere existence of a handle is the wrong test, and testing
            # it that way suppressed every filler on every call: LiveKit creates
            # the handle when generation *starts*, which is the whole period
            # this is meant to cover. `scheduled` is the property that says
            # audio is actually going out, which is the thing not to talk over.
            # No `current_speech` check. `scheduled` is set when the reply is
            # queued, not when its audio starts, so testing it here suppressed
            # every filler on every call — which is how this was found: by
            # someone saying they had never heard one.

            phrase = self._choose()
            self._last = phrase
            self._spoken += 1

            logger.info(
                "filler_spoken",
                extra={
                    "phrase": phrase,
                    "after_s": self._after,
                    "agent_state": self._state,
                },
            )

            # Interruptible: if the caller carries on talking, the filler must
            # give way like any other agent speech (spec 29).
            await self._session.say(
                phrase,
                # **Not interruptible, and this is the load-bearing choice.**
                # The filler plays before the turn commits, so the tail of the
                # caller's utterance can land on top of it. An interruptible
                # filler is then interrupted — and LiveKit cancels the pending
                # reply along with it, which is why the agent sometimes said
                # "let me check" and then nothing at all.
                #
                # The cost is a second of overlap if the caller resumes. The
                # cost of the alternative is a lost answer, which is the worse
                # of the two by a distance.
                allow_interruptions=False,
                # Kept out of the conversation history. "One moment." is not
                # something the model said about the caller's problem, and
                # feeding it back teaches the agent that filler is part of its
                # own voice — it starts producing it in real answers.
                add_to_chat_ctx=False,
            )
        except asyncio.CancelledError:
            # The answer arrived first. Nothing to clean up and nothing worth
            # logging — this is the common case.
            raise
        except Exception:
            # A filler that fails must never end a call. It is decoration on a
            # wait, and the answer is still coming.
            logger.warning("filler_failed", exc_info=True)
