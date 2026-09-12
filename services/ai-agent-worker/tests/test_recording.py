"""Recording metadata row tests (spec 39 — Plan 2b.3b).

The egress to S3/MinIO was implemented in 2b.3.  This suite covers the
missing half: writing a ``call_recordings`` row and updating
``calls.recording_id`` after the egress stops.

Tests exercise three things:

1. ``_start_recording`` returns a ``_RecordingInfo`` (not just a string) with
   the expected fields populated.
2. ``write_recording_row`` sends the right SQL with the right parameters, and
   updates the back-pointer on ``calls``.
3. ``_run_call``'s finally block calls ``write_recording_row`` non-fatally —
   a DB write failure must not suppress the usage rollup.

SQL column coverage is verified by inspecting the query text, following the
same pattern as ``test_call_limits.py``.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.models import ProviderKind
from worker.config_loader import (
    CallContext,
    CallPolicy,
    TransferPolicy,
    _CALLS_SET_RECORDING_SQL,
    _RECORDING_INSERT_SQL,
    write_recording_row,
)
from worker.entrypoint import _RecordingInfo, _start_recording
from worker.providers.base import ProviderConfig


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _provider(kind: ProviderKind) -> ProviderConfig:
    return ProviderConfig(
        kind=kind,
        provider="openai_compatible",
        model="m",
        api_key=None,
        base_url="http://x",
    )


def _policy(**kwargs) -> CallPolicy:
    defaults = {
        "recording_enabled": True,
        "interruption_enabled": True,
        "interruption_min_words": 1,
        "silence_timeout_seconds": None,
        "max_call_duration_seconds": None,
        "transcription_enabled": False,
    }
    defaults.update(kwargs)
    return CallPolicy(**defaults)


def _context(policy: CallPolicy | None = None) -> CallContext:
    return CallContext(
        call_id="test-call-001",
        call_row_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        tenant_slug="acme",
        agent_id=uuid.uuid4(),
        agent_version_id=uuid.uuid4(),
        agent_name="test-agent",
        version_number=1,
        room_name="test-room",
        did="1001",
        caller_number="+15550001111",
        sip_trunk_id=uuid.uuid4(),
        pbx_id=uuid.uuid4(),
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


def _mock_session(return_rows: list | None = None):
    """Return a mock AsyncSession that yields ``return_rows`` on fetchone().

    ``session.execute`` is an ``AsyncMock`` so ``.call_count`` works in tests.
    """
    session = MagicMock()
    rows = list(return_rows) if return_rows is not None else [MagicMock()]
    call_idx = [0]

    async def _execute(query: Any, params: Any = None) -> Any:
        idx = call_idx[0]
        call_idx[0] += 1
        result = MagicMock()
        result.fetchone.return_value = rows[idx] if idx < len(rows) else None
        return result

    session.execute = AsyncMock(side_effect=_execute)
    session.commit = AsyncMock()
    return session


# --------------------------------------------------------------------------- #
# 1. _start_recording returns _RecordingInfo
# --------------------------------------------------------------------------- #


class TestStartRecordingReturnsInfo:
    @pytest.mark.asyncio
    async def test_returns_recording_info_on_success(self):
        ctx = MagicMock()
        context = _context()

        mock_egress_response = MagicMock()
        mock_egress_response.egress_id = "egress-abc-123"

        mock_lk = AsyncMock()
        mock_lk.egress.start_room_composite_egress = AsyncMock(
            return_value=mock_egress_response
        )

        mock_settings = MagicMock()
        mock_settings.s3_bucket_recordings = "recordings-bucket"
        mock_settings.s3_access_key_id.get_secret_value.return_value = "key"
        mock_settings.s3_secret_access_key.get_secret_value.return_value = "secret"
        mock_settings.s3_region = "us-east-1"
        mock_settings.s3_endpoint_url = None
        mock_settings.livekit_url = "wss://lk.example.com"
        mock_settings.livekit_api_key.get_secret_value.return_value = "lk-key"
        mock_settings.livekit_api_secret.get_secret_value.return_value = "lk-secret"

        class _FakeLKAPI:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return mock_lk

            async def __aexit__(self, *args):
                pass

        with (
            patch("worker.entrypoint.get_settings", return_value=mock_settings),
            patch("livekit.api.LiveKitAPI", _FakeLKAPI),
        ):
            result = await _start_recording(ctx, context)

        assert isinstance(result, _RecordingInfo)
        assert result.egress_id == "egress-abc-123"
        assert result.bucket == "recordings-bucket"
        assert "calls/" in result.object_key
        assert str(context.tenant_id) in result.object_key
        assert result.object_key.endswith(".mp4")
        assert result.started_at is not None
        assert isinstance(result.started_at, datetime)

    @pytest.mark.asyncio
    async def test_returns_none_when_no_bucket_configured(self):
        ctx = MagicMock()
        context = _context()

        mock_settings = MagicMock()
        mock_settings.s3_bucket_recordings = None  # not configured

        with patch("worker.entrypoint.get_settings", return_value=mock_settings):
            result = await _start_recording(ctx, context)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_livekit_error(self):
        ctx = MagicMock()
        context = _context()

        mock_settings = MagicMock()
        mock_settings.s3_bucket_recordings = "recordings-bucket"
        mock_settings.s3_access_key_id.get_secret_value.return_value = "key"
        mock_settings.s3_secret_access_key.get_secret_value.return_value = "secret"
        mock_settings.s3_region = "us-east-1"
        mock_settings.s3_endpoint_url = None
        mock_settings.livekit_url = "wss://lk.example.com"
        mock_settings.livekit_api_key.get_secret_value.return_value = "lk-key"
        mock_settings.livekit_api_secret.get_secret_value.return_value = "lk-secret"

        class _FaultyLKAPI:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                raise ConnectionError("LiveKit down")

            async def __aexit__(self, *args):
                pass

        with (
            patch("worker.entrypoint.get_settings", return_value=mock_settings),
            patch("livekit.api.LiveKitAPI", _FaultyLKAPI),
        ):
            result = await _start_recording(ctx, context)

        assert result is None  # non-fatal — call continues


# --------------------------------------------------------------------------- #
# 2. write_recording_row executes correct SQL
# --------------------------------------------------------------------------- #


class TestWriteRecordingRow:
    @pytest.mark.asyncio
    async def test_executes_insert_and_update(self):
        context = _context()
        session = _mock_session()
        started = datetime.now(UTC)

        await write_recording_row(
            session,
            context=context,
            egress_id="egress-xyz",
            bucket="my-bucket",
            object_key="calls/tenant/call.mp4",
            started_at=started,
            duration_seconds=120,
        )

        # Both INSERT and UPDATE must have been called
        assert session.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_skips_calls_update_on_conflict(self):
        """If the INSERT conflicts (ON CONFLICT DO NOTHING), RETURNING is None
        and the calls UPDATE must NOT run."""
        context = _context()
        session = _mock_session(return_rows=[None])  # RETURNING returns nothing
        started = datetime.now(UTC)

        await write_recording_row(
            session,
            context=context,
            egress_id="egress-xyz",
            bucket="my-bucket",
            object_key="calls/tenant/call.mp4",
            started_at=started,
            duration_seconds=None,
        )

        # Only the INSERT ran; UPDATE must be skipped on conflict
        assert session.execute.call_count == 1

    @pytest.mark.asyncio
    async def test_returns_uuid(self):
        context = _context()
        session = _mock_session()
        started = datetime.now(UTC)

        result = await write_recording_row(
            session,
            context=context,
            egress_id="egress-xyz",
            bucket="bucket",
            object_key="key.mp4",
            started_at=started,
            duration_seconds=60,
        )

        assert isinstance(result, uuid.UUID)


class TestRecordingSQL:
    def test_insert_has_required_columns(self):
        sql = str(_RECORDING_INSERT_SQL)
        for col in (
            "call_id",
            "tenant_id",
            "bucket",
            "object_key",
            "content_type",
            "livekit_egress_id",
            "started_at",
            "ended_at",
            "duration_seconds",
        ):
            assert col in sql, f"_RECORDING_INSERT_SQL missing column: {col}"

    def test_insert_uses_on_conflict_do_nothing(self):
        sql = str(_RECORDING_INSERT_SQL)
        assert "ON CONFLICT" in sql
        assert "NOTHING" in sql

    def test_insert_returns_id(self):
        sql = str(_RECORDING_INSERT_SQL)
        assert "RETURNING" in sql

    def test_calls_update_sets_recording_id(self):
        sql = str(_CALLS_SET_RECORDING_SQL)
        assert "recording_id" in sql
        assert "calls" in sql


# --------------------------------------------------------------------------- #
# 3. _run_call finally block: write is non-fatal
# --------------------------------------------------------------------------- #


class TestRunCallRecordingIntegration:
    def test_recording_row_write_is_non_fatal(self):
        """Source inspection: write_recording_row is called non-fatally in
        _run_call — a DB failure must not suppress the usage rollup."""
        import worker.entrypoint as ep

        src = inspect.getsource(ep._run_call)
        assert "write_recording_row" in src
        assert "recording_row_write_failed" in src

    def test_recording_info_replaces_egress_id(self):
        """_run_call should use recording_info, not a bare egress_id."""
        import worker.entrypoint as ep

        src = inspect.getsource(ep._run_call)
        assert "recording_info" in src
        assert "recording_info.egress_id" in src

    def test_stop_recording_called_with_egress_id(self):
        """_stop_recording should receive recording_info.egress_id."""
        import worker.entrypoint as ep

        src = inspect.getsource(ep._run_call)
        assert "_stop_recording(recording_info.egress_id)" in src

    def test_usage_write_still_runs_after_recording_failure(self):
        """Both write_recording_row and update_usage have their own try/except
        blocks so one failure does not block the other."""
        import worker.entrypoint as ep

        src = inspect.getsource(ep._run_call)
        assert "recording_row_write_failed" in src
        assert "usage_update_failed" in src
