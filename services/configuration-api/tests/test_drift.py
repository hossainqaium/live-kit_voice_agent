"""Drift detection (spec 46, Plan 5.7).

The compare is the thing that must be right: deleting a LiveKit trunk that
PostgreSQL still names is ``DRIFTED``, a LiveKit object no row names is an
orphan, and a LiveKit outage must not flip every row to drifted.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from prometheus_client import CollectorRegistry
from shared.models import SyncStatus
from shared.telemetry.metrics import ControlPlaneMetrics

from app.livekit.drift import (
    DriftKind,
    DriftReport,
    apply_findings,
    compare_rules,
    compare_trunks,
    detect_drift,
    last_report,
    publish_metrics,
    reset_last_report,
)
from app.livekit.errors import LiveKitUnavailableError
from app.livekit.scheduler import next_delay_seconds, start_drift_checker
from app.livekit.sip import DispatchRuleSnapshot, SipResourceManager, TrunkSnapshot

_LIVEKIT_PAGE = (
    Path(__file__).parent.parent.parent.parent
    / "services"
    / "frontend"
    / "app"
    / "platform"
    / "livekit"
    / "page.tsx"
)
_PLATFORM_PAGE = (
    Path(__file__).parent.parent.parent.parent
    / "services"
    / "frontend"
    / "app"
    / "platform"
    / "page.tsx"
)


def _trunk(
    *,
    name: str = "pbx-trunk",
    livekit_id: str | None = "ST_1",
    numbers: list[str] | None = None,
    allowed_ips: list[str] | None = None,
    auth_username: str | None = "lkdev",
    status: SyncStatus = SyncStatus.SYNCED,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name=name,
        livekit_resource_id=livekit_id,
        allowed_ips=allowed_ips or ["192.168.0.10"],
        auth_username=auth_username,
        sync_status=status,
        sync_error=None,
        last_synced_at=None,
        _numbers=numbers or ["1801"],
    )


def _snapshot(trunk: SimpleNamespace, *, name: str | None = None) -> TrunkSnapshot:
    return TrunkSnapshot(
        livekit_trunk_id=trunk.livekit_resource_id or "ST_unknown",
        name=name or trunk.name,
        numbers=tuple(trunk._numbers),
        allowed_addresses=tuple(trunk.allowed_ips),
        auth_username=trunk.auth_username,
    )


def _rule(
    *,
    name: str = "inbound",
    livekit_id: str | None = "SDR_1",
    agent: str = "voice-agent",
    room_prefix: str | None = "call-",
    matched: list[str] | None = None,
    status: SyncStatus = SyncStatus.SYNCED,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name=name,
        livekit_resource_id=livekit_id,
        room_prefix=room_prefix,
        agent_dispatch_name=agent,
        matched_numbers=matched or [],
        sync_status=status,
        sync_error=None,
        last_synced_at=None,
    )


def _rule_snapshot(
    rule: SimpleNamespace, *, trunk_ids: list[str], name: str | None = None
) -> DispatchRuleSnapshot:
    return DispatchRuleSnapshot(
        livekit_rule_id=rule.livekit_resource_id or "SDR_unknown",
        name=name or rule.name,
        trunk_ids=tuple(trunk_ids),
        room_prefix=rule.room_prefix,
        agent_names=(rule.agent_dispatch_name,),
        inbound_numbers=tuple(rule.matched_numbers),
    )


@pytest.fixture(autouse=True)
def _isolate_last_report() -> Iterator[None]:
    reset_last_report()
    yield
    reset_last_report()


class TestCompareTrunks:
    def test_deleted_livekit_trunk_is_missing(self) -> None:
        """Exit criterion: a trunk removed in LiveKit is detected as drift."""
        row = _trunk()
        findings = compare_trunks(
            SipResourceManager(),
            [row],  # type: ignore[list-item]
            {row.id: row._numbers},
            [],
        )
        assert len(findings) == 1
        assert findings[0].kind is DriftKind.MISSING
        assert findings[0].row_id == row.id
        assert "ST_1" in findings[0].reason

    def test_matching_trunk_is_not_drift(self) -> None:
        row = _trunk()
        findings = compare_trunks(
            SipResourceManager(),
            [row],  # type: ignore[list-item]
            {row.id: row._numbers},
            [_snapshot(row)],
        )
        assert findings == []

    def test_number_list_mismatch_is_drift(self) -> None:
        row = _trunk(numbers=["1801"])
        live = _snapshot(row)
        live = TrunkSnapshot(
            livekit_trunk_id=live.livekit_trunk_id,
            name=live.name,
            numbers=("1801", "1802"),
            allowed_addresses=live.allowed_addresses,
            auth_username=live.auth_username,
        )
        findings = compare_trunks(
            SipResourceManager(),
            [row],  # type: ignore[list-item]
            {row.id: row._numbers},
            [live],
        )
        assert findings[0].kind is DriftKind.MISMATCH
        assert "numbers" in findings[0].reason

    def test_orphan_has_no_row(self) -> None:
        live = TrunkSnapshot(
            livekit_trunk_id="ST_orphan",
            name="leftover",
            numbers=("1801",),
            allowed_addresses=(),
            auth_username=None,
        )
        findings = compare_trunks(SipResourceManager(), [], {}, [live])
        assert findings[0].kind is DriftKind.ORPHAN
        assert findings[0].row_id is None
        assert findings[0].livekit_resource_id == "ST_orphan"

    def test_pending_row_without_livekit_id_is_ignored(self) -> None:
        row = _trunk(livekit_id=None, status=SyncStatus.PENDING)
        findings = compare_trunks(
            SipResourceManager(),
            [row],  # type: ignore[list-item]
            {row.id: row._numbers},
            [],
        )
        assert findings == []


class TestCompareRules:
    def test_deleted_rule_is_missing(self) -> None:
        row = _rule()
        findings = compare_rules(
            SipResourceManager(),
            [row],  # type: ignore[list-item]
            {row.id: ["ST_1"]},
            [],
        )
        assert findings[0].kind is DriftKind.MISSING

    def test_agent_name_mismatch(self) -> None:
        row = _rule(agent="voice-agent")
        live = _rule_snapshot(row, trunk_ids=["ST_1"])
        live = DispatchRuleSnapshot(
            livekit_rule_id=live.livekit_rule_id,
            name=live.name,
            trunk_ids=live.trunk_ids,
            room_prefix=live.room_prefix,
            agent_names=("other-worker",),
            inbound_numbers=live.inbound_numbers,
        )
        findings = compare_rules(
            SipResourceManager(),
            [row],  # type: ignore[list-item]
            {row.id: ["ST_1"]},
            [live],
        )
        assert findings[0].kind is DriftKind.MISMATCH
        assert "agents" in findings[0].reason

    def test_orphan_rule(self) -> None:
        live = DispatchRuleSnapshot(
            livekit_rule_id="SDR_orphan",
            name="stale",
            trunk_ids=("ST_1",),
            room_prefix="call-",
            agent_names=("voice-agent",),
            inbound_numbers=(),
        )
        findings = compare_rules(SipResourceManager(), [], {}, [live])
        assert findings[0].kind is DriftKind.ORPHAN


class TestApplyFindings:
    def test_missing_marks_drifted(self) -> None:
        row = _trunk()
        findings = compare_trunks(
            SipResourceManager(),
            [row],  # type: ignore[list-item]
            {row.id: row._numbers},
            [],
        )
        apply_findings([row], findings, now=datetime.now(UTC))  # type: ignore[list-item]
        assert row.sync_status is SyncStatus.DRIFTED
        assert row.sync_error is not None

    def test_match_recovers_drifted_to_synced(self) -> None:
        row = _trunk(status=SyncStatus.DRIFTED)
        row.sync_error = "was missing"
        now = datetime(2026, 9, 12, tzinfo=UTC)
        apply_findings([row], [], now=now)  # type: ignore[list-item]
        assert row.sync_status is SyncStatus.SYNCED
        assert row.sync_error is None
        assert row.last_synced_at == now

    def test_pending_without_id_is_untouched(self) -> None:
        row = _trunk(livekit_id=None, status=SyncStatus.PENDING)
        apply_findings([row], [], now=datetime.now(UTC))  # type: ignore[list-item]
        assert row.sync_status is SyncStatus.PENDING


class TestDetectDriftUnreachable:
    async def test_outage_does_not_write(self) -> None:
        session = AsyncMock()
        manager = MagicMock()
        manager.list_inbound_trunks = AsyncMock(
            side_effect=LiveKitUnavailableError("connection refused")
        )
        report = await detect_drift(session, manager)
        assert report.livekit_reachable is False
        assert report.configuration_drift_detected is False
        session.execute.assert_not_called()
        assert last_report() is report


class TestMetrics:
    def test_gauges_include_zero_after_repair(self) -> None:
        from prometheus_client import generate_latest

        registry = CollectorRegistry()
        metrics = ControlPlaneMetrics(registry=registry)
        report = DriftReport(
            checked_at=datetime.now(UTC),
            livekit_reachable=True,
            trunks_compared=1,
            rules_compared=1,
            findings=[],
        )
        publish_metrics(report, metrics)
        body = generate_latest(registry).decode()
        assert 'api_livekit_drift_detected{resource="sip_trunk"} 0.0' in body
        assert 'api_livekit_drift_detected{resource="orphan"} 0.0' in body


class TestScheduler:
    def test_jitter_is_non_negative(self) -> None:
        rng = random.Random(0)
        delay = next_delay_seconds(300.0, 30.0, rng=rng)
        assert 300.0 <= delay <= 330.0

    def test_zero_jitter_is_exact(self) -> None:
        assert next_delay_seconds(300.0, 0.0) == 300.0

    def test_negative_interval_is_refused(self) -> None:
        with pytest.raises(ValueError):
            next_delay_seconds(-1.0, 0.0)

    def test_disabled_does_not_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LIVEKIT_DRIFT_CHECK_ENABLED", "false")
        from app.core.settings import get_settings

        get_settings.cache_clear()
        assert start_drift_checker(ControlPlaneMetrics(registry=CollectorRegistry())) is None
        get_settings.cache_clear()


class TestDriftCheckRoute:
    async def test_check_requires_a_platform_session(self, client) -> None:
        response = await client.post("/api/v1/platform/livekit/drift-check")
        assert response.status_code == 401

    async def test_openapi_documents_the_check(self, client) -> None:
        paths = (await client.get("/openapi.json")).json()["paths"]
        assert "/api/v1/platform/livekit/drift-check" in paths
        assert "post" in paths["/api/v1/platform/livekit/drift-check"]


class TestConsoleCopy:
    def test_livekit_page_names_the_prd_phrase(self) -> None:
        if not _LIVEKIT_PAGE.exists():
            pytest.skip("services/frontend is not mounted in this container")
        src = _LIVEKIT_PAGE.read_text()
        assert "Configuration Drift Detected" in src
        assert "checkLivekitDrift" in src
        assert "orphans" in src

    def test_platform_dashboard_names_the_prd_phrase(self) -> None:
        if not _PLATFORM_PAGE.exists():
            pytest.skip("services/frontend is not mounted in this container")
        assert "Configuration Drift Detected" in _PLATFORM_PAGE.read_text()
