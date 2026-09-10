"""JSON log formatting (spec 58).

Every record is emitted as a single JSON object carrying ``service``,
``timestamp`` and ``event``, plus whatever correlation fields are in scope.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import sys
import traceback
from typing import Any

from .context import get_context

# Attributes LogRecord always carries; anything else was passed by the caller
# via ``extra=`` and belongs in the emitted document.
_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)

# Field names whose values must never reach the logs (spec 54).
_REDACT_HINTS = ("password", "secret", "token", "api_key", "apikey", "credential", "authorization")
_REDACTED = "***redacted***"


def _redact(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(hint in lowered for hint in _REDACT_HINTS):
        return _REDACTED
    return value


class JsonFormatter(logging.Formatter):
    """Render a ``LogRecord`` as one line of JSON."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        # The log message doubles as the event name, so call sites read as
        # logger.info("tts_started") rather than prose.
        document: dict[str, Any] = {
            "service": self.service,
            "timestamp": _dt.datetime.fromtimestamp(record.created, tz=_dt.UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
            "logger": record.name,
        }

        document.update(get_context())

        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                document[key] = _redact(key, value)

        if record.exc_info:
            exc_type, exc_value, exc_tb = record.exc_info
            document["error"] = {
                "type": getattr(exc_type, "__name__", str(exc_type)),
                "message": str(exc_value),
                "stack": "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
            }

        return json.dumps(document, default=str, separators=(",", ":"))


def configure_logging(service: str | None = None, level: str | None = None) -> logging.Logger:
    """Install the JSON formatter on the root logger.

    Called once at process start by every service. ``service`` and ``level``
    fall back to the ``SERVICE_NAME`` and ``LOG_LEVEL`` environment variables.
    """
    service = service or os.getenv("SERVICE_NAME", "unknown")
    level = (level or os.getenv("LOG_LEVEL", "info")).upper()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Third-party libraries that install their own handlers would otherwise
    # emit every line twice: once through our formatter and once through
    # theirs. Clearing and propagating routes them through ours instead.
    for name in (
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "livekit",
        "livekit.agents",
    ):
        third_party = logging.getLogger(name)
        third_party.handlers.clear()
        third_party.propagate = True

    return root


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger. Configuration is global."""
    return logging.getLogger(name)
