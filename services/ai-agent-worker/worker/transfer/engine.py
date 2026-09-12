"""Warm transfer orchestrator (spec 35–40, CR-1).

The two legs run together: the caller hears the announcement and hold media
while the human is dialled and whispered the summary. The legs are bridged
only after the whisper (or a DTMF skip). Generation of the summary must not
delay the announcement (TS-3). A failed summary still transfers (TS-4).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shared.logging import get_logger
from shared.models import CallState, SpeakerType, TransferStatus
from worker.call_state import CallStateTracker, IllegalTransitionError

if TYPE_CHECKING:
    from worker.config_loader import CallContext
from worker.transfer.audio import IsolatedTrack, hold_track, whisper_track
from worker.transfer.destinations import (
    TransferDestinationInfo,
    resolve_destination,
)
from worker.transfer.dial import DialOutcome, dial_human
from worker.transfer.fallback import plan_fallback
from worker.transfer.isolation import (
    allow_all_local_tracks,
    bridge_remote_legs,
    caller_identity,
    human_identity,
    isolate_remote_legs,
    is_human_agent,
    restrict_local_tracks,
)
from worker.transfer.status import IllegalTransferTransition, advance, patch
from worker.transfer.summary import TransferSummary, generate_transfer_summary

logger = get_logger(__name__)

DEFAULT_ANNOUNCEMENT = (
    "Your call is being transferred to a human agent. Please wait."
)

_DIAL_TO_STATUS = {
    DialOutcome.NO_ANSWER: TransferStatus.AGENT_NO_ANSWER,
    DialOutcome.BUSY: TransferStatus.AGENT_BUSY,
    DialOutcome.REJECTED: TransferStatus.AGENT_REJECTED,
    DialOutcome.FAILED: TransferStatus.FAILED,
}


class WarmTransfer:
    """One in-flight transfer for one call."""

    def __init__(
        self,
        context: "CallContext",
        factory: async_sessionmaker[AsyncSession],
        *,
        room: Any = None,
        session: Any = None,
        tts: Any = None,
        tracker: CallStateTracker | None = None,
        observer: Any = None,
        memory: Any = None,
    ) -> None:
        self._context = context
        self._factory = factory
        self._room = room
        self._session = session
        self._tts = tts
        self._tracker = tracker
        self._observer = observer
        self._memory = memory
        self._status = TransferStatus.NOT_REQUESTED
        self._task: asyncio.Task[None] | None = None
        self._hold: IsolatedTrack | None = None
        self._whisper: IsolatedTrack | None = None
        self._skip = asyncio.Event()
        self._abandoned = asyncio.Event()
        self._human_left = asyncio.Event()
        self._bridged = False
        self._dtmf_bound = False

    @property
    def call_id(self) -> str:
        return self._context.call_id

    @property
    def status(self) -> TransferStatus:
        return self._status

    @property
    def bridged(self) -> bool:
        return self._bridged

    def bind(
        self,
        *,
        room: Any | None = None,
        session: Any | None = None,
        tts: Any | None = None,
        tracker: CallStateTracker | None = None,
        observer: Any | None = None,
        memory: Any | None = None,
    ) -> None:
        if room is not None:
            self._room = room
        if session is not None:
            self._session = session
        if tts is not None:
            self._tts = tts
        if tracker is not None:
            self._tracker = tracker
        if observer is not None:
            self._observer = observer
        if memory is not None:
            self._memory = memory

    async def request(self, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Start the transfer. Returns as soon as ``REQUESTED`` is written."""
        arguments = dict(arguments or {})
        policy = self._context.transfer_policy
        if not policy.enabled:
            return {"ok": False, "error": "transfer is not enabled for this agent"}
        if self._status is not TransferStatus.NOT_REQUESTED:
            return {
                "ok": True,
                "status": self._status.value,
                "already_requested": True,
            }

        dest = resolve_destination(
            self._context.transfer_destinations,
            name=str(arguments.get("destination") or arguments.get("target") or "") or None,
        )
        if dest is None:
            return {
                "ok": False,
                "error": "no transfer destination is configured for this tenant",
            }

        try:
            await self._advance(
                TransferStatus.REQUESTED,
                destination_id=dest.id,
            )
        except IllegalTransferTransition as exc:
            return {"ok": False, "error": str(exc)}

        await self._call_state(CallState.TRANSFERRING)
        self._listen_dtmf()
        self._task = asyncio.create_task(
            self._run(dest, arguments),
            name=f"warm-transfer-{self.call_id}",
        )
        return {
            "ok": True,
            "status": TransferStatus.REQUESTED.value,
            "destination": dest.name,
        }

    async def abandon(self) -> None:
        """Caller hung up mid-transfer."""
        if self._bridged or self._status in {
            TransferStatus.BRIDGED,
            TransferStatus.ABANDONED,
            TransferStatus.NOT_REQUESTED,
        }:
            return
        self._abandoned.set()
        if self._status in TRANSFER_CAN_ABANDON:
            try:
                await self._advance(TransferStatus.ABANDONED)
            except IllegalTransferTransition:
                pass
        await self._stop_audio()

    async def aclose(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        await self._stop_audio()

    async def _run(self, dest: TransferDestinationInfo, arguments: dict[str, Any]) -> None:
        reason = str(arguments.get("reason") or "").strip() or None
        dialogue = self._dialogue(reason)
        summary_task = asyncio.create_task(
            generate_transfer_summary(self._context, dialogue, reason=reason)
        )
        tried: set = set()
        current = dest
        try:
            await self._advance(
                TransferStatus.ANNOUNCING,
                announcement_started_at=datetime.now(UTC),
            )
            await self._announce()
            await self._start_hold()

            first_dial = True
            while current is not None:
                tried.add(current.id)
                if first_dial:
                    await self._advance(
                        TransferStatus.DIALING_AGENT,
                        destination_id=current.id,
                        agent_dialed_at=datetime.now(UTC),
                    )
                    first_dial = False
                else:
                    await patch(
                        self._factory,
                        call_row_id=self._context.call_row_id,
                        tenant_id=self._context.tenant_id,
                        destination_id=current.id,
                        agent_dialed_at=datetime.now(UTC),
                        status=self._status,
                    )
                outcome = await self._dial(current, await self._peek_summary(summary_task))
                if outcome is DialOutcome.ANSWERED:
                    await self._on_answered(current, summary_task, reason)
                    return
                trigger = _DIAL_TO_STATUS[outcome]
                plan = plan_fallback(
                    trigger,
                    fallback=self._context.transfer_fallback,
                    destinations=self._context.transfer_destinations,
                    already_tried=frozenset(tried),
                )
                if plan is not None and plan.destination is not None:
                    logger.info(
                        "transfer_retrying_fallback_destination",
                        extra={"action": plan.action.value, "destination": plan.destination.name},
                    )
                    current = plan.destination
                    continue
                await self._fail(trigger, plan)
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("transfer_run_failed")
            if summary_task and not summary_task.done():
                summary_task.cancel()
            plan = plan_fallback(
                TransferStatus.FAILED,
                fallback=self._context.transfer_fallback,
                destinations=self._context.transfer_destinations,
                already_tried=frozenset(tried),
            )
            await self._fail(TransferStatus.FAILED, plan)

    async def _on_answered(
        self,
        dest: TransferDestinationInfo,
        summary_task: asyncio.Task[TransferSummary],
        reason: str | None,
    ) -> None:
        if self._abandoned.is_set():
            await self.abandon()
            return
        answered_at = datetime.now(UTC)
        human_id = human_identity(self.call_id)
        caller_id = caller_identity(self._room) if self._room is not None else None
        await isolate_remote_legs(
            room_name=self._context.room_name,
            caller_id=caller_id,
            human_id=human_id,
        )
        summary = await self._await_summary(summary_task, reason)
        await self._persist_summary(summary)

        if dest.whisper_summary:
            await self._advance(
                TransferStatus.WHISPERING_SUMMARY,
                agent_answered_at=answered_at,
                summary=summary.fields,
            )
            seconds = await self._whisper_to_agent(summary, caller_id=caller_id, human_id=human_id)
            if self._human_left.is_set() and not self._skip.is_set():
                plan = plan_fallback(
                    TransferStatus.AGENT_REJECTED,
                    fallback=self._context.transfer_fallback,
                    destinations=self._context.transfer_destinations,
                    already_tried=frozenset({dest.id}),
                )
                await self._fail(TransferStatus.AGENT_REJECTED, plan)
                return
        else:
            seconds = 0.0
            await self._advance(
                TransferStatus.WHISPERING_SUMMARY,
                agent_answered_at=answered_at,
                summary=summary.fields,
            )

        if self._abandoned.is_set():
            await self.abandon()
            return

        await self._bridge(caller_id=caller_id, human_id=human_id, whisper_seconds=seconds)

    async def _fail(self, status: TransferStatus, plan: Any) -> None:
        taken = None
        spoken = DEFAULT_ANNOUNCEMENT
        hangup = True
        if plan is not None:
            taken = plan.action.value
            spoken = plan.spoken
            hangup = plan.hangup
        try:
            await self._advance(status, fallback_taken=taken)
        except IllegalTransferTransition:
            logger.warning("transfer_fail_illegal", extra={"status": status.value})
        await self._stop_audio()
        if spoken:
            await self._say_to_caller(spoken)
        if hangup:
            await self._hangup()

    async def _bridge(
        self,
        *,
        caller_id: str | None,
        human_id: str,
        whisper_seconds: float,
    ) -> None:
        await self._stop_audio()
        await allow_all_local_tracks(self._room) if self._room is not None else None
        await bridge_remote_legs(
            room_name=self._context.room_name,
            caller_id=caller_id,
            human_id=human_id,
        )
        await self._leave_ai()
        self._bridged = True
        await self._advance(
            TransferStatus.BRIDGED,
            bridged_at=datetime.now(UTC),
            whisper_seconds=whisper_seconds,
        )
        await self._call_state(CallState.HUMAN_AGENT)
        logger.info("transfer_bridged", extra={"whisper_seconds": whisper_seconds})

    async def _announce(self) -> None:
        text = (
            (self._context.transfer_policy.announcement_text or "").strip()
            or DEFAULT_ANNOUNCEMENT
        )
        await self._say_to_caller(text)

    async def _start_hold(self) -> None:
        if self._room is None or self._tts is None:
            return
        text = (
            (self._context.transfer_policy.announcement_text or "").strip()
            or DEFAULT_ANNOUNCEMENT
        )
        track = hold_track(self._room, self._tts)
        self._hold = track
        await track.start()
        caller_id = caller_identity(self._room)
        if caller_id and track.sid:
            await restrict_local_tracks(self._room, permissions=[(caller_id, [track.sid])])
        await track.loop(text)

    async def _say_to_caller(self, text: str) -> None:
        if self._hold is not None:
            await self._hold.play(text)
            return
        if self._room is not None and self._tts is not None:
            track = hold_track(self._room, self._tts)
            self._hold = track
            await track.play(text)
            return
        session = self._session
        say = getattr(session, "say", None)
        if say is not None:
            result = say(text, allow_interruptions=False)
            if hasattr(result, "__await__"):
                await result

    async def _dial(
        self,
        dest: TransferDestinationInfo,
        summary: TransferSummary | None,
    ) -> DialOutcome:
        headers = summary.sip_headers if summary is not None else None
        metadata = None
        if summary is not None:
            metadata = json.dumps(summary.fields)
        return await dial_human(
            dest,
            room_name=self._context.room_name,
            call_id=self.call_id,
            headers=headers,
            participant_metadata=metadata,
        )

    async def _whisper_to_agent(
        self,
        summary: TransferSummary,
        *,
        caller_id: str | None,
        human_id: str,
    ) -> float:
        policy = self._context.transfer_policy
        max_seconds = max(5, int(policy.summary_max_seconds or 30))
        self._record_private(summary.spoken)
        if self._room is None or self._tts is None:
            # Tests and environments without a room still count as whispered.
            await asyncio.sleep(0)
            return 0.0
        track = whisper_track(self._room, self._tts)
        self._whisper = track
        await track.start()
        permissions: list[tuple[str, list[str]]] = []
        if human_id and track.sid:
            permissions.append((human_id, [track.sid]))
        if caller_id and self._hold is not None and self._hold.sid:
            permissions.append((caller_id, [self._hold.sid]))
        if permissions:
            await restrict_local_tracks(self._room, permissions=permissions)

        play = asyncio.create_task(track.play(summary.spoken, max_seconds=max_seconds))
        skip = asyncio.create_task(self._skip.wait())
        left = asyncio.create_task(self._human_left.wait())
        abandoned = asyncio.create_task(self._abandoned.wait())
        done, pending = await asyncio.wait(
            {play, skip, left, abandoned},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        seconds = 0.0
        if play in done and not play.cancelled():
            try:
                seconds = float(play.result())
            except Exception:
                seconds = 0.0
        else:
            seconds = min(max_seconds, 0.1)
        track.stop()
        return min(seconds, float(max_seconds))

    async def _leave_ai(self) -> None:
        """Stop the AI conversation; stay in the room so recording continues (TR-8)."""
        session = self._session
        closer = getattr(session, "aclose", None)
        if closer is not None:
            try:
                result = closer()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                logger.warning("transfer_session_close_failed", exc_info=True)
        interrupt = getattr(session, "interrupt", None)
        if interrupt is not None:
            try:
                result = interrupt()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                pass
        self._listen_human_speech()

    async def _hangup(self) -> None:
        room = self._room
        delete = getattr(room, "disconnect", None)
        if delete is not None:
            try:
                result = delete()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                logger.warning("transfer_fallback_disconnect_failed", exc_info=True)
        if self._tracker is not None and not self._tracker.is_terminal:
            try:
                await self._tracker.transition(CallState.COMPLETED)
            except IllegalTransitionError:
                pass

    async def _stop_audio(self) -> None:
        for track in (self._hold, self._whisper):
            if track is not None:
                try:
                    await track.aclose()
                except Exception:
                    logger.warning("transfer_track_close_failed", exc_info=True)
        self._hold = None
        self._whisper = None

    async def _advance(self, to_status: TransferStatus, **fields: Any) -> None:
        await advance(
            self._factory,
            call_row_id=self._context.call_row_id,
            tenant_id=self._context.tenant_id,
            from_status=self._status,
            to_status=to_status,
            **fields,
        )
        self._status = to_status
        logger.info("transfer_status_changed", extra={"status": to_status.value})

    async def _call_state(self, state: CallState) -> None:
        if self._tracker is None:
            return
        try:
            await self._tracker.transition(state)
        except IllegalTransitionError:
            logger.warning("transfer_call_state_skipped", extra={"state": state.value})

    async def _persist_summary(self, summary: TransferSummary) -> None:
        await patch(
            self._factory,
            call_row_id=self._context.call_row_id,
            tenant_id=self._context.tenant_id,
            summary=summary.fields,
            status=self._status,
        )

    async def _peek_summary(
        self, task: asyncio.Task[TransferSummary]
    ) -> TransferSummary | None:
        if task.done() and not task.cancelled():
            try:
                return task.result()
            except Exception:
                return None
        return None

    async def _await_summary(
        self,
        task: asyncio.Task[TransferSummary],
        reason: str | None,
    ) -> TransferSummary:
        try:
            return await task
        except Exception:
            logger.warning("transfer_summary_await_failed", exc_info=True)
            return await generate_transfer_summary(self._context, "", reason=reason)

    def _dialogue(self, reason: str | None) -> str:
        parts: list[str] = []
        memory = self._memory
        if memory is not None:
            summary = getattr(memory, "summary", "") or ""
            recent = getattr(memory, "recent_turns", None) or []
            if summary:
                parts.append(summary)
            parts.extend(str(turn) for turn in recent)
        if reason:
            parts.append(f"Transfer reason: {reason}")
        if not parts:
            parts.append("The caller asked to speak to a human agent.")
        return "\n".join(parts)

    def _record_private(self, text: str) -> None:
        observer = self._observer
        if observer is None or not text:
            return
        record = getattr(observer, "record_segment", None)
        if record is None:
            return
        result = record(SpeakerType.AI, text, private=True)
        if hasattr(result, "__await__"):
            asyncio.create_task(result)

    def _listen_dtmf(self) -> None:
        if self._dtmf_bound or self._room is None:
            return
        skip_digit = (self._context.transfer_policy.skip_dtmf or "1").strip() or "1"

        def _on_dtmf(*args: Any, **kwargs: Any) -> None:
            digit = _dtmf_digit(*args, **kwargs)
            if digit == skip_digit:
                logger.info("transfer_whisper_skipped_dtmf", extra={"digit": digit})
                self._skip.set()

        for event in ("sip_dtmf_received", "sip_dtmf"):
            on = getattr(self._room, "on", None)
            if on is None:
                continue
            try:
                on(event, _on_dtmf)
                self._dtmf_bound = True
            except Exception:
                continue

        def _on_participant_left(participant: Any = None, *_: Any) -> None:
            identity = getattr(participant, "identity", None)
            if is_human_agent(identity) and not self._bridged:
                self._human_left.set()

        on = getattr(self._room, "on", None)
        if on is not None:
            try:
                on("participant_disconnected", _on_participant_left)
            except Exception:
                pass

    def _listen_human_speech(self) -> None:
        room = self._room
        observer = self._observer
        if room is None or observer is None:
            return

        def _on_transcription(*args: Any, **kwargs: Any) -> None:
            identity, text = _transcription_parts(*args, **kwargs)
            if not text or not is_human_agent(identity):
                return
            record = getattr(observer, "record_segment", None)
            if record is None:
                return
            result = record(SpeakerType.HUMAN_AGENT, text, private=False)
            if hasattr(result, "__await__"):
                asyncio.create_task(result)

        on = getattr(room, "on", None)
        if on is None:
            return
        for event in ("transcription_received", "transcription"):
            try:
                on(event, _on_transcription)
            except Exception:
                continue


TRANSFER_CAN_ABANDON = frozenset(
    {
        TransferStatus.REQUESTED,
        TransferStatus.ANNOUNCING,
        TransferStatus.DIALING_AGENT,
        TransferStatus.WHISPERING_SUMMARY,
    }
)


def _dtmf_digit(*args: Any, **kwargs: Any) -> str:
    for candidate in (
        kwargs.get("digit"),
        getattr(args[0], "digit", None) if args else None,
        getattr(args[0], "code", None) if args else None,
    ):
        if candidate is not None:
            return str(candidate).strip()
    if args and isinstance(args[0], str):
        return args[0].strip()
    return ""


def _transcription_parts(*args: Any, **kwargs: Any) -> tuple[str | None, str]:
    identity = kwargs.get("identity") or kwargs.get("participant_identity")
    text = str(kwargs.get("text") or "").strip()
    if args:
        event = args[0]
        identity = identity or getattr(event, "identity", None)
        participant = getattr(event, "participant", None)
        if identity is None and participant is not None:
            identity = getattr(participant, "identity", None)
        if not text:
            segments = getattr(event, "segments", None) or getattr(event, "alternatives", None)
            if segments:
                first = segments[0]
                text = str(getattr(first, "text", first) or "").strip()
            else:
                text = str(getattr(event, "text", "") or "").strip()
    return (str(identity) if identity else None, text)
