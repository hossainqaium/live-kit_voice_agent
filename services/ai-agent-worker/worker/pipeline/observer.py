"""Per-turn observability for a call (spec 40, 56, 57, 58).

This module is what makes a conversation visible from the outside. Without it a
call can only be judged by whether it connected — which is how a completely
broken LLM once produced calls that looked healthy end to end: the greeting is
spoken by the session rather than the model, so connection, state machine and
duration all appeared normal while every turn failed.

Three responsibilities:

* **Transcripts** (spec 40) — speaker, timestamp, text and confidence, for both
  the caller and the AI, persisted to ``call_transcript_segments``.
* **Latency** (spec 56) — the six required measurements, taken from the metrics
  the LiveKit session already emits rather than re-derived from timestamps.
* **Turn events** (spec 58) — one structured log line and one ``call_events``
  row per turn, carrying the latency breakdown.

The hard constraint is that **none of this may touch the audio loop.** LiveKit
dispatches its events on the same loop that moves audio, and the worker already
logs `event loop blocked for Nms` warnings under load. Every handler here is
therefore synchronous, does no I/O, and only enqueues; a background task does
the database work.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from shared.logging import get_logger
from shared.models import SpeakerType
from shared.telemetry import VoiceMetrics

logger = get_logger(__name__)

#: Queue capacity. A turn produces a handful of items, so this holds a long
#: conversation's backlog. If the database stalls we drop the overflow and say
#: so, because blocking the audio loop to preserve a transcript row would
#: degrade the actual call.
_QUEUE_LIMIT = 512

#: How long the flusher waits to accumulate a batch. Long enough that a busy
#: turn becomes one round trip, short enough that a transcript is queryable
#: while the call is still up.
_FLUSH_INTERVAL_SECONDS = 1.0

#: Rows per insert batch.
_BATCH_SIZE = 50


def _positive(value: Any) -> float | None:
    """Return a latency only when it is a real measurement.

    The session reports ``-1`` for a latency that does not apply — notably the
    TTS time-to-first-byte of a greeting spoken by ``session.say()``, which has
    no preceding caller turn to measure from. Treating that as a measurement
    would put negative values in a latency histogram and make the composite
    sums nonsense.
    """
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if numeric >= 0 else None


def _speaker_for_role(role: str) -> SpeakerType | None:
    """Map a chat role to a spec-40 speaker type.

    ``system`` and ``tool`` roles are deliberately unmapped: they are prompt
    scaffolding and tool plumbing, not utterances anyone spoke, and putting
    them in a transcript would misrepresent the conversation.
    """
    if role == "user":
        return SpeakerType.CALLER
    if role == "assistant":
        return SpeakerType.AI
    return None


@dataclass(slots=True)
class _Turn:
    """Latency pieces for one turn, correlated by the session's speech id.

    The session emits end-of-utterance, LLM and TTS metrics separately. The
    composite measurements spec 56 asks for — time to first response, time to
    first audio, end-to-end — are sums across them, so they can only be
    computed once the pieces have arrived.
    """

    speech_id: str
    #: Delay from the caller stopping speaking to the turn being recognised as
    #: finished. This is turn detection, not transcription.
    end_of_utterance_delay: float | None = None
    transcription_delay: float | None = None
    llm_ttft: float | None = None
    tts_ttfb: float | None = None
    tts_audio_duration: float | None = None
    created_at: float = field(default_factory=time.monotonic)

    @property
    def time_to_first_response(self) -> float | None:
        """End of caller speech to the model's first token (spec 56)."""
        if self.end_of_utterance_delay is None or self.llm_ttft is None:
            return None
        return self.end_of_utterance_delay + self.llm_ttft

    @property
    def time_to_first_audio(self) -> float | None:
        """End of caller speech to the first audio frame the caller hears.

        The number that actually corresponds to perceived lag.
        """
        base = self.time_to_first_response
        if base is None or self.tts_ttfb is None:
            return None
        return base + self.tts_ttfb

    @property
    def end_to_end(self) -> float | None:
        """End of caller speech to the end of the agent's utterance (spec 56)."""
        base = self.time_to_first_audio
        if base is None or self.tts_audio_duration is None:
            return None
        return base + self.tts_audio_duration

    def is_complete(self) -> bool:
        return self.end_to_end is not None


