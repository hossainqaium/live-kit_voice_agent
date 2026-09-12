"""Endpointing window (Plan 2b.7, spec 29).

The constants exist so a 1.1 s transcript still joins its turn. These tests
assert that relationship against the Plan §12.1 measurements — if someone
lowers the window back to LiveKit's 0.5 s default, the failing call's race
returns.
"""

from __future__ import annotations

import inspect

from worker.config_loader import CallPolicy
from worker.endpointing import (
    MAX_ENDPOINTING_DELAY_S,
    MIN_ENDPOINTING_DELAY_S,
    turn_handling_for,
)
from worker.entrypoint import prewarm

# Plan §12.1, call_20260911T063710 (hosted STT, the number to tune against).
_HOSTED_STT_S = 1.103
_FAILING_EOU_S = 2.581
# LiveKit defaults that produced "transcript arrives after turn has been committed".
_LIVEKIT_MIN_DELAY_S = 0.5
_LIVEKIT_MAX_DELAY_S = 3.0


def _policy(**overrides: object) -> CallPolicy:
    values: dict[str, object] = {
        "silence_timeout_seconds": 15,
        "max_call_duration_seconds": None,
        "interruption_enabled": True,
        "interruption_min_words": 2,
        "recording_enabled": False,
        "transcription_enabled": True,
    }
    values.update(overrides)
    return CallPolicy(**values)  # type: ignore[arg-type]


class TestWindowBeatsTheFailingCall:
    def test_min_delay_exceeds_hosted_stt(self) -> None:
        assert MIN_ENDPOINTING_DELAY_S > _HOSTED_STT_S

    def test_min_delay_exceeds_livekit_default(self) -> None:
        assert MIN_ENDPOINTING_DELAY_S > _LIVEKIT_MIN_DELAY_S

    def test_max_delay_exceeds_failing_eou(self) -> None:
        assert MAX_ENDPOINTING_DELAY_S > _FAILING_EOU_S

    def test_max_delay_exceeds_min_delay(self) -> None:
        assert MAX_ENDPOINTING_DELAY_S > MIN_ENDPOINTING_DELAY_S

    def test_max_delay_exceeds_livekit_default(self) -> None:
        assert MAX_ENDPOINTING_DELAY_S > _LIVEKIT_MAX_DELAY_S


class TestTurnHandlingFor:
    def test_carries_the_window(self) -> None:
        handling = turn_handling_for(_policy())
        assert handling["endpointing"]["min_delay"] == MIN_ENDPOINTING_DELAY_S
        assert handling["endpointing"]["max_delay"] == MAX_ENDPOINTING_DELAY_S
        assert handling["endpointing"]["mode"] == "fixed"

    def test_uses_vad_not_the_local_eot_model(self) -> None:
        """2b.11: the default TurnDetector loads EOT() for 537 ms in start()."""
        handling = turn_handling_for(_policy())
        assert handling["turn_detection"] == "vad"


class TestPrewarmStaysOnSilero:
    def test_prewarm_does_not_load_the_eot_model(self) -> None:
        src = inspect.getsource(prewarm)
        assert "silero.VAD.load" in src
        assert "TurnDetector" not in src
        assert "EOT()" not in src

    def test_forwards_interruption_off(self) -> None:
        handling = turn_handling_for(_policy(interruption_enabled=False, interruption_min_words=5))
        assert handling["interruption"] == {"enabled": False, "min_words": 5}
