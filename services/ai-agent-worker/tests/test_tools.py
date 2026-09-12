"""Function calling: builtins, HTTP, substitution, allow-list (spec 30-32)."""

from __future__ import annotations

import inspect
import uuid
from typing import Any
from unittest.mock import AsyncMock

import httpx

from shared.models import ProviderKind, TicketSource
from worker.config_loader import CallContext, CallPolicy, TransferPolicy
from worker.entrypoint import ConfigurableAgent, _build_agent
from worker.providers.base import ProviderConfig
from worker.tools.builtins import HANDLERS, create_ticket, get_customer, refund_order
from worker.tools.definitions import ToolDefinition
from worker.tools.executor import ToolRuntime
from worker.tools.http import execute_http


def _provider(kind: ProviderKind = ProviderKind.STT) -> ProviderConfig:
    return ProviderConfig(kind=kind, provider="openai_compatible", model="test")


def _context(**overrides: Any) -> CallContext:
    values = {
        "call_id": "call_test",
        "call_row_id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "tenant_slug": "dev",
        "agent_id": uuid.uuid4(),
        "agent_version_id": uuid.uuid4(),
        "agent_name": "Service Agent",
        "version_number": 1,
        "room_name": "room",
        "did": "1801",
        "caller_number": "+15559990000",
        "sip_trunk_id": None,
        "pbx_id": None,
        "language": "en",
        "greeting": None,
        "system_prompt": "File tickets.",
        "stt": _provider(ProviderKind.STT),
        "llm": _provider(ProviderKind.LLM),
        "tts": _provider(ProviderKind.TTS),
        "call_policy": CallPolicy(
            silence_timeout_seconds=None,
            max_call_duration_seconds=None,
            interruption_enabled=True,
            interruption_min_words=0,
            recording_enabled=False,
            transcription_enabled=False,
        ),
        "transfer_policy": TransferPolicy(
            enabled=False,
            announcement_text=None,
            hold_media_object_key=None,
            summary_template=None,
            summary_max_seconds=30,
            skip_dtmf=None,
        ),
    }
    values.update(overrides)
    return CallContext(**values)


def _tool(name: str, **overrides: Any) -> ToolDefinition:
    values = {
        "id": uuid.uuid4(),
        "name": name,
        "description": f"Call {name}",
        "url_template": f"builtin://{name}",
        "request_schema": {"type": "object", "properties": {}, "required": []},
    }
    values.update(overrides)
    return ToolDefinition(**values)


class _FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one(self) -> Any:
        return self._value


class _FakeSession:
    def __init__(self, next_number: int = 1) -> None:
        self.next_number = next_number
        self.inserts: list[dict[str, Any]] = []

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        sql = str(statement)
        if "MAX" in sql or "SUBSTRING" in sql:
            return _FakeResult(self.next_number)
        if params is not None and "ticket_number" in params:
            self.inserts.append(params)
        return _FakeResult(None)

    async def commit(self) -> None:
        return None

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class TestAllowList:
    async def test_ungranted_tool_is_denied(self) -> None:
        runtime = ToolRuntime((_tool("get_customer"),), _context())
        result = await runtime.invoke("refund_order", {"order_id": "ORD-100"})
        assert result["ok"] is False
        assert "not allowed" in result["error"]

    async def test_granted_tool_runs(self) -> None:
        runtime = ToolRuntime((_tool("get_customer"),), _context())
        result = await runtime.invoke("get_customer", {"customer_id": "1001"})
        assert result["ok"] is True
        assert result["customer"]["name"] == "Ada Lovelace"

    async def test_conversation_limit(self) -> None:
        tool = _tool("get_customer", max_calls_per_conversation=1)
        runtime = ToolRuntime((tool,), _context())
        first = await runtime.invoke("get_customer", {"customer_id": "1001"})
        second = await runtime.invoke("get_customer", {"customer_id": "1001"})
        assert first["ok"] is True
        assert second["ok"] is False
        assert "only be called" in second["error"]


class TestCreateTicket:
    async def test_writes_an_agent_ticket(self) -> None:
        session = _FakeSession(next_number=1)
        context = _context()
        result = await create_ticket(
            _tool("create_ticket"),
            {"title": "Lift stuck", "description": "Floor 3", "priority": "HIGH"},
            context,
            factory=lambda: session,  # type: ignore[arg-type]
        )
        assert result["ok"] is True
        assert result["ticket_number"] == "TCK-0002"
        row = session.inserts[0]
        assert row["source"] == TicketSource.AGENT.value
        assert row["agent_id"] == context.agent_id
        assert row["call_id"] == context.call_row_id
        assert row["caller_number"] == context.caller_number
        assert row["title"] == "Lift stuck"

    async def test_requires_title_and_description(self) -> None:
        result = await create_ticket(_tool("create_ticket"), {"title": ""}, _context(), None)
        assert result["ok"] is False


class TestSandboxBuiltins:
    def test_prd_examples_are_registered(self) -> None:
        for name in (
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
        ):
            assert name in HANDLERS

    async def test_refund_exists_so_it_can_be_denied(self) -> None:
        result = await refund_order(_tool("refund_order"), {"order_id": "ORD-100"}, _context(), None)
        assert result["ok"] is True

    async def test_unknown_customer(self) -> None:
        result = await get_customer(_tool("get_customer"), {"customer_id": "nope"}, _context(), None)
        assert result["ok"] is False


class TestHttpAndSubstitution:
    async def test_caller_number_is_substituted(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"status": "ok", "extra": "drop"})

        tool = _tool(
            "lookup",
            url_template="https://api.example.com/guests/{{caller_number}}",
            request_schema={"type": "object", "properties": {}},
            response_schema={"type": "object", "properties": {"status": {"type": "string"}}},
        )
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await execute_http(tool, {}, _context(), client=client)
        assert result["ok"] is True
        assert captured["url"] == "https://api.example.com/guests/+15559990000"
        assert result["result"] == {"status": "ok"}

    async def test_unresolved_variable(self) -> None:
        tool = _tool(
            "lookup",
            url_template="https://api.example.com/orders/{{order_id}}",
        )
        result = await execute_http(tool, {}, _context(), client=AsyncMock())
        assert result["ok"] is False
        assert "order_id" in result["error"]


class TestPipelineWiring:
    def test_build_agent_registers_tools(self) -> None:
        src = inspect.getsource(ConfigurableAgent.__init__)
        assert "livekit_tools" in src
        assert "tools=tools" in src
        assert "ConfigurableAgent" in inspect.getsource(_build_agent)

    def test_config_loader_loads_granted_tools(self) -> None:
        from worker import config_loader
        from worker.config_loader import CallConfigLoader

        src = inspect.getsource(CallConfigLoader._load_tools)
        module = inspect.getsource(config_loader)
        assert "_TOOLS_SQL" in src
        assert "agent_tools" in module
        assert "tool_permission_denied" in src

    def test_redacted_context_lists_tool_names(self) -> None:
        context = _context(tools=(_tool("create_ticket"),))
        assert context.redacted()["tools"] == ["create_ticket"]
