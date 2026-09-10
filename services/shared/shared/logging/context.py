"""Correlation context propagated through a call's lifetime.

Spec 43 requires ``call_id`` to be present in every call-related log line across
every service, and 58 requires the surrounding identifiers alongside it. Rather
than threading these through every function signature, they live in context
variables that the JSON formatter reads automatically.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from types import MappingProxyType
from typing import Any

# Identifiers required on every call-related event (spec 58).
_CORRELATION_FIELDS = (
    "tenant_id",
    "call_id",
    "room_id",
    "agent_id",
    "agent_version_id",
    "request_id",
    "user_id",
)

#: Immutable default. A mutable one would be shared by every context, so a
#: single in-place mutation anywhere would leak correlation fields between
#: unrelated calls.
_EMPTY: Mapping[str, Any] = MappingProxyType({})

_context: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar(
    "voice_agent_log_context", default=_EMPTY
)


def get_context() -> dict[str, Any]:
    """Return the correlation fields currently in scope."""
    return dict(_context.get())


def bind(**fields: Any) -> contextvars.Token:
    """Merge ``fields`` into the correlation context.

    Returns the token needed to restore the previous context. Prefer the
    :func:`log_context` context manager unless you need manual control.
    """
    unknown = set(fields) - set(_CORRELATION_FIELDS)
    if unknown:
        raise ValueError(
            f"unknown correlation field(s): {sorted(unknown)}; "
            f"allowed: {sorted(_CORRELATION_FIELDS)}"
        )
    merged = {**_context.get(), **{k: v for k, v in fields.items() if v is not None}}
    return _context.set(merged)


def reset(token: contextvars.Token) -> None:
    """Restore the context captured before a matching :func:`bind`."""
    _context.reset(token)


def clear() -> contextvars.Token:
    """Drop all correlation fields (used at worker job boundaries)."""
    return _context.set({})


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Bind correlation fields for the duration of the block.

    >>> with log_context(call_id="call_123", tenant_id="tenant_001"):
    ...     logger.info("tts_started")
    """
    token = bind(**fields)
    try:
        yield
    finally:
        reset(token)
