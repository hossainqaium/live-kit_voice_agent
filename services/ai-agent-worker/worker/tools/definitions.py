"""Resolved tool definitions held on a call (spec 30, 31, 32)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from shared.models import HttpMethod, ToolAuthType
from shared.tools import CONTEXT_VARIABLES, is_builtin


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """One tool this call's agent is allowed to invoke.

    Loaded once with the rest of the call configuration (spec 45). Absence
    from this tuple is a denial (spec 32): the worker never registers a tool
    that is not on the version's allow-list.
    """

    id: uuid.UUID
    name: str
    description: str
    url_template: str
    http_method: HttpMethod = HttpMethod.GET
    headers: dict[str, str] = field(default_factory=dict)
    request_schema: dict[str, Any] = field(default_factory=dict)
    response_schema: dict[str, Any] = field(default_factory=dict)
    auth_type: ToolAuthType = ToolAuthType.NONE
    auth_header_name: str | None = None
    auth_secret: str | None = None
    timeout_seconds: int = 5
    max_retries: int = 1
    max_calls_per_conversation: int | None = 5
    denied_arguments: dict[str, Any] = field(default_factory=dict)

    @property
    def builtin(self) -> bool:
        return is_builtin(self.url_template)


def call_variables(context: Any) -> dict[str, Any]:
    """Values every tool can substitute without the model supplying them."""
    values: dict[str, Any] = {
        "caller_number": getattr(context, "caller_number", None),
        "call_id": getattr(context, "call_id", None),
        "did": getattr(context, "did", None),
        "tenant_id": str(getattr(context, "tenant_id", "")),
        "agent_name": getattr(context, "agent_name", None),
    }
    return {key: value for key, value in values.items() if key in CONTEXT_VARIABLES}
