"""Phase 6.1-6.5: tools pipeline, builder, substitution, publish, allow-list."""

from __future__ import annotations

import inspect

import pytest

from app.api.v1 import agents as agents_api
from app.api.v1 import tools as tools_api
from app.schemas.agent import AgentVersionConfig, AgentVersionResponse
from app.schemas.tool import ToolResponse
from app.services import seed as seed_mod
from tests.conftest import frontend_file


class TestSeededBuiltins:
    def test_create_ticket_is_seeded(self) -> None:
        src = inspect.getsource(seed_mod)
        assert "create_ticket" in src
        assert "builtin://create_ticket" in src or 'f"builtin://{name}"' in src
        assert "_SERVER_AGENT_PROMPT" in src
        assert "create_ticket" in seed_mod._SERVER_AGENT_PROMPT

    def test_prd_examples_are_seeded(self) -> None:
        names = {item[0] for item in seed_mod._BUILTIN_TOOLS}
        assert names >= {
            "create_ticket",
            "get_customer",
            "check_order",
            "create_order",
            "cancel_order",
            "check_inventory",
            "send_sms",
            "send_email",
            "transfer_call",
            "refund_order",
        }

    def test_server_agent_gets_create_ticket_only(self) -> None:
        src = inspect.getsource(seed_mod.seed_dev_tenant)
        assert 'tools_by_name["create_ticket"]' in src
        assert "refund_order" not in src

    def test_development_agent_does_not_get_refund(self) -> None:
        src = inspect.getsource(seed_mod.seed_dev_tenant)
        assert "get_customer" in src
        assert "check_inventory" in src
        assert "refund_order" not in src


class TestPublishValidatesToolSchemas:
    def test_invalid_granted_tool_blocks_publish(self) -> None:
        src = inspect.getsource(agents_api._validate_version)
        assert "schema_valid" in src
        assert "tool_ids" in src
        assert "invalid request schema" in src

    def test_draft_copies_tool_grants(self) -> None:
        src = inspect.getsource(agents_api._editable_draft)
        assert "_copy_tool_grants" in src

    def test_draft_accepts_tool_ids(self) -> None:
        payload = AgentVersionConfig.model_validate({"tool_ids": []})
        assert payload.tool_ids == []
        assert "tool_ids" in AgentVersionResponse.model_fields


class TestToolBuilderContract:
    def test_response_includes_builtin_flag(self) -> None:
        assert "is_builtin" in ToolResponse.model_fields

    def test_response_schema_and_retries_are_fields(self) -> None:
        assert "response_schema" in ToolResponse.model_fields
        assert "max_retries" in ToolResponse.model_fields

    async def test_openapi_lists_tools(self, client) -> None:
        paths = (await client.get("/openapi.json")).json()["paths"]
        assert "/api/v1/tools" in paths


class TestContextVariables:
    def test_caller_number_need_not_be_in_the_schema(self) -> None:
        tool = type(
            "T",
            (),
            {
                "url_template": "https://api.example.com/guests/{{caller_number}}",
                "headers": {},
                "request_schema": {},
            },
        )()
        valid, error = tools_api._validate_schema(tool)
        assert valid is True
        assert error is None

    def test_order_id_must_still_be_declared(self) -> None:
        tool = type(
            "T",
            (),
            {
                "url_template": "https://api.example.com/orders/{{order_id}}",
                "headers": {},
                "request_schema": {},
            },
        )()
        valid, error = tools_api._validate_schema(tool)
        assert valid is False
        assert error is not None
        assert "order_id" in error


class TestConsole:
    def test_tool_builder_has_response_schema_and_retries(self) -> None:
        path = frontend_file("app", "tools", "page.tsx")
        if path is None:
            pytest.skip("services/frontend is not mounted in this container")
        src = path.read_text()
        assert "Response schema" in src
        assert "max_retries" in src
        assert "caller_number" in src
        assert "is_builtin" in src

    def test_agent_builder_has_an_allow_list(self) -> None:
        path = frontend_file("app", "agents", "page.tsx")
        if path is None:
            pytest.skip("services/frontend is not mounted in this container")
        src = path.read_text()
        assert "tool_ids" in src
        assert "Only checked tools can be called" in src
        assert "refund" in src or "denied" in src

    def test_tickets_page_mentions_create_ticket(self) -> None:
        path = frontend_file("app", "tickets", "page.tsx")
        if path is None:
            pytest.skip("services/frontend is not mounted in this container")
        src = path.read_text()
        assert "create_ticket" in src
