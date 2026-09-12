"""Invoke a granted tool and refuse everything else (spec 30, 32)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shared.logging import get_logger
from shared.tools import missing_required_arguments
from worker.tools.builtins import execute_builtin
from worker.tools.definitions import ToolDefinition
from worker.tools.http import ToolHttpError, execute_http

logger = get_logger(__name__)


class ToolDeniedError(RuntimeError):
    """The agent tried a tool that is not on its allow-list (spec 32)."""


class ToolRuntime:
    """Per-call tool dispatcher.

    The allow-list is the set of definitions on the call context. A name that
    is not in that set is denied even if the model invents the call — hiding
    the function from the prompt is not enough (spec 32).
    """

    def __init__(
        self,
        tools: tuple[ToolDefinition, ...],
        context: Any,
        factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._by_name = {tool.name: tool for tool in tools}
        self._context = context
        self._factory = factory
        self._counts: dict[str, int] = {}

    @property
    def granted_names(self) -> frozenset[str]:
        return frozenset(self._by_name)

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._by_name.values())

    def definition(self, name: str) -> ToolDefinition | None:
        return self._by_name.get(name)

    async def invoke(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        arguments = dict(arguments or {})
        definition = self._by_name.get(name)
        if definition is None:
            logger.warning("tool_denied_not_granted", extra={"tool": name})
            return {
                "ok": False,
                "error": "this agent is not allowed to use that tool",
            }

        missing = missing_required_arguments(definition.request_schema, arguments)
        if missing:
            return {
                "ok": False,
                "error": f"missing required argument(s): {', '.join(missing)}",
            }

        for key, denied in (definition.denied_arguments or {}).items():
            if key in arguments and arguments[key] == denied:
                return {"ok": False, "error": f"argument {key!r} is not permitted"}

        limit = definition.max_calls_per_conversation
        used = self._counts.get(name, 0)
        if limit is not None and used >= limit:
            return {
                "ok": False,
                "error": f"{name} may only be called {limit} time(s) on this call",
            }
        self._counts[name] = used + 1

        try:
            if definition.builtin:
                result = await execute_builtin(
                    definition, arguments, self._context, self._factory
                )
            else:
                result = await execute_http(definition, arguments, self._context)
        except ToolHttpError as exc:
            result = {"ok": False, "error": str(exc)}
        except Exception:
            logger.exception("tool_failed", extra={"tool": name})
            result = {"ok": False, "error": "the tool failed"}

        logger.info(
            "tool_invoked",
            extra={"tool": name, "ok": bool(result.get("ok"))},
        )
        return result
