"""Background loop that runs drift detection (Plan 5.7).

An asyncio task on the API process, not a separate worker: the compare is a
pair of LiveKit list calls plus a handful of SELECT/UPDATEs, and the
overview already lives here. Interval + jitter keep two replicas from
hitting the admin API on the same beat (Phase 5 risk table).
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

from shared.logging import get_logger
from shared.telemetry.metrics import ControlPlaneMetrics

from app.core.settings import Settings, get_settings
from app.db.session import get_session_factory
from app.livekit.drift import detect_drift
from app.livekit.sip import SipResourceManager

logger = get_logger(__name__)

Sleep = Callable[[float], Awaitable[None]]


def next_delay_seconds(interval: float, jitter: float, *, rng: random.Random | None = None) -> float:
    """Interval plus a non-negative jitter so checks never bunch up.

    Jitter is ``[0, jitter]`` rather than centred: shrinking the interval
    would let a pair of replicas converge on the same earlier slot.
    """
    if interval < 0:
        raise ValueError("interval must be >= 0")
    if jitter < 0:
        raise ValueError("jitter must be >= 0")
    spread = (rng or random.Random()).uniform(0.0, jitter) if jitter else 0.0
    return interval + spread


async def drift_check_loop(
    metrics: ControlPlaneMetrics,
    settings: Settings | None = None,
    *,
    sleep: Sleep = asyncio.sleep,
    stop: asyncio.Event | None = None,
    manager_factory: Callable[[], SipResourceManager] | None = None,
) -> None:
    """Run until cancelled or ``stop`` is set.

    The first wait is jitter only so a freshly started API detects a deleted
    trunk within the detection interval rather than after a full period.
    """
    settings = settings or get_settings()
    interval = settings.livekit_drift_check_interval_seconds
    jitter = settings.livekit_drift_check_jitter_seconds

    await sleep(next_delay_seconds(0.0, jitter))

    while stop is None or not stop.is_set():
        try:
            factory = get_session_factory()
            async with factory() as session:
                manager = manager_factory() if manager_factory else SipResourceManager()
                await detect_drift(session, manager, metrics=metrics)
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("livekit_drift_check_failed")

        await sleep(next_delay_seconds(interval, jitter))
        if stop is not None and stop.is_set():
            return


def start_drift_checker(metrics: ControlPlaneMetrics) -> asyncio.Task[None] | None:
    """Spawn the loop when the setting is on. Returns None when disabled."""
    settings = get_settings()
    if not settings.livekit_drift_check_enabled:
        logger.info("livekit_drift_check_disabled")
        return None
    logger.info(
        "livekit_drift_check_scheduled",
        extra={
            "interval_seconds": settings.livekit_drift_check_interval_seconds,
            "jitter_seconds": settings.livekit_drift_check_jitter_seconds,
        },
    )
    return asyncio.create_task(drift_check_loop(metrics, settings), name="livekit-drift-check")


async def stop_drift_checker(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return
