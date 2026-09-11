"""Tests for per-turn observability (spec 40, 56).

Driven by fake event objects rather than a live call. The observer's job is to
translate what the session emits into transcript rows and latency
measurements, and that translation is exactly where the bugs were: a greeting
reported as a turn with null latencies, and a `-1` sentinel stored as if it
were a measurement.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from prometheus_client import CollectorRegistry

from shared.models import SpeakerType
from shared.telemetry import VoiceMetrics
from worker.pipeline.observer import CallObserver, _positive, _speaker_for_role

# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


@dataclass
class FakeChatMessage:
    role: str
    content: Any
    transcript_confidence: float | None = None
    interrupted: bool = False


@dataclass
class FakeItemEvent:
    item: Any


@dataclass
class FakeMetricsEvent:
    metrics: Any


class STTMetrics:
    def __init__(self, duration: float, label: str = "openai.STT") -> None:
        self.duration = duration
        self.label = label


class EOUMetrics:
    def __init__(self, speech_id: str, eou: float | None, transcription: float | None) -> None:
        self.speech_id = speech_id
        self.end_of_utterance_delay = eou
        self.transcription_delay = transcription


class LLMMetrics:
    def __init__(self, speech_id: str, ttft: float, label: str = "openai.LLM") -> None:
        self.speech_id = speech_id
        self.ttft = ttft
        self.label = label


class TTSMetrics:
    def __init__(
        self, speech_id: str, ttfb: float, audio_duration: float, label: str = "openai.TTS"
    ) -> None:
        self.speech_id = speech_id
        self.ttfb = ttfb
        self.audio_duration = audio_duration
        self.label = label


class InterruptionMetrics:
    def __init__(self, num_interruptions: int) -> None:
        self.num_interruptions = num_interruptions


@dataclass
class FakeOverlapEvent:
    is_interruption: bool
    detection_delay: float = 0.12
    total_duration: float = 0.9
    agent_ended: bool = True


def make_observer(**kwargs: Any) -> CallObserver:
    """An observer with no database behind it.

    ``start()`` is never called, so nothing is flushed; these tests assert on
    what the handlers enqueue and measure, not on persistence.
    """
    registry = CollectorRegistry()
    return CallObserver(
        session_factory=None,
        call_row_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        call_id="call_test",
        agent_id="Test Agent",
        metrics=VoiceMetrics(registry=registry),
        **kwargs,
    )


def queued(observer: CallObserver) -> list[tuple[str, dict[str, Any]]]:
    items = []
    while not observer._queue.empty():
        items.append(observer._queue.get_nowait())
    return items


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class TestPositive:
    """`-1` is the session's "not applicable", not a measurement."""

    @pytest.mark.parametrize("value", [-1, -1.0, -0.5])
    def test_negative_becomes_none(self, value: float) -> None:
        assert _positive(value) is None

    @pytest.mark.parametrize("value", [0, 0.0, 0.25, 12.5])
    def test_non_negative_is_kept(self, value: float) -> None:
        assert _positive(value) == float(value)

    @pytest.mark.parametrize("value", [None, "abc", object()])
    def test_unusable_becomes_none(self, value: object) -> None:
        assert _positive(value) is None


class TestSpeakerMapping:
    def test_roles_map_to_spec_40_speakers(self) -> None:
        assert _speaker_for_role("user") is SpeakerType.CALLER
        assert _speaker_for_role("assistant") is SpeakerType.AI

    @pytest.mark.parametrize("role", ["system", "tool", "developer", ""])
    def test_non_utterance_roles_are_excluded(self, role: str) -> None:
        """Prompt scaffolding and tool plumbing are not things anyone said."""
        assert _speaker_for_role(role) is None


# --------------------------------------------------------------------------- #
# Transcripts (spec 40)
# --------------------------------------------------------------------------- #


