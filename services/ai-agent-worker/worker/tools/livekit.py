"""Register granted tools with LiveKit Agents (spec 30)."""

from __future__ import annotations

from typing import Any

from worker.tools.definitions import ToolDefinition
from worker.tools.executor import ToolRuntime


def livekit_tools(runtime: ToolRuntime) -> list[Any]:
    """Function tools the model may call on this session.

    Built from the allow-list only. A tool that is not granted is never
    registered, and ``runtime.invoke`` still denies it if the model names it.
    """
    try:
        from livekit.agents import function_tool
    except ImportError:
        return []

    registered: list[Any] = []
    for definition in runtime.definitions:
        registered.append(_bind(definition, runtime, function_tool))
    return registered


def _bind(definition: ToolDefinition, runtime: ToolRuntime, function_tool: Any) -> Any:
    schema = {
        "name": definition.name,
        "description": definition.description,
        "parameters": definition.request_schema
        or {"type": "object", "properties": {}},
    }
    if schema["parameters"].get("type") is None:
        schema["parameters"] = {**schema["parameters"], "type": "object"}

    async def _invoke(raw_arguments: dict[str, object], context: Any = None) -> dict[str, Any]:
        return await runtime.invoke(definition.name, dict(raw_arguments or {}))

    _invoke.__name__ = definition.name
    _invoke.__doc__ = definition.description
    return function_tool(raw_schema=schema)(_invoke)