class CallObserver:
    """Records transcripts, latency and turn events for one call.

    Attach it to an ``AgentSession`` before starting the session, then
    ``start()`` it and ``aclose()`` it with the call.
    """

    def __init__(
        self,
        session_factory: Any,
        *,
        call_row_id: uuid.UUID,
        tenant_id: uuid.UUID,
        call_id: str,
        agent_id: str | None,
        metrics: VoiceMetrics,
        transcription_enabled: bool = True,
    ) -> None:
        self._factory = session_factory
        self._call_row_id = call_row_id
        self._tenant_id = tenant_id
        self._call_id = call_id
        #: Prometheus label. The UUID would make one time series per agent, so
        #: a readable name is used and cardinality stays bounded by the tenant's
        #: agent count.
        self._agent_label = agent_id or "unknown"
        self._metrics = metrics
        self._transcription_enabled = transcription_enabled

        self._queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=_QUEUE_LIMIT)
        self._flusher: asyncio.Task[None] | None = None
        self._transcript_row_id: uuid.UUID | None = None
        self._sequence = 0
        self._turns: dict[str, _Turn] = {}
        self._dropped = 0
        self._interruptions = 0
        self._segments_written = 0

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Create the transcript header and start the background flusher."""
        if self._transcription_enabled:
            self._transcript_row_id = uuid.uuid4()
            async with self._factory() as session:
                await session.execute(
                    text(
                        """
                        INSERT INTO call_transcripts
                            (id, tenant_id, call_id, segment_count, created_at, updated_at)
                        VALUES (:id, :tenant_id, :call_id, 0, now(), now())
                        ON CONFLICT (call_id) DO NOTHING
                        """
                    ),
                    {
                        "id": self._transcript_row_id,
                        "tenant_id": self._tenant_id,
                        "call_id": self._call_row_id,
                    },
                )
                await session.commit()

        self._flusher = asyncio.create_task(self._flush_loop(), name="call-observer-flush")

    async def aclose(self) -> None:
        """Drain what is queued and stop.

        Called in the call's ``finally``, so it must not raise: losing a
        transcript is bad, but masking the real reason a call ended is worse.
        """
        if self._flusher is not None:
            self._flusher.cancel()
            try:
                await self._flusher
            except asyncio.CancelledError:
                # Expected: we just cancelled it.
                pass
            except Exception:
                logger.exception("observer_flusher_failed")
            self._flusher = None

        try:
            await self._drain()
        except Exception:
            logger.exception("observer_final_flush_failed")

        if self._dropped:
            logger.warning(
                "observer_dropped_records",
                extra={"dropped": self._dropped, "reason": "queue full"},
            )

        logger.info(
            "call_observability_summary",
            extra={
                "transcript_segments": self._segments_written,
                "interruptions": self._interruptions,
                "turns_measured": sum(1 for t in self._turns.values() if t.is_complete()),
            },
        )

    # ------------------------------------------------------------------ #
    # Event wiring
    # ------------------------------------------------------------------ #

    def attach(self, session: Any) -> None:
        """Register handlers on an ``AgentSession``.

        Handlers are synchronous and I/O-free by design; see the module
        docstring.
        """
        session.on("conversation_item_added", self._on_conversation_item)
        session.on("metrics_collected", self._on_metrics)
        session.on("overlapping_speech", self._on_overlapping_speech)
        session.on("error", self._on_error)

    def _enqueue(self, kind: str, payload: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait((kind, payload))
        except asyncio.QueueFull:
            # Counted rather than logged per occurrence: if the database is
            # stalled this would fire on every turn and bury the cause.
            self._dropped += 1

    # ------------------------------------------------------------------ #
    # Handlers
    # ------------------------------------------------------------------ #

    def _on_conversation_item(self, event: Any) -> None:
        """Persist one utterance (spec 40)."""
        if not self._transcription_enabled:
            return

        item = getattr(event, "item", None)
        if item is None:
            return

        speaker = _speaker_for_role(str(getattr(item, "role", "")))
        if speaker is None:
            return

        content = getattr(item, "content", None)
        # Content is a list of parts; non-text parts (images, audio) have no
        # place in a spoken transcript.
        if isinstance(content, list):
            text_value = " ".join(part for part in content if isinstance(part, str)).strip()
        else:
            text_value = str(content or "").strip()

        if not text_value:
            return

        self._sequence += 1
        self._enqueue(
            "segment",
            {
                "id": uuid.uuid4(),
                "sequence": self._sequence,
                "speaker": speaker.value,
                "text": text_value,
                "confidence": getattr(item, "transcript_confidence", None),
                "spoken_at": datetime.now(UTC),
                "interrupted": bool(getattr(item, "interrupted", False)),
            },
        )

    def _on_metrics(self, event: Any) -> None:
        """Translate session metrics into the spec-56 measurements."""
        m = getattr(event, "metrics", None)
        if m is None:
            return

        kind = type(m).__name__
        labels = {"tenant_id": str(self._tenant_id), "agent_id": self._agent_label}

        if kind == "STTMetrics":
            duration = _positive(getattr(m, "duration", None))
            if duration is not None:
                self._metrics.stt_latency.labels(**labels, provider=self._label_of(m)).observe(
                    duration
                )
            return

        if kind == "EOUMetrics":
            turn = self._turn_for(getattr(m, "speech_id", None))
            if turn is not None:
                turn.end_of_utterance_delay = _positive(getattr(m, "end_of_utterance_delay", None))
                turn.transcription_delay = _positive(getattr(m, "transcription_delay", None))
            return

        if kind == "LLMMetrics":
            ttft = _positive(getattr(m, "ttft", None))
            if ttft is not None:
                self._metrics.llm_first_token_latency.labels(
                    **labels, provider=self._label_of(m), model=self._label_of(m)
                ).observe(ttft)
            turn = self._turn_for(getattr(m, "speech_id", None))
            if turn is not None:
                turn.llm_ttft = ttft
            return

        if kind == "TTSMetrics":
            ttfb = _positive(getattr(m, "ttfb", None))
            if ttfb is not None:
                self._metrics.tts_first_audio_latency.labels(
                    **labels, provider=self._label_of(m)
                ).observe(ttfb)
            turn = self._turn_for(getattr(m, "speech_id", None))
            if turn is not None:
                turn.tts_ttfb = ttfb
                turn.tts_audio_duration = _positive(getattr(m, "audio_duration", None))
                # TTS is the last piece, so the composites can be emitted now.
                self._complete_turn(turn, labels)
            return

        if kind == "InterruptionMetrics":
            count = getattr(m, "num_interruptions", 0) or 0
            if count > self._interruptions:
                new = count - self._interruptions
                self._interruptions = count
                self._metrics.interruptions_total.labels(**labels).inc(new)
            return

    def _on_overlapping_speech(self, event: Any) -> None:
        """Record barge-in (spec 29).

        The caller talking over the agent is the event that matters for voice
        quality, and its detection delay is how responsive barge-in feels.
        """
        if not getattr(event, "is_interruption", False):
            return
        self._enqueue(
            "event",
            {
                "event_type": "caller_interrupted_agent",
                "payload": {
                    "detection_delay_seconds": getattr(event, "detection_delay", None),
                    "overlap_duration_seconds": getattr(event, "total_duration", None),
                    "agent_stopped": bool(getattr(event, "agent_ended", False)),
                },
            },
        )
        logger.info(
            "caller_interrupted_agent",
            extra={"detection_delay_seconds": getattr(event, "detection_delay", None)},
        )

    def _on_error(self, event: Any) -> None:
        """Record a pipeline error against the call.

        A provider failing mid-conversation is the single most useful thing to
        have on the call record afterwards (spec 55).
        """
        error = getattr(event, "error", None)
        source = getattr(event, "source", None)
        detail = str(error)[:500] if error is not None else "unknown"
        self._enqueue(
            "event",
            {
                "event_type": "pipeline_error",
                "payload": {"source": str(source)[:120], "error": detail},
            },
        )
        logger.error("pipeline_error", extra={"source": str(source)[:120], "detail": detail})

    # ------------------------------------------------------------------ #
    # Turn accounting
    # ------------------------------------------------------------------ #

    @staticmethod
    def _label_of(m: Any) -> str:
        """Provider label from a metrics object, e.g. ``openai.LLM``."""
        return str(getattr(m, "label", None) or "unknown")[:64]

    def _turn_for(self, speech_id: str | None) -> _Turn | None:
        if not speech_id:
            return None
        turn = self._turns.get(speech_id)
        if turn is None:
            # Bound the map: a long call would otherwise accumulate a turn per
            # exchange for the life of the call.
            if len(self._turns) > 64:
                oldest = min(self._turns.values(), key=lambda t: t.created_at)
                self._turns.pop(oldest.speech_id, None)
            turn = _Turn(speech_id=speech_id)
            self._turns[speech_id] = turn
        return turn

    def _complete_turn(self, turn: _Turn, labels: dict[str, str]) -> None:
        """Emit the composite latencies and one turn event.

        Skipped entirely when there was no caller utterance to measure from:
        the greeting is spoken by ``session.say()``, so it produces TTS metrics
        with no end-of-utterance delay. Logging it as a turn with null
        latencies makes it look like measurement is broken.
        """
        if turn.end_of_utterance_delay is None:
            logger.debug(
                "agent_speech_without_caller_turn",
                extra={"tts_first_audio_ms": _ms(turn.tts_ttfb)},
            )
            return

        if turn.time_to_first_response is not None:
            self._metrics.time_to_first_response.labels(**labels).observe(
                turn.time_to_first_response
            )
        if turn.time_to_first_audio is not None:
            self._metrics.time_to_first_audio.labels(**labels).observe(turn.time_to_first_audio)
        if turn.end_to_end is not None:
            self._metrics.end_to_end_latency.labels(**labels).observe(turn.end_to_end)

        breakdown = {
            "end_of_utterance_delay_ms": _ms(turn.end_of_utterance_delay),
            "transcription_delay_ms": _ms(turn.transcription_delay),
            "llm_first_token_ms": _ms(turn.llm_ttft),
            "tts_first_audio_ms": _ms(turn.tts_ttfb),
            "time_to_first_response_ms": _ms(turn.time_to_first_response),
            "time_to_first_audio_ms": _ms(turn.time_to_first_audio),
            "end_to_end_ms": _ms(turn.end_to_end),
        }

        # One line per turn. This is the signal that was missing: it shows that
        # a turn happened at all, and where its time went.
        logger.info("turn_completed", extra=breakdown)

        self._enqueue("event", {"event_type": "turn_completed", "payload": breakdown})

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    async def _flush_loop(self) -> None:
        """Drain the queue periodically until cancelled."""
        while True:
            await asyncio.sleep(_FLUSH_INTERVAL_SECONDS)
            try:
                await self._drain()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed flush must not kill the task, or the rest of the
                # call would silently record nothing.
                logger.exception("observer_flush_failed")

    async def _drain(self) -> None:
        """Write everything currently queued, in batches."""
        segments: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []

        while not self._queue.empty() and len(segments) + len(events) < _BATCH_SIZE:
            kind, payload = self._queue.get_nowait()
            (segments if kind == "segment" else events).append(payload)

        if not segments and not events:
            return

        async with self._factory() as session:
            if segments and self._transcript_row_id is not None:
                await session.execute(
                    text(
                        """
                        INSERT INTO call_transcript_segments
                            (id, tenant_id, call_transcript_id, sequence, speaker,
                             spoken_at, text, confidence, is_private_to_agent,
                             created_at, updated_at)
                        VALUES
                            (:id, :tenant_id, :transcript_id, :sequence, :speaker,
                             :spoken_at, :text, :confidence, false, now(), now())
                        ON CONFLICT (call_transcript_id, sequence) DO NOTHING
                        """
                    ),
                    [
                        {
                            "id": s["id"],
                            "tenant_id": self._tenant_id,
                            "transcript_id": self._transcript_row_id,
                            "sequence": s["sequence"],
                            "speaker": s["speaker"],
                            "spoken_at": s["spoken_at"],
                            "text": s["text"],
                            "confidence": s["confidence"],
                        }
                        for s in segments
                    ],
                )
                await session.execute(
                    text(
                        """
                        UPDATE call_transcripts
                        SET segment_count = (
                                SELECT count(*) FROM call_transcript_segments
                                WHERE call_transcript_id = :transcript_id
                            ),
                            updated_at = now()
                        WHERE id = :transcript_id
                        """
                    ),
                    {"transcript_id": self._transcript_row_id},
                )
                self._segments_written += len(segments)

            if events:
                await session.execute(
                    text(
                        """
                        INSERT INTO call_events
                            (id, tenant_id, call_id, event_type, occurred_at,
                             payload, created_at, updated_at)
                        VALUES
                            (:id, :tenant_id, :call_id, :event_type, now(),
                             CAST(:payload AS jsonb), now(), now())
                        """
                    ),
                    [
                        {
                            "id": uuid.uuid4(),
                            "tenant_id": self._tenant_id,
                            "call_id": self._call_row_id,
                            "event_type": e["event_type"],
                            "payload": _json(e["payload"]),
                        }
                        for e in events
                    ],
                )

            await session.commit()


def _ms(seconds: float | None) -> float | None:
    """Seconds to milliseconds, rounded. Milliseconds read better in a log."""
    return None if seconds is None else round(seconds * 1000, 1)


def _json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, default=str)
