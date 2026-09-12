"""Background Synchronize / Retry / Repair (spec 12, 80). Plan 5.6 + 5.9."""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from shared.models import RoomStrategy, SyncStatus

from app.api.v1 import dispatch_rules as rules_api
from app.api.v1 import sip_trunks as trunks_api
from app.livekit import jobs
from app.livekit.errors import LiveKitError, LiveKitResourceMissingError
from app.livekit.sip import TrunkSnapshot


def _trunk(**overrides):
    row = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="Office",
        livekit_resource_id="ST_old",
        sync_status=SyncStatus.DRIFTED,
        sync_error="gone",
        sync_attempts=2,
        last_synced_at=None,
        allowed_ips=[],
        auth_username=None,
    )
    row.__dict__.update(overrides)
    return row


def _rule(**overrides):
    row = SimpleNamespace(
        id=uuid.uuid4(),
        sip_trunk_id=uuid.uuid4(),
        room_strategy=RoomStrategy.INDIVIDUAL,
        livekit_resource_id="SDR_old",
        sync_status=SyncStatus.DRIFTED,
        sync_error="gone",
        sync_attempts=1,
        last_synced_at=None,
    )
    row.__dict__.update(overrides)
    return row


def _session(*gets):
    session = MagicMock()
    session.flush = AsyncMock()
    session.get = AsyncMock(side_effect=list(gets) or [None])
    return session


class TestApplyTrunkSync:
    @pytest.mark.asyncio
    async def test_synchronize_updates_in_place(self, monkeypatch: pytest.MonkeyPatch) -> None:
        trunk = _trunk()
        snapshot = TrunkSnapshot(
            livekit_trunk_id="ST_old",
            name="Office",
            numbers=(),
            allowed_addresses=(),
            auth_username=None,
        )
        manager = MagicMock()
        manager.update_inbound_trunk = AsyncMock(return_value=snapshot)
        manager.create_inbound_trunk = AsyncMock()
        manager.delete_inbound_trunk = AsyncMock()
        monkeypatch.setattr(jobs, "accepted_numbers", AsyncMock(return_value=["1801"]))
        monkeypatch.setattr(jobs, "decrypt_trunk_password", AsyncMock(return_value=None))

        await jobs.apply_trunk_sync(_session(), trunk, repair=False, manager=manager)

        manager.update_inbound_trunk.assert_awaited_once()
        manager.create_inbound_trunk.assert_not_awaited()
        manager.delete_inbound_trunk.assert_not_awaited()
        assert trunk.sync_status is SyncStatus.SYNCED
        assert trunk.livekit_resource_id == "ST_old"

    @pytest.mark.asyncio
    async def test_repair_deletes_then_creates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        trunk = _trunk()
        snapshot = TrunkSnapshot(
            livekit_trunk_id="ST_new",
            name="Office",
            numbers=(),
            allowed_addresses=(),
            auth_username=None,
        )
        manager = MagicMock()
        manager.delete_inbound_trunk = AsyncMock()
        manager.create_inbound_trunk = AsyncMock(return_value=snapshot)
        manager.update_inbound_trunk = AsyncMock()
        monkeypatch.setattr(jobs, "accepted_numbers", AsyncMock(return_value=["1801"]))
        monkeypatch.setattr(jobs, "decrypt_trunk_password", AsyncMock(return_value=None))

        await jobs.apply_trunk_sync(_session(), trunk, repair=True, manager=manager)

        manager.delete_inbound_trunk.assert_awaited_once_with("ST_old")
        manager.create_inbound_trunk.assert_awaited_once()
        manager.update_inbound_trunk.assert_not_awaited()
        assert trunk.livekit_resource_id == "ST_new"
        assert trunk.sync_status is SyncStatus.SYNCED
        assert trunk.sync_attempts == 0

    @pytest.mark.asyncio
    async def test_repair_clears_id_when_livekit_already_gone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        trunk = _trunk()
        snapshot = TrunkSnapshot(
            livekit_trunk_id="ST_new",
            name="Office",
            numbers=(),
            allowed_addresses=(),
            auth_username=None,
        )
        manager = MagicMock()
        manager.delete_inbound_trunk = AsyncMock(
            side_effect=LiveKitResourceMissingError("gone", resource_id="ST_old")
        )
        manager.create_inbound_trunk = AsyncMock(return_value=snapshot)
        monkeypatch.setattr(jobs, "accepted_numbers", AsyncMock(return_value=[]))
        monkeypatch.setattr(jobs, "decrypt_trunk_password", AsyncMock(return_value=None))

        await jobs.apply_trunk_sync(_session(), trunk, repair=True, manager=manager)

        assert trunk.livekit_resource_id == "ST_new"
        assert trunk.sync_status is SyncStatus.SYNCED

    @pytest.mark.asyncio
    async def test_synchronize_missing_id_fails_so_repair_can_recreate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        trunk = _trunk()
        manager = MagicMock()
        manager.update_inbound_trunk = AsyncMock(
            side_effect=LiveKitResourceMissingError("gone", resource_id="ST_old")
        )
        manager.create_inbound_trunk = AsyncMock()
        monkeypatch.setattr(jobs, "accepted_numbers", AsyncMock(return_value=[]))
        monkeypatch.setattr(jobs, "decrypt_trunk_password", AsyncMock(return_value=None))

        await jobs.apply_trunk_sync(_session(), trunk, repair=False, manager=manager)

        manager.create_inbound_trunk.assert_not_awaited()
        assert trunk.sync_status is SyncStatus.FAILED
        assert trunk.sync_attempts == 3

    @pytest.mark.asyncio
    async def test_livekit_error_is_failed_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        trunk = _trunk(livekit_resource_id=None)
        manager = MagicMock()
        manager.create_inbound_trunk = AsyncMock(side_effect=LiveKitError("down"))
        monkeypatch.setattr(jobs, "accepted_numbers", AsyncMock(return_value=[]))
        monkeypatch.setattr(jobs, "decrypt_trunk_password", AsyncMock(return_value=None))

        await jobs.apply_trunk_sync(_session(), trunk, manager=manager)

        assert trunk.sync_status is SyncStatus.FAILED
        assert "down" in (trunk.sync_error or "")


