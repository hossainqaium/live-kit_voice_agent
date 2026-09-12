"""Turn-taking window (Plan 2b.7, spec 29).

LiveKit's defaults (``min_delay=0.5``, ``max_delay=3.0``) commit a turn
before a 1.1 s transcript arrives. The failing call in Plan §12.1 named
that race:

    transcript arrives after turn has been committed.
    consider raising `min_delay` in the endpointing

These numbers are fitted to the **hosted** STT measurement from that call
(1103 ms) plus the 2581 ms end-of-utterance, not to the 11832 ms local
contention sample — that variance is a 2b.9 hardware decision, not a
window to tune against.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from worker.config_loader import CallPolicy

#: Seconds of silence after the last detected speech before the turn is
#: committed. Must exceed the time the final transcript takes to arrive, or the
#: turn commits without it and the caller is ignored — the 2b.7 failure.
#:
#: **2.0 s was fitted to hosted STT at 1103 ms and is no longer what runs.**
#: Self-hosted `faster-whisper-tiny` measures 228 ms p50 / 682 ms p95
#: sequentially (`make measure-latency`), and 970-990 ms in-call across
#: consecutive turns — the in-call figure is the one that matters, since it
#: includes streaming and VAD overhead the harness does not.
#:
#: 1.4 s clears the worst in-call sample by about 400 ms. That is a deliberate
#: trade: it takes 600 ms off every single turn, and the cost of being wrong is
#: the 2b.7 symptom returning, so it is asserted in a test against the measured
#: figure rather than left as a number someone can nudge.
MIN_ENDPOINTING_DELAY_S = 1.4

#: The worst in-call transcription delay observed on consecutive turns. The
#: window is checked against this in the test suite.
OBSERVED_IN_CALL_STT_S = 0.99

#: Hard cap on waiting for the turn to end. Must exceed both ``min_delay``
#: and the 2.581 s EOU of the same call.
MAX_ENDPOINTING_DELAY_S = 6.0


def turn_handling_for(policy: CallPolicy) -> dict[str, Any]:
    """``TurnHandlingOptions`` for this call's interruption policy.

    Passed as ``AgentSession(turn_handling=...)``. The deprecated top-level
    ``allow_interruptions`` / ``min_interruption_words`` kwargs are ignored
    once ``turn_handling`` is set, so interruption belongs here too.
    """
    return {
        # VAD, not the default TurnDetector: constructing that detector's
        # local EOT model is 537 ms of synchronous work inside
        # AgentSession.start (Plan 2b.11). With min_delay=2.0 the model
        # cannot commit a turn earlier than the window anyway.
        "turn_detection": "vad",
        "endpointing": {
            "mode": "fixed",
            "min_delay": MIN_ENDPOINTING_DELAY_S,
            "max_delay": MAX_ENDPOINTING_DELAY_S,
        },
        "interruption": {
            "enabled": policy.interruption_enabled,
            "min_words": policy.interruption_min_words,
        },
        # Start generating the reply during the endpointing window instead of
        # after it. The window is 1.4 s of pure waiting (above), and the LLM
        # needs about a second after it — overlapping the two is the only way
        # to remove a wait rather than decorate it.
        #
        # **Measured over six turns each way, and it does not make calls
        # faster.** Median 3332 -> 3154 ms, but the mean moves only 3208 ->
        # 3163, and the baseline's best turn beats every preemptive one. What
        # it does is halve the spread: stdev 393 -> 178. Steadier, not quicker,
        # which for a voice agent is still worth having.
        #
        # It cannot do more here, and the reason is in the same numbers: the
        # transcript does not arrive until ~1230 ms and the window closes at
        # 1400, so there are about 170 ms to speculate into. Preemptive
        # generation needs a transcript to generate from.
        #
        # `preemptive_tts` is left **off**. It would speculate on synthesis
        # too, and this host's self-hosted TTS already saturates near two
        # concurrent requests (Plan §12.1) — spending CPU on speech that may be
        # discarded is the wrong trade on a box that cannot serve two real
        # calls. Revisit with a GPU or hosted TTS.
        "preemptive_generation": {
            "enabled": True,
            "preemptive_tts": False,
        },
    }
