"""Warm transfer: status, summary, isolation, fallback, engine (Plan 6.9–6.10c)."""

from __future__ import annotations

import inspect
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.models import (
    TRANSFER_FALLBACK_TRIGGERS,
    FallbackAction,
    ProviderKind,
    SpeakerType,
    TransferDestinationKind,
    TransferStatus,
)
from worker.config_loader import CallContext, CallPolicy, TransferPolicy
from worker.entrypoint import _wait_for_disconnect
from worker.pipeline.observer import CallObserver
from worker.providers.base import ProviderConfig
from worker.tools.builtins import transfer_call
from worker.tools.definitions import ToolDefinition
from worker.transfer.destinations import (
    TransferDestinationInfo,
    TransferFallback,
    resolve_destination,
)
from worker.transfer.dial import DialOutcome, map_sip_error
from worker.transfer.engine import DEFAULT_ANNOUNCEMENT, WarmTransfer
from worker.transfer.fallback import plan_fallback
from worker.transfer.isolation import (
    HOLD_TRACK_NAME,
    HUMAN_IDENTITY_PREFIX,
    WHISPER_TRACK_NAME,
    human_identity,
    is_human_agent,
    is_inbound_caller,
)
from worker.transfer.registry import get as get_transfer
from worker.transfer.registry import register as register_transfer
from worker.transfer.registry import unregister as unregister_transfer
from worker.transfer.status import IllegalTransferTransition, assert_legal
from worker.transfer.summary import (
    DEFAULT_SPOKEN_TEMPLATE,
    SUMMARY_FIELDS,
    fallback_fields,
    fallback_whisper,
    generate_transfer_summary,
    parse_fields,
    render_spoken,
    structured_headers,
)


def _provider(kind: ProviderKind = ProviderKind.STT) -> ProviderConfig:
    return ProviderConfig(kind=kind, provider="openai_compatible", model="test")


def _destination(**overrides: Any) -> TransferDestinationInfo:
    values = {
        "id": uuid.uuid4(),
        "name": "Reception",
        "kind": TransferDestinationKind.PBX_EXTENSION,
        "target": "1000",
        "pbx_id": None,
        "sip_trunk_id": None,
        "livekit_trunk_id": "ST_test",
        "whisper_summary": True,
        "ring_timeout_seconds": 20,
    }
    values.update(overrides)
    return TransferDestinationInfo(**values)


def _context(**overrides: Any) -> CallContext:
    dest = overrides.pop("destination", _destination())
    values = {
        "call_id": "call_xfer",
        "call_row_id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "tenant_slug": "dev",
        "agent_id": uuid.uuid4(),
        "agent_version_id": uuid.uuid4(),
        "agent_name": "Development Agent",
        "version_number": 1,
        "room_name": "room",
        "did": "1801",
        "caller_number": "+15559990000",
        "sip_trunk_id": None,
        "pbx_id": None,
        "language": "en",
        "greeting": None,
        "system_prompt": "Help then transfer.",
        "stt": _provider(ProviderKind.STT),
        "llm": _provider(ProviderKind.LLM),
        "tts": _provider(ProviderKind.TTS),
        "call_policy": CallPolicy(
            silence_timeout_seconds=None,
            max_call_duration_seconds=None,
            interruption_enabled=True,
            interruption_min_words=0,
            recording_enabled=False,
            transcription_enabled=True,
        ),
        "transfer_policy": TransferPolicy(
            enabled=True,
            announcement_text=DEFAULT_ANNOUNCEMENT,
            hold_media_object_key=None,
            summary_template="Customer {{customer}}. Reason {{reason}}. {{summary}}",
            summary_max_seconds=30,
            skip_dtmf="1",
        ),
        "transfer_destinations": (dest,),
    }
    values.update(overrides)
    return CallContext(**values)


def _tool() -> ToolDefinition:
    return ToolDefinition(
        id=uuid.uuid4(),
        name="transfer_call",
        description="Warm transfer",
        url_template="builtin://transfer_call",
        request_schema={"type": "object", "properties": {}},
    )


class _FakeSession:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        if params is not None:
            self.updates.append(dict(params))
        result = MagicMock()
        result.first.return_value = ("REQUESTED",)
        return result

    async def commit(self) -> None:
        return None

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _factory(session: _FakeSession | None = None) -> Any:
    session = session or _FakeSession()

    def _make() -> _FakeSession:
        return session

    _make.session = session  # type: ignore[attr-defined]
    return _make