class TestTranscriptCapture:
    def test_caller_utterance_is_captured(self) -> None:
        observer = make_observer()
        observer._on_conversation_item(
            FakeItemEvent(
                FakeChatMessage(
                    role="user", content=["what are your hours"], transcript_confidence=0.94
                )
            )
        )
        items = queued(observer)
        assert len(items) == 1
        kind, payload = items[0]
        assert kind == "segment"
        assert payload["speaker"] == SpeakerType.CALLER.value
        assert payload["text"] == "what are your hours"
        assert payload["confidence"] == 0.94
        assert payload["sequence"] == 1

    def test_agent_utterance_is_captured(self) -> None:
        observer = make_observer()
        observer._on_conversation_item(
            FakeItemEvent(FakeChatMessage(role="assistant", content=["We open at nine."]))
        )
        _, payload = queued(observer)[0]
        assert payload["speaker"] == SpeakerType.AI.value

    def test_sequence_increments_across_turns(self) -> None:
        observer = make_observer()
        for text in ("one", "two", "three"):
            observer._on_conversation_item(
                FakeItemEvent(FakeChatMessage(role="user", content=[text]))
            )
        assert [p["sequence"] for _, p in queued(observer)] == [1, 2, 3]

    def test_string_content_is_accepted(self) -> None:
        """Content is normally a list of parts, but not always."""
        observer = make_observer()
        observer._on_conversation_item(
            FakeItemEvent(FakeChatMessage(role="user", content="plain string"))
        )
        assert queued(observer)[0][1]["text"] == "plain string"

    def test_non_text_parts_are_dropped(self) -> None:
        """An image part has no place in a spoken transcript."""
        observer = make_observer()
        observer._on_conversation_item(
            FakeItemEvent(
                FakeChatMessage(role="user", content=["hello", {"image": "..."}, "there"])
            )
        )
        assert queued(observer)[0][1]["text"] == "hello there"

    def test_empty_utterance_is_not_stored(self) -> None:
        observer = make_observer()
        for content in ([], [""], ["   "], None):
            observer._on_conversation_item(
                FakeItemEvent(FakeChatMessage(role="user", content=content))
            )
        assert queued(observer) == []

    def test_system_messages_are_not_stored(self) -> None:
        observer = make_observer()
        observer._on_conversation_item(
            FakeItemEvent(FakeChatMessage(role="system", content=["You are a helpful agent."]))
        )
        assert queued(observer) == []

    def test_nothing_is_captured_when_transcription_is_disabled(self) -> None:
        """Spec 18 makes transcription a per-agent setting."""
        observer = make_observer(transcription_enabled=False)
        observer._on_conversation_item(
            FakeItemEvent(FakeChatMessage(role="user", content=["should not be stored"]))
        )
        assert queued(observer) == []


# --------------------------------------------------------------------------- #
# Latency (spec 56)
# --------------------------------------------------------------------------- #


class TestLatencyMeasurement:
    def _run_full_turn(self, observer: CallObserver, speech_id: str = "s1") -> None:
        observer._on_metrics(FakeMetricsEvent(STTMetrics(duration=0.21)))
        observer._on_metrics(FakeMetricsEvent(EOUMetrics(speech_id, eou=0.30, transcription=0.10)))
        observer._on_metrics(FakeMetricsEvent(LLMMetrics(speech_id, ttft=0.40)))
        observer._on_metrics(
            FakeMetricsEvent(TTSMetrics(speech_id, ttfb=0.15, audio_duration=1.80))
        )

    def test_composites_are_sums_of_the_stages(self) -> None:
        observer = make_observer()
        self._run_full_turn(observer)

        turn = observer._turns["s1"]
        # end of caller speech -> first token
        assert turn.time_to_first_response == pytest.approx(0.70)
        # ... -> first audio frame the caller hears
        assert turn.time_to_first_audio == pytest.approx(0.85)
        # ... -> end of the agent's utterance
        assert turn.end_to_end == pytest.approx(2.65)
        assert turn.is_complete()

    def test_a_turn_event_is_emitted_with_the_breakdown(self) -> None:
        observer = make_observer()
        self._run_full_turn(observer)

        events = [p for k, p in queued(observer) if k == "event"]
        assert len(events) == 1
        payload = events[0]["payload"]
        assert events[0]["event_type"] == "turn_completed"
        assert payload["end_of_utterance_delay_ms"] == 300.0
        assert payload["llm_first_token_ms"] == 400.0
        assert payload["tts_first_audio_ms"] == 150.0
        assert payload["time_to_first_audio_ms"] == 850.0
        assert payload["end_to_end_ms"] == 2650.0

    def test_all_six_spec_56_metrics_are_recorded(self) -> None:
        registry = CollectorRegistry()
        metrics = VoiceMetrics(registry=registry)
        observer = CallObserver(
            session_factory=None,
            call_row_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            call_id="call_test",
            agent_id="Test Agent",
            metrics=metrics,
        )
        self._run_full_turn(observer)

        names = {
            "voice_stt_latency_seconds",
            "voice_llm_first_token_latency_seconds",
            "voice_tts_first_audio_latency_seconds",
            "voice_time_to_first_response_seconds",
            "voice_time_to_first_audio_seconds",
            "voice_end_to_end_response_latency_seconds",
        }
        recorded = {
            sample.name.rsplit("_", 1)[0]
            for metric in registry.collect()
            for sample in metric.samples
            if sample.name.endswith("_count") and sample.value > 0
        }
        assert names <= recorded, f"not recorded: {sorted(names - recorded)}"

    def test_the_greeting_is_not_reported_as_a_turn(self) -> None:
        """A greeting spoken by session.say() has no caller utterance behind it.

        The session reports ttfb = -1 for it. Reporting that as a turn produced
        a log line full of nulls and a negative latency, which read as broken
        measurement rather than as "not applicable".
        """
        observer = make_observer()
        observer._on_metrics(
            FakeMetricsEvent(TTSMetrics("greeting", ttfb=-1.0, audio_duration=2.5))
        )
        assert [k for k, _ in queued(observer)] == []

    def test_an_incomplete_turn_emits_nothing(self) -> None:
        """Composites are undefined until every stage has reported."""
        observer = make_observer()
        observer._on_metrics(FakeMetricsEvent(EOUMetrics("s2", eou=0.3, transcription=0.1)))
        observer._on_metrics(FakeMetricsEvent(LLMMetrics("s2", ttft=0.4)))
        assert queued(observer) == []
        assert not observer._turns["s2"].is_complete()

    def test_turn_map_is_bounded(self) -> None:
        """A long call must not accumulate a turn per exchange forever."""
        observer = make_observer()
        for i in range(200):
            observer._on_metrics(FakeMetricsEvent(EOUMetrics(f"s{i}", eou=0.3, transcription=0.1)))
        assert len(observer._turns) <= 65


