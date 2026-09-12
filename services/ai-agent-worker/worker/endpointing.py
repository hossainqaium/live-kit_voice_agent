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
#: committed. Must exceed hosted STT (1.103 s) so the transcript still joins.
MIN_ENDPOINTING_DELAY_S = 2.0

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
    }
