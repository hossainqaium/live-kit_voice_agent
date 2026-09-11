"""Routing engine unit tests (spec 20, 37, 38 — Plan 4b.3).

Exercises the pure-Python logic in ``worker/routing.py`` without a database.
SQL correctness (i.e. that the right columns are selected) is checked by
asserting on the SQL text, following the same pattern as
``test_provider_chain.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from worker.routing import (
    RoutingClosedError,
    RoutingDecision,
    _ALL_RULES_SQL,
    _HOURS_SQL,
    _PINNED_RULE_SQL,
    _check_open_now,
    _matches,
    _specificity,
    resolve_route,
)


# --------------------------------------------------------------------------- #
# Condition matching
# --------------------------------------------------------------------------- #


class TestConditionMatching:
    def test_empty_conditions_match_anything(self) -> None:
        pbx = uuid.uuid4()
        trunk = uuid.uuid4()
        assert _matches({}, did="+15550001111", caller_number="+15559990000", pbx_id=pbx, sip_trunk_id=trunk)

    def test_did_condition_matches_exact(self) -> None:
        assert _matches(
            {"did": "+15550001111"},
            did="+15550001111",
            caller_number=None,
            pbx_id=None,
            sip_trunk_id=None,
        )

    def test_did_condition_rejects_mismatch(self) -> None:
        assert not _matches(
            {"did": "+15550001111"},
            did="+15559999999",
            caller_number=None,
            pbx_id=None,
            sip_trunk_id=None,
        )

    def test_caller_number_prefix_matches(self) -> None:
        assert _matches(
            {"caller_number_prefix": "+1555"},
            did="+15550001111",
            caller_number="+15559990000",
            pbx_id=None,
            sip_trunk_id=None,
        )

    def test_caller_number_prefix_rejects_non_prefix(self) -> None:
        assert not _matches(
            {"caller_number_prefix": "+1999"},
            did="+15550001111",
            caller_number="+15559990000",
            pbx_id=None,
            sip_trunk_id=None,
        )

    def test_caller_number_prefix_with_none_caller(self) -> None:
        """Anonymous callers never match a prefix condition."""
        assert not _matches(
            {"caller_number_prefix": "+1555"},
            did="+15550001111",
            caller_number=None,
            pbx_id=None,
            sip_trunk_id=None,
        )

    def test_pbx_id_matched_as_string(self) -> None:
        """JSONB stores UUIDs as strings; the comparison must be string-based."""
        pbx = uuid.uuid4()
        assert _matches(
            {"pbx_id": str(pbx)},
            did="+15550001111",
            caller_number=None,
            pbx_id=pbx,
            sip_trunk_id=None,
        )

    def test_pbx_id_absent_rejects_condition(self) -> None:
        assert not _matches(
            {"pbx_id": str(uuid.uuid4())},
            did="+15550001111",
            caller_number=None,
            pbx_id=None,  # DID has no PBX
            sip_trunk_id=None,
        )

    def test_sip_trunk_id_condition(self) -> None:
        trunk = uuid.uuid4()
        assert _matches(
            {"sip_trunk_id": str(trunk)},
            did="+15550001111",
            caller_number=None,
            pbx_id=None,
            sip_trunk_id=trunk,
        )

    def test_destination_number_aliases_did(self) -> None:
        """destination_number is an alias for the called DID."""
        assert _matches(
            {"destination_number": "+15550001111"},
            did="+15550001111",
            caller_number=None,
            pbx_id=None,
            sip_trunk_id=None,
        )

    def test_multiple_conditions_all_must_match(self) -> None:
        trunk = uuid.uuid4()
        assert _matches(
            {"did": "+15550001111", "sip_trunk_id": str(trunk)},
            did="+15550001111",
            caller_number=None,
            pbx_id=None,
            sip_trunk_id=trunk,
        )
        # Trunk mismatch → rejected
        assert not _matches(
            {"did": "+15550001111", "sip_trunk_id": str(uuid.uuid4())},
            did="+15550001111",
            caller_number=None,
            pbx_id=None,
            sip_trunk_id=trunk,
        )


# --------------------------------------------------------------------------- #
# Specificity
# --------------------------------------------------------------------------- #


class TestSpecificity:
    def test_empty_conditions_have_zero_specificity(self) -> None:
        assert _specificity({}) == 0

    def test_one_condition_gives_specificity_one(self) -> None:
        assert _specificity({"did": "+15550001111"}) == 1

    def test_two_conditions_give_specificity_two(self) -> None:
        assert _specificity({"did": "+15550001111", "caller_number_prefix": "+1555"}) == 2

    def test_null_values_do_not_contribute(self) -> None:
        """A condition stored as None in JSONB is not an active filter."""
        assert _specificity({"did": "+15550001111", "pbx_id": None}) == 1


# --------------------------------------------------------------------------- #
# Business-hours evaluation
# --------------------------------------------------------------------------- #


def _tz_now_at(hour: int, minute: int = 0, tz: str = "America/New_York") -> None:
    """Return a ``time`` object in the given timezone at the given local time.

    Used to construct mock ``datetime.now`` results for ``_check_open_now``.
    """


class TestBusinessHoursCheck:
    """Tests for ``_check_open_now``.

    We freeze time by monkeypatching ``datetime.now`` inside
    ``worker.routing`` so the tests are deterministic.
    """

    def _interval(self, day: str, opens: time, closes: time) -> tuple[str, time, time]:
        return (day, opens, closes)

    def _freeze(self, weekday: int, hour: int, minute: int = 0, tz: str = "UTC") -> datetime:
        """Return a UTC datetime that, in ``tz``, is the given weekday/time."""
        # Build a datetime in the target tz, convert to UTC.
        zone = ZoneInfo(tz)
        # Anchor to a known Monday (2026-09-07).
        base = datetime(2026, 9, 7, tzinfo=zone)  # Monday
        local = base + timedelta(days=weekday, hours=hour, minutes=minute)
        return local.astimezone(UTC)

    def test_open_during_an_interval(self) -> None:
        intervals = [self._interval("MONDAY", time(9, 0), time(17, 0))]
        frozen = self._freeze(weekday=0, hour=12, tz="UTC")

        with patch("worker.routing.datetime") as mock_dt:
            mock_dt.now.return_value = frozen
            result = _check_open_now(intervals, "UTC", [])

        assert result is True

    def test_closed_outside_interval(self) -> None:
        intervals = [self._interval("MONDAY", time(9, 0), time(17, 0))]
        frozen = self._freeze(weekday=0, hour=20, tz="UTC")  # 8pm

        with patch("worker.routing.datetime") as mock_dt:
            mock_dt.now.return_value = frozen
            result = _check_open_now(intervals, "UTC", [])

        assert result is False

    def test_closed_on_a_different_day(self) -> None:
        intervals = [self._interval("MONDAY", time(9, 0), time(17, 0))]
        frozen = self._freeze(weekday=1, hour=12, tz="UTC")  # Tuesday noon

        with patch("worker.routing.datetime") as mock_dt:
            mock_dt.now.return_value = frozen
            result = _check_open_now(intervals, "UTC", [])

        assert result is False

    def test_holiday_override_closes(self) -> None:
        intervals = [self._interval("MONDAY", time(9, 0), time(17, 0))]
        frozen = self._freeze(weekday=0, hour=12, tz="UTC")  # Monday noon
        today = frozen.astimezone(ZoneInfo("UTC")).date().isoformat()
        holidays = [{"date": today, "closed": True}]

        with patch("worker.routing.datetime") as mock_dt:
            mock_dt.now.return_value = frozen
            result = _check_open_now(intervals, "UTC", holidays)

        assert result is False

    def test_holiday_override_opens_a_sunday(self) -> None:
        """A holiday entry can declare a normally-closed day as open."""
        intervals = [self._interval("MONDAY", time(9, 0), time(17, 0))]
        frozen = self._freeze(weekday=6, hour=12, tz="UTC")  # Sunday
        today = frozen.astimezone(ZoneInfo("UTC")).date().isoformat()
        holidays = [{"date": today, "closed": False}]

        with patch("worker.routing.datetime") as mock_dt:
            mock_dt.now.return_value = frozen
            result = _check_open_now(intervals, "UTC", holidays)

        assert result is True

    def test_unknown_timezone_returns_none(self) -> None:
        """A timezone typo should not silently close the business."""
        result = _check_open_now([], "Not/A/Timezone", [])
        assert result is None

    def test_split_schedule_multiple_intervals(self) -> None:
        """Two intervals on one day cover a lunch break."""
        intervals = [
            self._interval("MONDAY", time(9, 0), time(12, 0)),
            self._interval("MONDAY", time(13, 0), time(17, 0)),
        ]
        # During lunch
        frozen_lunch = self._freeze(weekday=0, hour=12, minute=30, tz="UTC")
        # After lunch
        frozen_after = self._freeze(weekday=0, hour=14, tz="UTC")

        with patch("worker.routing.datetime") as mock_dt:
            mock_dt.now.return_value = frozen_lunch
            assert _check_open_now(intervals, "UTC", []) is False

            mock_dt.now.return_value = frozen_after
            assert _check_open_now(intervals, "UTC", []) is True


# --------------------------------------------------------------------------- #
# SQL column assertions
# --------------------------------------------------------------------------- #


class TestRoutingSQL:
    """The SQL must select every column the engine reads."""

    def test_all_rules_query_selects_required_columns(self) -> None:
        sql = str(_ALL_RULES_SQL)
        for col in (
            "conditions",
            "agent_id",
            "business_hours_id",
            "fallback_action",
            "fallback_agent_id",
            "closed_action",
            "priority",
        ):
            assert col in sql, f"missing column: {col}"

    def test_pinned_rule_query_selects_same_columns(self) -> None:
        sql = str(_PINNED_RULE_SQL)
        for col in ("conditions", "agent_id", "business_hours_id", "fallback_agent_id"):
            assert col in sql, f"missing column: {col}"

    def test_hours_query_selects_intervals_and_metadata(self) -> None:
        sql = str(_HOURS_SQL)
        for col in ("timezone", "holidays", "day_of_week", "opens_at", "closes_at"):
            assert col in sql, f"missing column: {col}"

    def test_resolve_sql_selects_routing_rule_id(self) -> None:
        """The DID row must expose routing_rule_id for the pinned-rule path."""
        from worker.config_loader import _RESOLVE_SQL

        sql = str(_RESOLVE_SQL)
        assert "routing_rule_id" in sql
        assert "tenant_timezone" in sql or "t.timezone" in sql


# --------------------------------------------------------------------------- #
# resolve_route — async integration (mock session)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestResolveRoute:
    """Test ``resolve_route`` with a mocked AsyncSession.

    We build fake DB rows rather than hitting Postgres, following the same
    pattern as the provider-chain tests. The goal is the logic — priority
    ordering, condition filtering, business-hours gate — not the SQL.
    """

    def _rule_row(
        self,
        *,
        name: str = "default",
        priority: int = 100,
        conditions: dict | None = None,
        agent_id: uuid.UUID | None = None,
        business_hours_id: uuid.UUID | None = None,
        fallback_action: str | None = None,
        fallback_agent_id: uuid.UUID | None = None,
        closed_action: str | None = None,
    ) -> dict:
        return {
            "id": uuid.uuid4(),
            "name": name,
            "priority": priority,
            "conditions": conditions or {},
            "agent_id": agent_id or uuid.uuid4(),
            "business_hours_id": business_hours_id,
            "fallback_action": fallback_action,
            "fallback_agent_id": fallback_agent_id,
            "closed_action": closed_action,
        }

    def _mock_session(self, rules: list[dict], hours: list[dict] | None = None):
        """Return an AsyncSession mock that returns ``rules`` for the rules query
        and ``hours`` for the hours query."""
        session = MagicMock()

        async def execute(query, params=None):
            sql_text = str(query)
            result = MagicMock()
            if "routing_rules" in sql_text:
                result.mappings.return_value.all.return_value = rules
                result.mappings.return_value.first.return_value = rules[0] if rules else None
            elif "business_hours" in sql_text:
                result.mappings.return_value.all.return_value = hours or []
            else:
                result.mappings.return_value.all.return_value = []
            return result

        session.execute = execute
        return session

    async def test_no_rules_returns_none(self) -> None:
        session = self._mock_session([])
        result = await resolve_route(
            session,
            tenant_id=uuid.uuid4(),
            tenant_timezone="UTC",
            did="+15550001111",
            caller_number=None,
            pbx_id=None,
            sip_trunk_id=None,
            pinned_rule_id=None,
        )
        assert result is None

    async def test_matching_rule_returns_decision(self) -> None:
        agent_id = uuid.uuid4()
        rule = self._rule_row(agent_id=agent_id, conditions={"did": "+15550001111"})
        session = self._mock_session([rule])

        result = await resolve_route(
            session,
            tenant_id=uuid.uuid4(),
            tenant_timezone="UTC",
            did="+15550001111",
            caller_number=None,
            pbx_id=None,
            sip_trunk_id=None,
            pinned_rule_id=None,
        )

        assert isinstance(result, RoutingDecision)
        assert result.agent_id == agent_id

    async def test_non_matching_rule_returns_none(self) -> None:
        rule = self._rule_row(conditions={"did": "+19999999999"})
        session = self._mock_session([rule])

        result = await resolve_route(
            session,
            tenant_id=uuid.uuid4(),
            tenant_timezone="UTC",
            did="+15550001111",
            caller_number=None,
            pbx_id=None,
            sip_trunk_id=None,
            pinned_rule_id=None,
        )
        assert result is None

    async def test_higher_priority_wins(self) -> None:
        """Lower priority number = evaluated first."""
        agent_low = uuid.uuid4()
        agent_high = uuid.uuid4()
        # priority=50 evaluated before priority=100
        rule_high = self._rule_row(name="high", priority=50, agent_id=agent_high)
        rule_low = self._rule_row(name="low", priority=100, agent_id=agent_low)
        session = self._mock_session([rule_high, rule_low])

        result = await resolve_route(
            session,
            tenant_id=uuid.uuid4(),
            tenant_timezone="UTC",
            did="+15550001111",
            caller_number=None,
            pbx_id=None,
            sip_trunk_id=None,
            pinned_rule_id=None,
        )

        assert result is not None
        assert result.agent_id == agent_high

    async def test_more_specific_rule_beats_catch_all_at_same_priority(self) -> None:
        """Specificity tie-break: a rule with a did condition beats an empty one."""
        catch_all_agent = uuid.uuid4()
        specific_agent = uuid.uuid4()
        catch_all = self._rule_row(name="catch-all", priority=100, agent_id=catch_all_agent, conditions={})
        specific = self._rule_row(name="specific", priority=100, agent_id=specific_agent, conditions={"did": "+15550001111"})
        # Both at the same priority — specific should win.
        session = self._mock_session([catch_all, specific])

        result = await resolve_route(
            session,
            tenant_id=uuid.uuid4(),
            tenant_timezone="UTC",
            did="+15550001111",
            caller_number=None,
            pbx_id=None,
            sip_trunk_id=None,
            pinned_rule_id=None,
        )

        assert result is not None
        assert result.agent_id == specific_agent

    async def test_closed_hours_raises_routing_closed_error(self) -> None:
        """Outside business hours + HANGUP closed_action → RoutingClosedError."""
        hours_id = uuid.uuid4()
        agent_id = uuid.uuid4()
        rule = self._rule_row(
            agent_id=agent_id,
            business_hours_id=hours_id,
            closed_action="HANGUP",
        )

        # Closed schedule: intervals for a day that is not today.
        # We freeze the weekday to Monday, intervals to Tuesday only.
        frozen = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)  # Monday noon UTC
        hours_rows = [
            {
                "id": hours_id,
                "timezone": "UTC",
                "holidays": [],
                "day_of_week": "TUESDAY",  # not today
                "opens_at": time(9, 0),
                "closes_at": time(17, 0),
            }
        ]
        session = self._mock_session([rule], hours=hours_rows)

        with patch("worker.routing.datetime") as mock_dt:
            mock_dt.now.return_value = frozen
            with pytest.raises(RoutingClosedError):
                await resolve_route(
                    session,
                    tenant_id=uuid.uuid4(),
                    tenant_timezone="UTC",
                    did="+15550001111",
                    caller_number=None,
                    pbx_id=None,
                    sip_trunk_id=None,
                    pinned_rule_id=None,
                )

    async def test_fallback_agent_exposed_in_decision(self) -> None:
        """``fallback_agent_id`` and ``fallback_action`` are surfaced in the decision."""
        primary = uuid.uuid4()
        fallback = uuid.uuid4()
        rule = self._rule_row(
            agent_id=primary,
            fallback_action="SECONDARY_AGENT",
            fallback_agent_id=fallback,
        )
        session = self._mock_session([rule])

        result = await resolve_route(
            session,
            tenant_id=uuid.uuid4(),
            tenant_timezone="UTC",
            did="+15550001111",
            caller_number=None,
            pbx_id=None,
            sip_trunk_id=None,
            pinned_rule_id=None,
        )

        assert result is not None
        assert result.fallback_agent_id == fallback
        assert result.fallback_action is not None
        assert result.fallback_action.value == "SECONDARY_AGENT"
