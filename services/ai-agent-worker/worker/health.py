"""Health, readiness and metrics endpoint for the worker (spec 59).

The worker is not an HTTP service, but Kubernetes needs somewhere to probe and
Prometheus needs somewhere to scrape, so a small aiohttp listener runs
alongside the agent job loop. It carries no audio.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as aioredis
from aiohttp import web
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from shared.logging import get_logger
from shared.telemetry import CONTENT_TYPE_LATEST, render_metrics
from worker.settings import get_settings

logger = get_logger(__name__)

Check = Callable[[], Awaitable[tuple[str, str | None]]]


class WorkerState:
    """Tracks whether this worker is accepting new calls.

    Set to draining on SIGTERM so readiness fails and the orchestrator stops
    routing new work here while existing calls finish (spec 51).
    """

    def __init__(self) -> None:
        self.draining = False
        self.active_calls = 0
        self.started_at = time.time()

    @property
    def accepting(self) -> bool:
        settings = get_settings()
        return not self.draining and self.active_calls < settings.worker_max_concurrent_calls


state = WorkerState()


async def _check_postgres() -> tuple[str, str | None]:
    settings = get_settings()
    # A short-lived engine keeps the probe independent of the pool the call
    # path uses, so a saturated pool does not read as a dead dependency.
    engine = create_async_engine(settings.sqlalchemy_dsn, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return "ok", None
    except Exception as exc:
        return "error", str(exc)
    finally:
        await engine.dispose()


async def _check_redis() -> tuple[str, str | None]:
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


_REQUIRED_CHECKS: dict[str, Check] = {
    "postgresql": _check_postgres,
    "redis": _check_redis,
}


async def handle_health(request: web.Request) -> web.Response:
    """Liveness: the process is running. No dependencies consulted."""
    settings = get_settings()
    return web.json_response(
        {
            "status": "ok",
            "service": settings.service_name,
            "environment": settings.environment,
            "uptime_seconds": round(time.time() - state.started_at, 1),
        }
    )


async def handle_ready(request: web.Request) -> web.Response:
    """Readiness: dependencies verified and this worker can take a new call."""
    settings = get_settings()
    timeout = settings.readiness_timeout_seconds

    async def run(name: str, check: Check) -> tuple[str, dict[str, Any]]:
        started = time.perf_counter()
        try:
            status, detail = await asyncio.wait_for(check(), timeout=timeout)
        except TimeoutError:
            status, detail = "timeout", f"exceeded {timeout}s"
        result: dict[str, Any] = {
            "status": status,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }
        if detail is not None:
            result["detail"] = detail
        return name, result

    checks = dict(await asyncio.gather(*(run(n, c) for n, c in _REQUIRED_CHECKS.items())))
    failed = [name for name, result in checks.items() if result["status"] != "ok"]

    ready = not failed and not state.draining
    if failed:
        logger.warning("readiness_failed", extra={"failed_dependencies": failed})

    return web.json_response(
        {
            "status": "ready" if ready else "not_ready",
            "service": settings.service_name,
            "draining": state.draining,
            "active_calls": state.active_calls,
            "capacity": settings.worker_max_concurrent_calls,
            "accepting_calls": state.accepting,
            "checks": checks,
        },
        status=200 if ready else 503,
    )


async def handle_metrics(request: web.Request) -> web.Response:
    return web.Response(body=render_metrics(), content_type=CONTENT_TYPE_LATEST.split(";")[0])


def build_app() -> web.Application:
    app = web.Application()
    app.add_routes(
        [
            web.get("/health", handle_health),
            web.get("/ready", handle_ready),
            web.get("/metrics", handle_metrics),
        ]
    )
    return app


async def start_health_server() -> web.AppRunner:
    """Start the listener and return its runner so shutdown can clean up."""
    settings = get_settings()
    runner = web.AppRunner(build_app(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, settings.health_host, settings.health_port)
    await site.start()
    logger.info(
        "health_server_started",
        extra={"host": settings.health_host, "port": settings.health_port},
    )
    return runner
