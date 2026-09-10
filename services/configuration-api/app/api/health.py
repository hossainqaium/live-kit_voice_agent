"""Liveness and readiness endpoints (spec 59).

``/health`` answers "is this process alive" and must never touch a dependency.
``/ready`` answers "can this process serve traffic" and therefore does verify
its required dependencies.

The distinction matters operationally: a failing liveness probe gets the
container killed, so making it depend on PostgreSQL would turn a brief database
blip into a cluster-wide restart storm.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Literal

import redis.asyncio as aioredis
from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.core.settings import get_settings
from app.db.session import get_engine
from shared.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["health"])

CheckState = Literal["ok", "error", "timeout"]


async def _check_postgres() -> tuple[CheckState, str | None]:
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return "ok", None
    except Exception as exc:
        return "error", str(exc)


async def _check_redis() -> tuple[CheckState, str | None]:
    settings = get_settings()
    # Constructing the client is separated from using it so the cleanup below
    # can never run against an unbound name: from_url does not connect, but a
    # malformed URL raises here rather than at ping time.
    try:
        client = aioredis.from_url(settings.redis_url)
    except Exception as exc:
        return "error", f"invalid redis url: {exc}"

    try:
        await client.ping()
        return "ok", None
    except Exception as exc:
        return "error", str(exc)
    finally:
        await client.aclose()


#: Dependencies whose failure means this service cannot serve requests.
#: LiveKit and object storage are deliberately absent: the API still serves
#: configuration reads and writes when they are down, and their health is
#: surfaced on the capacity dashboard instead (spec 48).
_REQUIRED_CHECKS = {
    "postgresql": _check_postgres,
    "redis": _check_redis,
}


@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, Any]:
    """Report that the process is running. No dependencies are consulted."""
    settings = get_settings()
    return {
        "status": "ok",
        "service": settings.service_name,
        "environment": settings.environment,
    }


@router.get("/ready", summary="Readiness probe")
async def ready(response: Response) -> dict[str, Any]:
    """Verify required dependencies before declaring the service ready."""
    settings = get_settings()
    timeout = settings.readiness_timeout_seconds

    async def run(name: str, check) -> tuple[str, dict[str, Any]]:
        started = time.perf_counter()
        try:
            state, detail = await asyncio.wait_for(check(), timeout=timeout)
        except TimeoutError:
            state, detail = "timeout", f"exceeded {timeout}s"
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        result: dict[str, Any] = {"status": state, "latency_ms": elapsed_ms}
        if detail is not None:
            result["detail"] = detail
        return name, result

    results = dict(await asyncio.gather(*(run(n, c) for n, c in _REQUIRED_CHECKS.items())))

    failed = [name for name, result in results.items() if result["status"] != "ok"]
    if failed:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        logger.warning("readiness_failed", extra={"failed_dependencies": failed})

    return {
        "status": "ready" if not failed else "not_ready",
        "service": settings.service_name,
        "checks": results,
    }
