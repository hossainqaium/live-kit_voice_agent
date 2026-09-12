"""Dispatch rules are long-lived and synced PostgreSQL-first (spec 21, 12).

Plan 5.3 / 5.4 / 5.5. The worker must never create a rule on the call path.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from shared.models import RoomStrategy

from app.api.v1 import dispatch_rules as rules_api
from app.schemas.telephony import DispatchRuleCreate, DispatchRuleUpdate


_PAGE = (
    Path(__file__).parent.parent.parent.parent
    / "services"
    / "frontend"
    / "app"
    / "dispatch-rules"
    / "page.tsx"
)
_SHELL = (
    Path(__file__).parent.parent.parent.parent
    / "services"
    / "frontend"
    / "components"
    / "Shell.tsx"
)
_WORKER = (
    Path(__file__).parent.parent.parent.parent
    / "services"
    / "ai-agent-worker"
)


class TestLongLivedOnly:
    def test_individual_is_the_default(self) -> None:
        payload = DispatchRuleCreate(name="ok", agent_dispatch_name="voice-agent")
        assert payload.room_strategy is RoomStrategy.INDIVIDUAL

    def test_schema_accepts_shared_so_the_handler_can_refuse_it(self) -> None:
        # A silent coerce to INDIVIDUAL would hide a tenant asking for a
        # shared room. The API returns 400 instead (spec 22).
        payload = DispatchRuleCreate(
            name="x",
            agent_dispatch_name="voice-agent",
            room_strategy=RoomStrategy.SHARED,
        )
        assert payload.room_strategy is RoomStrategy.SHARED

    def test_handler_refuses_shared(self) -> None:
        src = inspect.getsource(rules_api.create_rule)
        assert "INDIVIDUAL" in src
        assert "spec 22" in src

    def test_sync_is_postgres_then_livekit(self) -> None:
        src = inspect.getsource(rules_api._sync_to_livekit)
        assert "PENDING" in src
        assert "create_dispatch_rule" in src
        assert src.index("PENDING") < src.index("create_dispatch_rule")

    def test_create_writes_then_syncs(self) -> None:
        src = inspect.getsource(rules_api.create_rule)
        assert "repository.add" in src
        assert "_sync_to_livekit" in src
        assert src.index("repository.add") < src.index("_sync_to_livekit")

    def test_no_per_call_create_in_worker(self) -> None:
        if not _WORKER.exists():
            pytest.skip("worker sources are not mounted")
        for path in _WORKER.rglob("*.py"):
            text = path.read_text()
            assert "create_sip_dispatch_rule" not in text
            assert "CreateSIPDispatchRuleRequest" not in text


class TestAuditAndRoutes:
    async def test_openapi_lists_the_resource(self, client) -> None:
        paths = (await client.get("/openapi.json")).json()["paths"]
        assert "/api/v1/dispatch-rules" in paths
        assert "/api/v1/dispatch-rules/{rule_id}/sync" in paths

    async def test_writes_require_auth(self, client) -> None:
        response = await client.post(
            "/api/v1/dispatch-rules",
            json={"name": "x", "agent_dispatch_name": "voice-agent"},
        )
        assert response.status_code == 401

    def test_create_is_audited(self) -> None:
        assert "audit.record" in inspect.getsource(rules_api.create_rule)
        assert "dispatch_rule.created" in inspect.getsource(rules_api.create_rule)

    def test_update_schema_has_agent_dispatch(self) -> None:
        assert "agent_dispatch_name" in DispatchRuleUpdate.model_fields
        assert "sip_trunk_id" in DispatchRuleUpdate.model_fields


class TestConsole:
    def test_page_exists_or_skip(self) -> None:
        if not _PAGE.exists():
            pytest.skip("services/frontend is not mounted in this container")
        src = _PAGE.read_text()
        assert "never created per call" in src or "Never created per call" in src
        assert "dispatchRules.create" in src
        assert "dispatchRules.sync" in src
        assert "WORKER_AGENT_NAME" in src
        assert "486 flood" in src

    def test_nav_lists_the_page(self) -> None:
        if not _SHELL.exists():
            pytest.skip("services/frontend is not mounted in this container")
        assert "/dispatch-rules" in _SHELL.read_text()
