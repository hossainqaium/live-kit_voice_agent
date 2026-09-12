"""Call-policy enforcement tests (spec 18, 29 — Plan 2b.1–2b.4).

Verifies that the worker maps CallPolicy fields onto AgentSession kwargs and
that the max-duration watchdog fires at the right time.

These tests exercise ``_build_session``, ``_max_duration_watchdog``, and
``_wait_for_call_end`` without a live LiveKit room: the session constructor
is intercepted by inspection (we do not want to import the full LiveKit stack
in CI), and the watchdog is exercised with ``asyncio.sleep`` mocked out.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.models import HangupReason, ProviderKind
from worker.config_loader import CallContext, CallPolicy, TransferPolicy
from worker.providers.base import ProviderConfig


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _provider(kind: ProviderKind = ProviderKind.STT) -> ProviderConfig:
    return ProviderConfig(
        kind=kind,
        provider="openai_compatible",
        model="test-model",
        api_key=None,
        base_url="http://stt.internal/v1",
    )


def _policy(**overrides: Any) -> CallPolicy:
    base = CallPolicy(
        silence_timeout_seconds=None,
        max_call_duration_seconds=None,
        interruption_enabled=True,
        interruption_min_words=0,
        recording_enabled=False,
        transcription_enabled=False,
    )
    return replace(base, **overrides)


def _context(policy: CallPolicy | None = None) -> CallContext:
    return CallContext(
        call_id="call_test",
        call_row_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        tenant_slug="test-tenant",
        agent_id=uuid.uuid4(),
        agent_version_id=uuid.uuid4(),
        agent_name="Test Agent",
        version_number=1,
        room_name="room-test",
        did="+15550001111",
        caller_number="+15559990000",
        sip_trunk_id=None,
        pbx_id=None,
        language="en",
        greeting=None,
        system_prompt="You are a helpful assistant.",
        stt=_provider(ProviderKind.STT),
        llm=_provider(ProviderKind.LLM),
        tts=_provider(ProviderKind.TTS),
        call_policy=policy or _policy(),
        transfer_policy=TransferPolicy(
            enabled=False,
            announcement_text=None,
            hold_media_object_key=None,
            summary_template=None,
            summary_max_seconds=30,
            skip_dtmf=None,
        ),
    )


# --------------------------------------------------------------------------- #
# 2b.1 + 2b.4: AgentSession kwargs from call policy
# --------------------------------------------------------------------------- #


class TestBuildSessionPassesPolicy:
    """`_build_session` must forward every call-policy option to AgentSession."""

    def _capture_session_kwargs(self, context: CallContext) -> dict:
        """Run ``_build_session`` with AgentSession stubbed out; return kwargs."""
        captured: dict = {}

        class FakeSession:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

        vad = MagicMock()

        with (
            patch("worker.entrypoint.build_stt") as mock_stt,
            patch("worker.entrypoint.build_llm") as mock_llm,
            patch("worker.entrypoint.build_tts") as mock_tts,
            patch("worker.entrypoint.AgentSession", FakeSession),
        ):
            mock_stt.return_value.build_livekit_component.return_value = MagicMock()
            mock_llm.return_value.build_livekit_component.return_value = MagicMock()
            mock_tts.return_value.build_livekit_component.return_value = MagicMock()

            from worker.entrypoint import _build_session

            _build_session(context, vad)

        return captured

    def test_interruptions_enabled_by_default(self) -> None:
        ctx = _context(_policy(interruption_enabled=True, interruption_min_words=0))
        kwargs = self._capture_session_kwargs(ctx)
        assert kwargs["turn_handling"]["interruption"]["enabled"] is True

    def test_interruptions_disabled_when_policy_says_so(self) -> None:
        ctx = _context(_policy(interruption_enabled=False))
        kwargs = self._capture_session_kwargs(ctx)
        assert kwargs["turn_handling"]["interruption"]["enabled"] is False

    def test_min_interruption_words_forwarded(self) -> None:
        ctx = _context(_policy(interruption_min_words=3))
        kwargs = self._capture_session_kwargs(ctx)
        assert kwargs["turn_handling"]["interruption"]["min_words"] == 3

    def test_endpointing_window_is_set(self) -> None:
        """2b.7: the session must not fall back to LiveKit's 0.5 / 3.0 defaults.

        Asserted against the constants rather than literals. The window is
        fitted to a measurement that changes when STT placement changes, and a
        test that hard-codes the number has to be edited every time — which
        makes it a copy of the value rather than a check on it. What must not
        drift is the *relationship*: see ``test_filler.py`` for the assertion
        that the window still clears measured transcription.
        """
        from worker.endpointing import MAX_ENDPOINTING_DELAY_S, MIN_ENDPOINTING_DELAY_S

        kwargs = self._capture_session_kwargs(_context(_policy()))
        endpointing = kwargs["turn_handling"]["endpointing"]
        assert endpointing["min_delay"] == MIN_ENDPOINTING_DELAY_S
        assert endpointing["max_delay"] == MAX_ENDPOINTING_DELAY_S
        assert endpointing["min_delay"] != 0.5, "fell back to the LiveKit default"
        assert endpointing["mode"] == "fixed"
        assert kwargs["turn_handling"]["turn_detection"] == "vad"

    def test_deprecated_top_level_interruption_kwargs_are_not_set(self) -> None:
        """Once turn_handling is passed, top-level kwargs are ignored by LiveKit."""
        kwargs = self._capture_session_kwargs(_context(_policy()))
        assert "allow_interruptions" not in kwargs
        assert "min_interruption_words" not in kwargs

    def test_silence_timeout_forwarded_as_float(self) -> None:
        ctx = _context(_policy(silence_timeout_seconds=30))
        kwargs = self._capture_session_kwargs(ctx)
        assert kwargs.get("user_away_timeout") == 30.0
        assert isinstance(kwargs.get("user_away_timeout"), float)

    def test_no_silence_timeout_gives_none(self) -> None:
        ctx = _context(_policy(silence_timeout_seconds=None))
        kwargs = self._capture_session_kwargs(ctx)
        assert kwargs.get("user_away_timeout") is None

    def test_silence_timeout_zero_gives_none(self) -> None:
        """A timeout of 0 makes no semantic sense; treat it as unlimited."""
        ctx = _context(_policy(silence_timeout_seconds=0))
        kwargs = self._capture_session_kwargs(ctx)
        assert kwargs.get("user_away_timeout") is None


# --------------------------------------------------------------------------- #
# 2b.2: max-duration watchdog
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestMaxDurationWatchdog:
    async def test_watchdog_returns_max_duration_reason(self) -> None:
        ctx = MagicMock()
        ctx.delete_room = AsyncMock()

        with patch("worker.entrypoint.asyncio.sleep", new_callable=AsyncMock):
            from worker.entrypoint import _max_duration_watchdog

            reason = await _max_duration_watchdog(ctx, max_seconds=300, call_id="call_test")

        assert reason is HangupReason.MAX_DURATION
        ctx.delete_room.assert_awaited_once()

    async def test_wait_for_call_end_returns_caller_hangup_when_no_limit(self) -> None:
        ctx = MagicMock()
        context = _context(_policy(max_call_duration_seconds=None))

        with patch("worker.entrypoint._wait_for_disconnect", new_callable=AsyncMock):
            from worker.entrypoint import _wait_for_call_end

            reason = await _wait_for_call_end(ctx, context)

        assert reason is HangupReason.CALLER_HANGUP

    async def test_wait_for_call_end_caller_hangup_beats_watchdog(self) -> None:
        """When the caller hangs up before the limit, reason is CALLER_HANGUP."""
        ctx = MagicMock()
        context = _context(_policy(max_call_duration_seconds=3600))

        # Disconnect resolves immediately; watchdog sleeps forever.
        async def fast_disconnect(_ctx: Any) -> None:
            return

        async def slow_watchdog(_ctx: Any, _s: int, _id: str) -> HangupReason:
            await asyncio.sleep(9999)
            return HangupReason.MAX_DURATION

        with (
            patch("worker.entrypoint._wait_for_disconnect", fast_disconnect),
            patch("worker.entrypoint._max_duration_watchdog", slow_watchdog),
        ):
            from worker.entrypoint import _wait_for_call_end

            reason = await _wait_for_call_end(ctx, context)

        assert reason is HangupReason.CALLER_HANGUP

    async def test_wait_for_call_end_watchdog_beats_caller(self) -> None:
        """When the limit fires first, reason is MAX_DURATION."""
        ctx = MagicMock()
        context = _context(_policy(max_call_duration_seconds=1))

        async def slow_disconnect(_ctx: Any) -> None:
            await asyncio.sleep(9999)

        async def fast_watchdog(_ctx: Any, _s: int, _id: str) -> HangupReason:
            return HangupReason.MAX_DURATION

        with (
            patch("worker.entrypoint._wait_for_disconnect", slow_disconnect),
            patch("worker.entrypoint._max_duration_watchdog", fast_watchdog),
        ):
            from worker.entrypoint import _wait_for_call_end

            reason = await _wait_for_call_end(ctx, context)

        assert reason is HangupReason.MAX_DURATION


# --------------------------------------------------------------------------- #
# 2b.3: recording flag
# --------------------------------------------------------------------------- #


class TestRecordingPolicy:
    def test_recording_enabled_flag_is_in_call_policy(self) -> None:
        """CallPolicy.recording_enabled is the gate; it must exist."""
        policy = _policy(recording_enabled=True)
        assert policy.recording_enabled is True

    def test_recording_disabled_by_default(self) -> None:
        policy = _policy()
        assert policy.recording_enabled is False

    def test_entrypoint_reads_recording_flag(self) -> None:
        """_run_call must read recording_enabled, not hardcode False."""
        import inspect as ins

        from worker import entrypoint

        source = ins.getsource(entrypoint._run_call)
        assert "recording_enabled" in source, (
            "_run_call must check context.call_policy.recording_enabled"
        )

    def test_entrypoint_calls_start_and_stop_recording(self) -> None:
        """Both _start_recording and _stop_recording must be referenced in _run_call."""
        import inspect as ins

        from worker import entrypoint

        source = ins.getsource(entrypoint._run_call)
        assert "_start_recording" in source
        assert "_stop_recording" in source


# --------------------------------------------------------------------------- #
# SQL: _RESOLVE_SQL must select the columns added for routing (2b.1–2b.4)
# --------------------------------------------------------------------------- #


class TestResolveQueryContainsRequiredColumns:
    def test_tenant_timezone_is_selected(self) -> None:
        from worker.config_loader import _RESOLVE_SQL

        sql = str(_RESOLVE_SQL)
        assert "timezone" in sql, "tenant timezone needed for business hours fallback"

    def test_routing_rule_id_is_selected(self) -> None:
        from worker.config_loader import _RESOLVE_SQL

        sql = str(_RESOLVE_SQL)
        assert "routing_rule_id" in sql
