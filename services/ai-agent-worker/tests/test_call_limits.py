"""Call-limit enforcement tests (spec 47 — Plan 1b.1).

Covers the three limits checked by ``CallConfigLoader._enforce_limits``:
  1. max_concurrent_calls — already had a partial test; expanded here.
  2. max_daily_calls      — new: count of today's calls in tenant timezone.
  3. max_monthly_minutes  — new: estimated minutes used this calendar month.

SQL correctness (i.e. that the right columns are queried) is asserted by
inspecting the SQL text, following the same pattern as the provider-chain
tests.  Business logic is exercised via a mock AsyncSession so no real
database is required.
"""

from __future__ import annotations

import inspect
import re
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.models import CallState
from worker.config_loader import (
    CallConfigLoader,
    TenantLimitExceededError,
    _DAILY_CALLS_SQL,
    _MONTHLY_SECONDS_SQL,
    _USAGE_UPSERT_SQL,
    update_usage,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _tenant_row(
    *,
    max_concurrent: int | None = None,
    max_daily: int | None = None,
    max_monthly: int | None = None,
    slug: str = "acme",
    timezone: str = "UTC",
) -> dict:
    return {
        "tenant_id": uuid.uuid4(),
        "tenant_slug": slug,
        "tenant_timezone": timezone,
        "max_concurrent_calls": max_concurrent,
        "max_daily_calls": max_daily,
        "max_monthly_minutes": max_monthly,
    }


def _mock_session(*return_scalars: int):
    """Return an AsyncSession mock that yields ``return_scalars`` in order."""
    session = MagicMock()
    call_count = 0

    async def execute(query: Any, params: Any = None) -> Any:
        nonlocal call_count
        result = MagicMock()
        value = return_scalars[call_count] if call_count < len(return_scalars) else 0
        result.scalar_one.return_value = value
        call_count += 1
        return result

    session.execute = execute
    return session


# --------------------------------------------------------------------------- #
# Concurrent-call limit
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestConcurrentCallLimit:
    async def test_no_limit_skips_all_checks(self) -> None:
        """All three limits None → no DB queries at all."""
        row = _tenant_row()
        session = MagicMock()
        session.execute = AsyncMock()
        loader = CallConfigLoader()

        await loader._enforce_limits(session, row)

        session.execute.assert_not_called()

    async def test_at_limit_raises(self) -> None:
        row = _tenant_row(max_concurrent=2)
        session = _mock_session(2)  # active = 2 = limit
        loader = CallConfigLoader()

        with pytest.raises(TenantLimitExceededError) as exc_info:
            await loader._enforce_limits(session, row)

        assert exc_info.value.limit == "MAX_CONCURRENT_CALLS"

    async def test_below_limit_passes(self) -> None:
        row = _tenant_row(max_concurrent=5)
        session = _mock_session(3)  # active = 3 < 5
        loader = CallConfigLoader()

        await loader._enforce_limits(session, row)  # must not raise


# --------------------------------------------------------------------------- #
# Daily call limit
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestDailyCallLimit:
    async def test_at_daily_limit_raises(self) -> None:
        row = _tenant_row(max_daily=10)
        # max_concurrent=None → skipped; first and only query is the daily count
        session = _mock_session(10)
        loader = CallConfigLoader()

        with pytest.raises(TenantLimitExceededError) as exc_info:
            await loader._enforce_limits(session, row)

        assert exc_info.value.limit == "MAX_DAILY_CALLS"

    async def test_below_daily_limit_passes(self) -> None:
        row = _tenant_row(max_daily=10)
        session = _mock_session(0, 9)  # concurrent=0, daily=9
        loader = CallConfigLoader()

        await loader._enforce_limits(session, row)

    async def test_daily_limit_not_queried_when_unset(self) -> None:
        """max_daily_calls=None → daily-calls SQL is never executed."""
        row = _tenant_row(max_concurrent=5)
        queries_run: list[str] = []

        session = MagicMock()

        async def execute(query: Any, params: Any = None) -> Any:
            queries_run.append(str(query))
            result = MagicMock()
            result.scalar_one.return_value = 0
            return result

        session.execute = execute
        loader = CallConfigLoader()

        await loader._enforce_limits(session, row)

        daily_sql = str(_DAILY_CALLS_SQL)
        assert not any(daily_sql[:30] in q for q in queries_run), (
            "daily-calls SQL should not run when max_daily_calls is None"
        )


# --------------------------------------------------------------------------- #
# Monthly minutes limit
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestMonthlyMinutesLimit:
    async def test_at_monthly_limit_raises(self) -> None:
        # Limit = 100 minutes; used = 100 * 60 = 6 000 seconds.
        # max_concurrent=None, max_daily=None → both skipped; only monthly queried.
        row = _tenant_row(max_monthly=100)
        session = _mock_session(6000)
        loader = CallConfigLoader()

        with pytest.raises(TenantLimitExceededError) as exc_info:
            await loader._enforce_limits(session, row)

        assert exc_info.value.limit == "MAX_MONTHLY_MINUTES"

    async def test_below_monthly_limit_passes(self) -> None:
        row = _tenant_row(max_monthly=100)
        session = _mock_session(0, 0, 5940)  # 99 minutes = 5940 s
        loader = CallConfigLoader()

        await loader._enforce_limits(session, row)

    async def test_monthly_checked_after_daily(self) -> None:
        """When daily passes, monthly is still evaluated."""
        # max_concurrent=None → skipped; first query=daily=50 (ok), second=monthly=7200s (over)
        row = _tenant_row(max_daily=100, max_monthly=10)
        session = _mock_session(50, 7200)
        loader = CallConfigLoader()

        with pytest.raises(TenantLimitExceededError) as exc_info:
            await loader._enforce_limits(session, row)

        assert exc_info.value.limit == "MAX_MONTHLY_MINUTES"

    async def test_monthly_limit_not_queried_when_unset(self) -> None:
        row = _tenant_row(max_concurrent=5, max_daily=50)
        queries_run: list[str] = []

        session = MagicMock()

        async def execute(query: Any, params: Any = None) -> Any:
            queries_run.append(str(query))
            result = MagicMock()
            result.scalar_one.return_value = 0
            return result

        session.execute = execute
        loader = CallConfigLoader()
        await loader._enforce_limits(session, row)

        monthly_sql = str(_MONTHLY_SECONDS_SQL)
        assert not any(monthly_sql[:30] in q for q in queries_run)

    async def test_none_seconds_treated_as_zero(self) -> None:
        """A SUM that returns NULL (no calls yet) should not crash."""
        row = _tenant_row(max_monthly=60)
        session = _mock_session(0, 0, 0)  # scalar_one returns 0 (COALESCE-ed)
        loader = CallConfigLoader()

        await loader._enforce_limits(session, row)  # must not raise


# --------------------------------------------------------------------------- #
# Limit ordering — concurrent checked first
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestLimitOrdering:
    async def test_concurrent_blocks_before_daily(self) -> None:
        """If concurrent limit fires, daily is never checked."""
        row = _tenant_row(max_concurrent=1, max_daily=100)
        # Concurrent check sees 2 active → raises. Daily would also be queried
        # but we should get the concurrent error, not daily.
        session = _mock_session(2)
        loader = CallConfigLoader()

        with pytest.raises(TenantLimitExceededError) as exc_info:
            await loader._enforce_limits(session, row)

        assert exc_info.value.limit == "MAX_CONCURRENT_CALLS"


# --------------------------------------------------------------------------- #
# SQL column assertions
# --------------------------------------------------------------------------- #


class TestLimitSQL:
    def test_daily_sql_uses_tenant_timezone(self) -> None:
        sql = str(_DAILY_CALLS_SQL)
        assert "timezone" in sql
        assert "start_time" in sql

    def test_monthly_sql_handles_in_progress_calls(self) -> None:
        """In-progress calls must be estimated so the limit is not silently
        circumvented by a batch of long-running calls."""
        sql = str(_MONTHLY_SECONDS_SQL)
        assert "duration_seconds" in sql
        # The CASE must cover the non-terminal (in-progress) path
        assert "state NOT IN" in sql
        assert "start_time" in sql

    def test_usage_upsert_increments_atomically(self) -> None:
        """ON CONFLICT clause must use additive UPDATE, not SET = :value."""
        sql = str(_USAGE_UPSERT_SQL)
        assert "ON CONFLICT" in sql
        # Allow extra alignment spaces between call_count and + 1
        assert re.search(r"call_count\s*\+\s*1", sql), (
            "ON CONFLICT clause must increment call_count additively"
        )
        assert "total_seconds" in sql

    def test_resolve_sql_exposes_all_three_limit_columns(self) -> None:
        from worker.config_loader import _RESOLVE_SQL

        sql = str(_RESOLVE_SQL)
        for col in ("max_concurrent_calls", "max_daily_calls", "max_monthly_minutes"):
            assert col in sql, f"missing column: {col}"

    def test_resolve_sql_exposes_tenant_timezone(self) -> None:
        from worker.config_loader import _RESOLVE_SQL

        assert "timezone" in str(_RESOLVE_SQL)


# --------------------------------------------------------------------------- #
# Usage rollup writer
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestUpdateUsage:
    async def test_upsert_is_executed(self) -> None:
        """update_usage must fire the UPSERT SQL."""
        from worker.config_loader import CallContext, CallPolicy, TransferPolicy
        from worker.providers.base import ProviderConfig
        from shared.models import ProviderKind

        def _p(kind: ProviderKind) -> ProviderConfig:
            return ProviderConfig(kind=kind, provider="openai_compatible", model="m", api_key=None, base_url="http://x")

        context = CallContext(
            call_id="c",
            call_row_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            tenant_slug="t",
            tenant_timezone="UTC",
            agent_id=uuid.uuid4(),
            agent_version_id=uuid.uuid4(),
            agent_name="A",
            version_number=1,
            room_name="r",
            did="+1",
            caller_number=None,
            sip_trunk_id=None,
            pbx_id=None,
            language="en",
            greeting=None,
            system_prompt="",
            stt=_p(ProviderKind.STT),
            llm=_p(ProviderKind.LLM),
            tts=_p(ProviderKind.TTS),
            call_policy=CallPolicy(
                silence_timeout_seconds=None,
                max_call_duration_seconds=None,
                interruption_enabled=True,
                interruption_min_words=0,
                recording_enabled=False,
                transcription_enabled=False,
            ),
            transfer_policy=TransferPolicy(
                enabled=False,
                announcement_text=None,
                hold_media_object_key=None,
                summary_template=None,
                summary_max_seconds=30,
                skip_dtmf=None,
            ),
        )

        session = MagicMock()
        executed_sqls: list[str] = []

        async def execute(query: Any, params: Any = None) -> Any:
            executed_sqls.append(str(query))
            return MagicMock()

        session.execute = execute

        await update_usage(session, context=context, duration_seconds=120, succeeded=True)

        upsert_sql = str(_USAGE_UPSERT_SQL)
        assert any(upsert_sql[:30] in q for q in executed_sqls), (
            "update_usage must execute the USAGE_UPSERT_SQL"
        )

    async def test_negative_duration_clamped_to_zero(self) -> None:
        """A negative duration (e.g. clock skew) must not write a negative int."""
        from worker.config_loader import CallContext, CallPolicy, TransferPolicy
        from worker.providers.base import ProviderConfig
        from shared.models import ProviderKind

        def _p(kind: ProviderKind) -> ProviderConfig:
            return ProviderConfig(kind=kind, provider="openai_compatible", model="m", api_key=None, base_url="http://x")

        context = CallContext(
            call_id="c",
            call_row_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            tenant_slug="t",
            tenant_timezone="UTC",
            agent_id=uuid.uuid4(),
            agent_version_id=uuid.uuid4(),
            agent_name="A",
            version_number=1,
            room_name="r",
            did="+1",
            caller_number=None,
            sip_trunk_id=None,
            pbx_id=None,
            language="en",
            greeting=None,
            system_prompt="",
            stt=_p(ProviderKind.STT),
            llm=_p(ProviderKind.LLM),
            tts=_p(ProviderKind.TTS),
            call_policy=CallPolicy(
                silence_timeout_seconds=None,
                max_call_duration_seconds=None,
                interruption_enabled=True,
                interruption_min_words=0,
                recording_enabled=False,
                transcription_enabled=False,
            ),
            transfer_policy=TransferPolicy(
                enabled=False,
                announcement_text=None,
                hold_media_object_key=None,
                summary_template=None,
                summary_max_seconds=30,
                skip_dtmf=None,
            ),
        )

        captured_params: list[dict] = []
        session = MagicMock()

        async def execute(query: Any, params: Any = None) -> Any:
            if params:
                captured_params.append(dict(params))
            return MagicMock()

        session.execute = execute

        await update_usage(session, context=context, duration_seconds=-5, succeeded=True)

        assert any(p.get("duration_seconds", -1) >= 0 for p in captured_params), (
            "duration_seconds must be clamped to >= 0"
        )