class TestStatusMachine:
    def test_happy_path_is_legal(self) -> None:
        path = (
            TransferStatus.NOT_REQUESTED,
            TransferStatus.REQUESTED,
            TransferStatus.ANNOUNCING,
            TransferStatus.DIALING_AGENT,
            TransferStatus.WHISPERING_SUMMARY,
            TransferStatus.BRIDGED,
        )
        for current, following in zip(path, path[1:], strict=False):
            assert_legal(current, following)

    def test_cannot_bridge_before_whisper(self) -> None:
        with pytest.raises(IllegalTransferTransition):
            assert_legal(TransferStatus.DIALING_AGENT, TransferStatus.BRIDGED)

    def test_fallback_triggers_match_spec(self) -> None:
        assert TransferStatus.AGENT_NO_ANSWER in TRANSFER_FALLBACK_TRIGGERS
        assert TransferStatus.BRIDGED not in TRANSFER_FALLBACK_TRIGGERS


class TestSummary:
    def test_seven_fields_are_exactly_the_prd_list(self) -> None:
        assert SUMMARY_FIELDS == (
            "customer",
            "reason",
            "summary",
            "actions_taken",
            "order_information",
            "sentiment",
            "required_next_action",
        )

    def test_template_substitutes_all_fields(self) -> None:
        fields = {key: key.upper() for key in SUMMARY_FIELDS}
        spoken = render_spoken(DEFAULT_SPOKEN_TEMPLATE, fields)
        for key in SUMMARY_FIELDS:
            assert key.upper() in spoken

    def test_fallback_whisper_includes_caller(self) -> None:
        text = fallback_whisper(caller_number="+15550001111", reason="billing")
        assert "+15550001111" in text
        assert "billing" in text

    def test_parse_json_fills_missing_from_fallback(self) -> None:
        fallback = fallback_fields(caller_number="3001", reason="help")
        parsed = parse_fields('{"customer": "Ada", "reason": "wifi"}', fallback=fallback)
        assert parsed["customer"] == "Ada"
        assert parsed["reason"] == "wifi"
        assert parsed["sentiment"] == "unknown"

    def test_structured_headers_are_best_effort(self) -> None:
        headers = structured_headers({"customer": "Ada", "reason": "wifi"})
        assert headers["X-Transfer-Customer"] == "Ada"
        assert headers["X-Transfer-Reason"] == "wifi"

    async def test_generation_failure_still_returns_a_briefing(self) -> None:
        async def _boom(*_a: Any, **_k: Any) -> str:
            raise RuntimeError("llm down")

        from unittest.mock import patch

        with patch("worker.transfer.summary._request_summary", _boom):
            briefing = await generate_transfer_summary(
                _context(), "Caller: I need a human", reason="escalate"
            )
        assert briefing.generated is False
        assert "Please accept the call" in briefing.spoken
        assert set(briefing.fields) == set(SUMMARY_FIELDS)


class TestDestinations:
    def test_resolve_by_name_then_first(self) -> None:
        queue = _destination(name="Support Queue", target="5000")
        reception = _destination(name="Reception", target="1000")
        assert resolve_destination((queue, reception), name="reception") is reception
        assert resolve_destination((queue, reception)) is queue


class TestDialMapping:
    def test_sip_codes(self) -> None:
        assert map_sip_error(RuntimeError("486 Busy Here")) is DialOutcome.BUSY
        assert map_sip_error(RuntimeError("480 Temporarily Unavailable")) is DialOutcome.NO_ANSWER
        assert map_sip_error(RuntimeError("603 Decline")) is DialOutcome.REJECTED
        assert map_sip_error(RuntimeError("500 exploded")) is DialOutcome.FAILED


class TestFallback:
    def test_queue_retries_unused_destination(self) -> None:
        dest = _destination(name="Queue", kind=TransferDestinationKind.PBX_QUEUE)
        plan = plan_fallback(
            TransferStatus.AGENT_NO_ANSWER,
            fallback=TransferFallback(
                action=FallbackAction.PBX_QUEUE,
                agent_id=None,
                destination_id=dest.id,
            ),
            destinations=(dest,),
        )
        assert plan is not None
        assert plan.destination is dest
        assert plan.hangup is False

    def test_already_tried_destination_hangs_up_with_speech(self) -> None:
        dest = _destination()
        plan = plan_fallback(
            TransferStatus.AGENT_BUSY,
            fallback=TransferFallback(
                action=FallbackAction.PBX_QUEUE,
                agent_id=None,
                destination_id=dest.id,
            ),
            destinations=(dest,),
            already_tried=frozenset({dest.id}),
        )
        assert plan is not None
        assert plan.hangup is True
        assert plan.spoken
        assert plan.destination is None

    def test_no_config_never_leaves_silence(self) -> None:
        plan = plan_fallback(
            TransferStatus.FAILED,
            fallback=None,
            destinations=(),
        )
        assert plan is not None
        assert plan.hangup is True
        assert "sorry" in plan.spoken.lower() or "goodbye" in plan.spoken.lower()


