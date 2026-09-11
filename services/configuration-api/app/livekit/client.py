"""LiveKit admin API client.

A dedicated module rather than scattered SDK calls (spec 80 rule 7). Two jobs:

1. Own the connection and credentials, so no other module needs the LiveKit
   API secret.
2. Translate SDK and transport failures into the error types in
   :mod:`app.livekit.errors`, so callers can tell "your configuration is
   wrong" from "LiveKit is down" from "the resource has drifted".
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TypeVar

import aiohttp
from livekit.api import ListRoomsRequest, LiveKitAPI, TwirpError

from app.core.settings import get_settings
from app.livekit.errors import (
    LiveKitRejectedError,
    LiveKitResourceMissingError,
    LiveKitUnavailableError,
    LiveKitUnsupportedError,
)
from shared.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

#: Twirp codes that mean the request itself is wrong. Retrying cannot help.
_CLIENT_ERROR_CODES = frozenset(
    {
        "invalid_argument",
        "malformed",
        "permission_denied",
        "unauthenticated",
        "already_exists",
        "failed_precondition",
        "out_of_range",
    }
)

#: Twirp codes meaning the resource is absent — the drift signal (spec 46).
_NOT_FOUND_CODES = frozenset({"not_found"})

#: The server has no route for the call. Means a version gap, not a bad
#: request, so the caller can fall back rather than fail.
_UNSUPPORTED_CODES = frozenset({"bad_route", "unimplemented"})


class LiveKitAdminClient:
    """Thin, typed wrapper over the LiveKit server API.

    The SDK opens its own aiohttp session, so instances are created per unit of
    work and closed afterwards rather than held process-wide. LiveKit admin
    calls are rare — configuration changes, not call traffic — so the
    connection setup cost is irrelevant, and a long-lived session that dies
    silently is worse than a fresh one.
    """

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
        *,
        timeout_seconds: float = 10.0,
    ) -> None:
        settings = get_settings()
        # LiveKit's admin API is HTTP; the configured URL is a websocket URL
        # because that is what clients use for signalling.
        self._url = (
            (url or settings.livekit_url).replace("ws://", "http://").replace("wss://", "https://")
        )
        self._api_key = api_key or settings.livekit_api_key.get_secret_value()
        self._api_secret = api_secret or settings.livekit_api_secret.get_secret_value()
        self._timeout = timeout_seconds

    @asynccontextmanager
    async def session(self) -> AsyncIterator[LiveKitAPI]:
        """Yield a connected API handle and always close it."""
        api = LiveKitAPI(url=self._url, api_key=self._api_key, api_secret=self._api_secret)
        try:
            yield api
        finally:
            await api.aclose()

    async def call(self, operation: str, fn: Callable[[LiveKitAPI], Awaitable[T]]) -> T:
        """Run one admin operation, translating failures.

        ``operation`` names the call for logs and error messages. Without it,
        a failure reads as "LiveKit returned 500" with no indication of what
        was being attempted.
        """
        async with self.session() as api:
            try:
                return await asyncio.wait_for(fn(api), timeout=self._timeout)

            except TimeoutError as exc:
                raise LiveKitUnavailableError(
                    f"{operation}: LiveKit did not respond within {self._timeout}s"
                ) from exc

            except TwirpError as exc:
                code = getattr(exc, "code", "") or ""
                message = f"{operation}: {getattr(exc, 'message', str(exc))}"

                if code in _UNSUPPORTED_CODES:
                    raise LiveKitUnsupportedError(
                        f"{message} — this LiveKit build does not implement the operation"
                    ) from exc
                if code in _NOT_FOUND_CODES:
                    raise LiveKitResourceMissingError(message) from exc
                if code in _CLIENT_ERROR_CODES:
                    raise LiveKitRejectedError(f"{message} (code={code})") from exc
                # Unknown or server-side codes: treat as transient. Assuming
                # otherwise would mark a row FAILED during a LiveKit restart.
                raise LiveKitUnavailableError(f"{message} (code={code})") from exc

            except aiohttp.ClientError as exc:
                raise LiveKitUnavailableError(f"{operation}: {exc}") from exc

    async def room_count(self) -> int:
        """How many rooms LiveKit currently holds.

        The closest thing LiveKit will report to live concurrency, and what the
        capacity dashboard (spec 48) shows next to the database's own count of
        in-flight calls. A disagreement between the two is the useful signal:
        it means a call ended without the worker writing its terminal state.
        """
        rooms = await self.call("room_count", lambda api: api.room.list_rooms(ListRoomsRequest()))
        return len(getattr(rooms, "rooms", None) or [])

    async def health(self) -> bool:
        """Whether the LiveKit admin API is answering.

        Used by the capacity dashboard (spec 48) rather than by readiness: the
        Configuration API still serves configuration when LiveKit is down, and
        failing readiness would take the Control Plane out of rotation during a
        media-layer incident.
        """
        try:
            await self.call("health", lambda api: api.room.list_rooms(ListRoomsRequest()))
        except Exception as exc:
            logger.warning("livekit_health_check_failed", extra={"detail": str(exc)})
            return False
        return True
