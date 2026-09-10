"""AI Agent Worker entrypoint — the Voice Execution Plane (spec 2, 23).

This process is a generic execution engine. It contains no tenant-specific
logic: at call start it loads tenant, agent, agent version, prompt, STT/LLM/TTS
configuration, voice, tools, knowledge base, transfer rules and call policies
from the Control Plane, then executes that configuration.

Phase 0 runs the health/metrics listener and the shutdown machinery. Phase 1
registers the LiveKit Agents job handler on top.
"""

from __future__ import annotations

import asyncio
import signal
from typing import Any

from shared.logging import configure_logging, get_logger
from worker.health import start_health_server, state
from worker.settings import get_settings

settings = get_settings()
configure_logging(service=settings.service_name, level=settings.log_level)
logger = get_logger(__name__)


class Worker:
    """Owns the worker lifecycle, including the drain on SIGTERM (spec 51)."""

    def __init__(self) -> None:
        self._shutdown = asyncio.Event()
        self._runner: Any = None

    def request_shutdown(self, signame: str) -> None:
        """Begin draining. Called from the signal handler.

        Marking the worker as draining first is what makes readiness fail, so
        the orchestrator stops sending new calls here before existing ones are
        touched.
        """
        if state.draining:
            logger.warning("shutdown_already_in_progress", extra={"signal": signame})
            return
        state.draining = True
        logger.info(
            "shutdown_requested",
            extra={
                "signal": signame,
                "active_calls": state.active_calls,
                "drain_timeout_seconds": settings.worker_drain_timeout_seconds,
            },
        )
        self._shutdown.set()

    async def _drain(self) -> None:
        """Wait for active calls to finish, up to the drain budget.

        Deployments must not needlessly terminate active calls (spec 51), so
        the timeout is a backstop for stuck sessions, not the normal path.
        """
        deadline = settings.worker_drain_timeout_seconds
        waited = 0.0
        interval = 1.0

        while state.active_calls > 0 and waited < deadline:
            if waited % 15 < interval:
                logger.info(
                    "draining",
                    extra={
                        "active_calls": state.active_calls,
                        "waited_seconds": round(waited, 1),
                        "deadline_seconds": deadline,
                    },
                )
            await asyncio.sleep(interval)
            waited += interval

        if state.active_calls > 0:
            # Surfacing this loudly matters: it means a deploy did cut calls,
            # which is exactly the outcome spec 51 forbids.
            logger.error(
                "drain_timeout_exceeded",
                extra={"abandoned_calls": state.active_calls, "waited_seconds": round(waited, 1)},
            )
        else:
            logger.info("drain_complete", extra={"waited_seconds": round(waited, 1)})

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for signame in ("SIGTERM", "SIGINT"):
            loop.add_signal_handler(getattr(signal, signame), self.request_shutdown, signame)

        self._runner = await start_health_server()
        logger.info(
            "worker_started",
            extra={
                "environment": settings.environment,
                "livekit_url": settings.livekit_url,
                "agent_name": settings.worker_agent_name,
                "capacity": settings.worker_max_concurrent_calls,
            },
        )

        # Phase 1: register the LiveKit Agents job handler here. The dispatch
        # rules created by the Control Plane target `worker_agent_name`.

        await self._shutdown.wait()
        await self._drain()

        if self._runner is not None:
            await self._runner.cleanup()
        logger.info("worker_stopped")


def main() -> None:
    try:
        asyncio.run(Worker().run())
    except KeyboardInterrupt:
        logger.info("worker_interrupted")


if __name__ == "__main__":
    main()