# --------------------------------------------------------------------------- #
# Barge-in and errors
# --------------------------------------------------------------------------- #


class TestInterruptions:
    def test_caller_interruption_is_recorded(self) -> None:
        """Spec 29: the caller talking over the agent is the event that matters."""
        observer = make_observer()
        observer._on_overlapping_speech(FakeOverlapEvent(is_interruption=True))
        events = [p for k, p in queued(observer) if k == "event"]
        assert events[0]["event_type"] == "caller_interrupted_agent"
        assert events[0]["payload"]["detection_delay_seconds"] == 0.12
        assert events[0]["payload"]["agent_stopped"] is True

    def test_overlap_that_is_not_an_interruption_is_ignored(self) -> None:
        observer = make_observer()
        observer._on_overlapping_speech(FakeOverlapEvent(is_interruption=False))
        assert queued(observer) == []

    def test_interruption_counter_only_counts_the_delta(self) -> None:
        """The metric is cumulative per call, so only the increase is new."""
        observer = make_observer()
        observer._on_metrics(FakeMetricsEvent(InterruptionMetrics(num_interruptions=1)))
        observer._on_metrics(FakeMetricsEvent(InterruptionMetrics(num_interruptions=3)))
        observer._on_metrics(FakeMetricsEvent(InterruptionMetrics(num_interruptions=3)))
        assert observer._interruptions == 3


class TestPipelineErrors:
    def test_an_error_is_recorded_against_the_call(self) -> None:
        """Spec 55: a provider failing mid-call is the most useful thing to keep."""
        observer = make_observer()
        observer._on_error(
            type("E", (), {"error": RuntimeError("tts exploded"), "source": "tts"})()
        )
        events = [p for k, p in queued(observer) if k == "event"]
        assert events[0]["event_type"] == "pipeline_error"
        assert "tts exploded" in events[0]["payload"]["error"]


# --------------------------------------------------------------------------- #
# Not blocking the audio loop
# --------------------------------------------------------------------------- #


class TestAudioLoopSafety:
    def test_a_full_queue_drops_rather_than_blocks(self) -> None:
        """Blocking the audio loop to keep a transcript row would be the wrong
        trade: it degrades the call the transcript describes."""
        observer = make_observer()
        for i in range(2000):
            observer._on_conversation_item(
                FakeItemEvent(FakeChatMessage(role="user", content=[f"utterance {i}"]))
            )
        assert observer._dropped > 0
        assert observer._queue.qsize() <= 512

    def test_handlers_are_synchronous(self) -> None:
        """LiveKit dispatches on the audio loop, so a coroutine handler would
        either be dropped or awaited on the hot path."""
        observer = make_observer()
        for handler in (
            observer._on_conversation_item,
            observer._on_metrics,
            observer._on_overlapping_speech,
            observer._on_error,
        ):
            assert not asyncio.iscoroutinefunction(handler)

    def test_a_malformed_event_does_not_raise(self) -> None:
        """An exception in a handler propagates into the session's event
        dispatch, so the observer must absorb surprises rather than raise."""
        observer = make_observer()
        observer._on_conversation_item(FakeItemEvent(item=None))
        observer._on_metrics(FakeMetricsEvent(metrics=None))
        observer._on_metrics(FakeMetricsEvent(metrics=object()))