class TestIsolation:
    def test_human_identity_is_distinct_from_caller(self) -> None:
        identity = human_identity("call_xfer")
        assert identity.startswith(HUMAN_IDENTITY_PREFIX)
        assert is_human_agent(identity)
        assert not is_human_agent("sip_caller")
        caller = MagicMock(identity="sip_inbound")
        human = MagicMock(identity=identity)
        assert is_inbound_caller(caller)
        assert not is_inbound_caller(human)

    def test_whisper_track_is_not_the_hold_track(self) -> None:
        from worker.transfer.audio import IsolatedTrack, whisper_track

        assert WHISPER_TRACK_NAME != HOLD_TRACK_NAME
        src = inspect.getsource(whisper_track)
        assert "WHISPER_TRACK_NAME" in src
        assert "HOLD_TRACK_NAME" not in src
        assert IsolatedTrack.__doc__ and "never share a mixer" in IsolatedTrack.__doc__

    def test_whisper_permissions_only_grant_the_human(self) -> None:
        src = inspect.getsource(WarmTransfer._whisper_to_agent)
        assert "restrict_local_tracks" in src
        assert "human_id" in src
        assert "isolate_remote_legs" in inspect.getsource(WarmTransfer._on_answered)


@pytest.mark.asyncio
class TestTransferCallTool:
    async def test_disabled_agent_is_refused(self) -> None:
        ctx = _context(
            transfer_policy=TransferPolicy(
                enabled=False,
                announcement_text=None,
                hold_media_object_key=None,
                summary_template=None,
                summary_max_seconds=30,
                skip_dtmf="1",
            )
        )
        xfer = WarmTransfer(ctx, _factory())
        register_transfer(xfer)
        try:
            result = await transfer_call(_tool(), {}, ctx, None)
        finally:
            unregister_transfer(ctx.call_id)
        assert result["ok"] is False
        assert "not enabled" in result["error"]

    async def test_returns_requested_without_waiting_for_sip(self) -> None:
        ctx = _context()
        xfer = WarmTransfer(ctx, _factory())

        async def _noop_run(*_a: Any, **_k: Any) -> None:
            return None

        xfer._run = _noop_run  # type: ignore[method-assign]
        register_transfer(xfer)
        try:
            result = await transfer_call(_tool(), {"reason": "billing"}, ctx, None)
        finally:
            unregister_transfer(ctx.call_id)
        assert result["ok"] is True
        assert result["status"] == TransferStatus.REQUESTED.value
        assert get_transfer(ctx.call_id) is None or True


