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

        await self._shutdown.wait()
        await self._drain()

        if self._runner is not None:
            await self._runner.cleanup()
        logger.info("worker_stopped")


async def _serve_health_only() -> None:
    """Run just the health listener, for the LiveKit agents worker process.

    The LiveKit Agents runtime owns the event loop and its own signal
    handling, so the health endpoint runs beside it rather than inside our own
    lifecycle manager. Kubernetes still needs somewhere to probe and Prometheus
    somewhere to scrape.
    """
    runner = await start_health_server()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def _start_health_thread() -> None:
    """Start the health listener on its own loop in a background thread."""
    import threading

    def run() -> None:
        asyncio.run(_serve_health_only())

    thread = threading.Thread(target=run, name="health-server", daemon=True)
    thread.start()


def main() -> None:
    """Run the LiveKit Agents worker.

    Delegates the process lifecycle to the agents runtime, which handles job
    dispatch, prewarming and its own draining. Our health endpoint runs
    alongside it; the Worker class above remains the lifecycle owner for the
    health-only mode used by tests and by `python -m worker.main --health-only`.
    """
    import sys

    from worker.entrypoint import worker_options

    if "--health-only" in sys.argv:
        logger.info("starting_in_health_only_mode")
        try:
            asyncio.run(Worker().run())
        except KeyboardInterrupt:
            logger.info("worker_interrupted")
        return

    _start_health_thread()

    logger.info(
        "registering_with_livekit",
        extra={
            "agent_name": settings.worker_agent_name,
            "livekit_url": settings.livekit_url,
            "capacity": settings.worker_max_concurrent_calls,
        },
    )

    # The agents CLI expects a subcommand. Defaulting to "start" keeps the
    # container command a plain `python -m worker.main` while still allowing
    # `download-files` or `dev` to be passed through explicitly.
    known_commands = {"start", "dev", "console", "connect", "download-files"}
    if not any(arg in known_commands for arg in sys.argv[1:]):
        sys.argv.insert(1, "start")

    _use_our_log_format()

    # cli.run_app installs its own SIGTERM handling and drains in-flight jobs
    # before exiting (spec 51), so it is given the process rather than wrapped.
    from livekit.agents import cli

    cli.run_app(worker_options())


def _use_our_log_format() -> None:
    """Make the agents runtime log through our formatter.

    The runtime installs its own root handler when it starts, and again in
    every job subprocess. With ours already there, each record was emitted
    twice — once in our format, once in theirs — doubling log volume and
    producing two records per event for any collector.

    Replacing the setup function is the available seam. It has to be patched on
    the module that *calls* it, not the one that defines it: cli.py does
    `from .log import setup_logging`, so it holds a direct reference and
    patching `cli.log.setup_logging` would have no effect.

    Their setup runs first, so the runtime's own level and noisy-logger choices
    are preserved; we then reassert a single root handler carrying the fields
    spec 58 requires. Job subprocesses forward records to the main process
    rather than configuring logging themselves, so fixing it here covers them.
    """
    from livekit.agents.cli import cli as cli_module

    original = cli_module.setup_logging

    def patched(log_level: str, devmode: bool, console: bool, compact: bool = False) -> None:
        original(log_level, devmode, console, compact)
        # Our own LOG_LEVEL wins. Passing LiveKit's CLI level through here
        # meant LOG_LEVEL=debug was silently ignored in worker mode, so the
        # debug diagnostics this service emits could never be turned on.
        configure_logging(service=settings.service_name, level=settings.log_level)

    cli_module.setup_logging = patched


if __name__ == "__main__":
    main()