class TestApplyDispatchRuleSync:
    @pytest.mark.asyncio
    async def test_creates_after_delete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rule = _rule()
        trunk = SimpleNamespace(livekit_resource_id="ST_1")
        manager = MagicMock()
        manager.delete_dispatch_rule = AsyncMock()
        manager.create_dispatch_rule = AsyncMock(
            return_value=SimpleNamespace(livekit_rule_id="SDR_new")
        )
        session = _session(trunk)

        await jobs.apply_dispatch_rule_sync(session, rule, repair=True, manager=manager)

        manager.delete_dispatch_rule.assert_awaited_once_with("SDR_old")
        assert rule.livekit_resource_id == "SDR_new"
        assert rule.sync_status is SyncStatus.SYNCED


class TestHandlersAreAsync:
    def test_trunk_create_commits_before_enqueue(self) -> None:
        src = inspect.getsource(trunks_api.create_trunk)
        assert "enqueue_trunk_sync" in src
        assert src.index("commit") < src.index("enqueue_trunk_sync")
        assert "await apply_trunk_sync" not in src
        assert "create_inbound_trunk" not in src

    def test_rule_create_commits_before_enqueue(self) -> None:
        src = inspect.getsource(rules_api.create_rule)
        assert src.index("commit") < src.index("enqueue_dispatch_rule_sync")
        assert "create_dispatch_rule" not in src

    def test_trunk_repair_route_enqueues_repair(self) -> None:
        src = inspect.getsource(trunks_api.repair_trunk)
        assert "repair=True" in src

    async def test_openapi_lists_the_three_actions(self, client) -> None:
        paths = (await client.get("/openapi.json")).json()["paths"]
        assert "/api/v1/sip-trunks/{trunk_id}/sync" in paths
        assert "/api/v1/sip-trunks/{trunk_id}/retry" in paths
        assert "/api/v1/sip-trunks/{trunk_id}/repair" in paths
        assert "/api/v1/platform/livekit/{kind}/{resource_id}/{action}" in paths