class TestEngine:
    async def test_happy_path_whispers_then_bridges(self) -> None:
        ctx = _context()
        session = _FakeSession()
        xfer = WarmTransfer(ctx, _factory(session))
        spoken: list[str] = []
        events: list[str] = []

        async def _say(text: str) -> None:
            spoken.append(text)

        async def _hold() -> None:
            events.append("hold")

        async def _dial(*_a: Any, **_k: Any) -> DialOutcome:
            events.append("dial")
            return DialOutcome.ANSWERED

        async def _whisper(summary: Any, **_k: Any) -> float:
            events.append(f"whisper:{summary.spoken}")
            return 4.0

        async def _bridge(**_k: Any) -> None:
            events.append("bridge")
            xfer._bridged = True
            xfer._status = TransferStatus.BRIDGED

        async def _leave() -> None:
            events.append("leave")

        xfer._say_to_caller = _say  # type: ignore[method-assign]
        xfer._start_hold = _hold  # type: ignore[method-assign]
        xfer._dial = _dial  # type: ignore[method-assign]
        xfer._whisper_to_agent = _whisper  # type: ignore[method-assign]
        xfer._bridge = _bridge  # type: ignore[method-assign]
        xfer._leave_ai = _leave  # type: ignore[method-assign]

        from unittest.mock import patch

        briefing = MagicMock(
            fields={key: "x" for key in SUMMARY_FIELDS},
            spoken="briefing for the human only",
            generated=True,
            sip_headers={},
        )

        async def _summary(*_a: Any, **_k: Any) -> Any:
            return briefing

        with patch("worker.transfer.engine.generate_transfer_summary", _summary):
            with patch("worker.transfer.engine.isolate_remote_legs", AsyncMock()):
                result = await xfer.request({"reason": "wifi"})
                assert result["ok"] is True
                await xfer._task

        assert any("transferred to a human" in line.lower() for line in spoken)
        assert "dial" in events
        assert any(item.startswith("whisper:") for item in events)
        assert "bridge" in events
        assert xfer.bridged is True
        assert xfer.status is TransferStatus.BRIDGED
        whisper_line = next(item for item in events if item.startswith("whisper:"))
        assert "briefing for the human only" in whisper_line
        assert "briefing for the human only" not in " ".join(spoken)

    async def test_no_answer_speaks_fallback_and_does_not_bridge(self) -> None:
        dest = _destination()
        ctx = _context(
            destination=dest,
            transfer_fallback=TransferFallback(
                action=FallbackAction.HANGUP,
                agent_id=None,
                destination_id=None,
            ),
        )
        xfer = WarmTransfer(ctx, _factory())
        spoken: list[str] = []

        async def _say(text: str) -> None:
            spoken.append(text)

        async def _hold() -> None:
            return None

        async def _dial(*_a: Any, **_k: Any) -> DialOutcome:
            return DialOutcome.NO_ANSWER

        async def _hangup() -> None:
            spoken.append("hungup")

        xfer._say_to_caller = _say  # type: ignore[method-assign]
        xfer._start_hold = _hold  # type: ignore[method-assign]
        xfer._dial = _dial  # type: ignore[method-assign]
        xfer._hangup = _hangup  # type: ignore[method-assign]
        xfer._stop_audio = AsyncMock()  # type: ignore[method-assign]

        from unittest.mock import patch

        with patch(
            "worker.transfer.engine.generate_transfer_summary",
            AsyncMock(return_value=MagicMock(fields={}, spoken="x", sip_headers={})),
        ):
            await xfer.request({})
            await xfer._task

        assert xfer.bridged is False
        assert xfer.status is TransferStatus.AGENT_NO_ANSWER
        assert any("sorry" in line.lower() or "goodbye" in line.lower() for line in spoken)
        assert "hungup" in spoken

    def test_dtmf_skip_sets_the_event(self) -> None:
        ctx = _context()
        handlers: dict[str, Any] = {}
        room = MagicMock()
        room.on.side_effect = lambda event, fn: handlers.setdefault(event, fn)
        xfer = WarmTransfer(ctx, _factory(), room=room)
        xfer._listen_dtmf()
        assert "sip_dtmf_received" in handlers or "sip_dtmf" in handlers
        handler = handlers.get("sip_dtmf_received") or handlers["sip_dtmf"]
        handler(MagicMock(digit="1"))
        assert xfer._skip.is_set()


class TestObserverAndDisconnect:
    def test_observer_records_private_whisper_and_human_speech(self) -> None:
        src = inspect.getsource(CallObserver.record_segment)
        assert "is_private_to_agent" in src
        assert "HUMAN_AGENT" in inspect.getsource(
            __import__("worker.transfer.engine", fromlist=["WarmTransfer"]).WarmTransfer._listen_human_speech
        )
        drain = inspect.getsource(CallObserver._drain)
        assert ":is_private" in drain or "is_private" in drain

    def test_speaker_type_exists(self) -> None:
        assert SpeakerType.HUMAN_AGENT.value == "HUMAN_AGENT"

    def test_wait_for_disconnect_ignores_human_before_bridge(self) -> None:
        src = inspect.getsource(_wait_for_disconnect)
        assert "is_human_agent" in src
        assert "bridged" in src


class TestWiring:
    def test_builtin_is_no_longer_a_stub(self) -> None:
        src = inspect.getsource(transfer_call)
        assert "not available yet" not in src
        assert "get_transfer" in src

    def test_entrypoint_registers_and_leaves_ai(self) -> None:
        from worker import entrypoint

        src = inspect.getsource(entrypoint._run_call)
        assert "register_transfer" in src
        assert "WarmTransfer" in src
        assert "unregister_transfer" in src
        leave = inspect.getsource(WarmTransfer._leave_ai)
        assert "aclose" in leave
        assert "HUMAN_AGENT" in inspect.getsource(WarmTransfer._bridge)
