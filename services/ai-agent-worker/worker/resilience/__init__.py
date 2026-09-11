"""Timeout, retry, exponential backoff, circuit breaker for provider calls (spec §55).

Architecture
------------
Three layers of defence against slow or unreliable AI providers:

**Layer 1 — Per-request timeout**
    Every httpx call gets a structured :class:`httpx.Timeout` so a provider
    that hangs never stalls a turn forever.  Values are generous enough for
    streaming responses yet tight enough to trip before LiveKit's own 30-s
    turn deadline.

**Layer 2 — Circuit breaker**
    A per-(provider-URL × stage) state machine.  After ``failure_threshold``
    consecutive transport-level failures the breaker *opens*, and the next
    request from that provider gets an immediate ``ConnectError`` instead of
    another slow failure.  After ``recovery_timeout`` seconds the breaker
    enters HALF_OPEN and lets one probe through; success closes it.

    Because the breaker lives in the httpx transport layer, LiveKit's
    ``FallbackAdapter`` sees the ``ConnectError`` and switches to the next
    tier without waiting for ``attempt_timeout``.  This makes open-circuit
    failover *instant* rather than latency-triggered.

**Layer 3 — Retry with exponential backoff**
    :func:`async_retry` retries a coroutine on retryable errors (rate-limit,
    transient 5xx) with jittered exponential back-off before re-raising.
    It is intentionally *not* wired into the streaming voice path — retrying
    mid-turn is worse than failing over; use it for health probes and setup.

Usage in a provider adapter
---------------------------
::

    from worker.resilience import make_resilient_client
    import openai as _openai

    def build_livekit_component(self) -> Any:
        http = make_resilient_client(
            provider_key=self.config.base_url or self.config.provider,
            kind="stt",   # "stt" | "llm" | "tts"
        )
        client = _openai.AsyncOpenAI(
            api_key=self.config.api_key or "not-required",
            base_url=self.config.base_url,
            http_client=http,
            max_retries=0,
        )
        return lk_openai.STT(client=client, model=..., language=...)

Usage in ``_build_session`` (FallbackAdapter latency trip-wire)
---------------------------------------------------------------
::

    from worker.resilience import ATTEMPT_TIMEOUT

    stt = stt_api.FallbackAdapter(
        stt_chain, attempt_timeout=ATTEMPT_TIMEOUT["stt"]
    )
"""

from __future__ import annotations

import asyncio
import enum
import logging
import threading
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-stage timeouts
# ---------------------------------------------------------------------------

#: httpx.Timeout per pipeline stage.
#:
#: connect / write: 5 s is already generous for DNS + TLS handshake.
#: read: stage-specific because LLM streaming responses can be long, while
#:   STT and TTS have bounded latency profiles.
_STAGE_TIMEOUTS: dict[str, httpx.Timeout] = {
    "stt": httpx.Timeout(connect=5.0, write=10.0, read=30.0, pool=5.0),
    "llm": httpx.Timeout(connect=5.0, write=10.0, read=120.0, pool=5.0),
    "tts": httpx.Timeout(connect=5.0, write=10.0, read=30.0, pool=5.0),
}
_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, write=10.0, read=60.0, pool=5.0)

#: Seconds before ``FallbackAdapter`` abandons a slow provider tier and
#: switches to the next one.  This is a wall-clock budget for the full
#: provider turn, distinct from the per-request httpx timeout above.
ATTEMPT_TIMEOUT: dict[str, float] = {
    "stt": 12.0,
    "llm": 10.0,
    "tts": 10.0,
}


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class _CBState(enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Thread-safe per-provider circuit breaker.

    State machine::

        CLOSED ──(failures ≥ threshold)──▶ OPEN
          ▲                                  │
          │   (probe succeeds)   (recovery_timeout elapsed)
          │                                  ▼
          └────────────────────── HALF_OPEN

    :meth:`record_success` and :meth:`record_failure` are called by the
    httpx transport on every request.  :meth:`is_open` is checked *before*
    sending a request; it also performs the OPEN → HALF_OPEN transition.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        success_threshold: int = 1,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.success_threshold = success_threshold

        self._state = _CBState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    def is_open(self) -> bool:
        """Return ``True`` if this provider should be skipped.

        As a side-effect, transitions OPEN → HALF_OPEN when the recovery
        window has elapsed, allowing one probe through.
        """
        with self._lock:
            if self._state is _CBState.OPEN:
                elapsed = time.monotonic() - (self._opened_at or 0.0)
                if elapsed >= self.recovery_timeout:
                    self._state = _CBState.HALF_OPEN
                    self._success_count = 0
                    logger.info("circuit_breaker_half_open")
                    return False  # let the probe through
                return True
            return False

    def record_success(self) -> None:
        """Decrement failure counter; close the breaker when probing."""
        with self._lock:
            if self._state is _CBState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    self._state = _CBState.CLOSED
                    self._failure_count = 0
                    logger.info("circuit_breaker_closed")
            elif self._state is _CBState.CLOSED:
                # Bleed off historical failures on each success so a provider
                # with occasional errors never silently drifts to tripping.
                self._failure_count = max(0, self._failure_count - 1)

    def record_failure(self) -> None:
        """Increment failure counter; open the breaker if threshold is hit."""
        with self._lock:
            self._failure_count += 1
            trip = (
                self._state is _CBState.HALF_OPEN
                or self._failure_count >= self.failure_threshold
            )
            if trip and self._state is not _CBState.OPEN:
                logger.warning(
                    "circuit_breaker_opened",
                    extra={"failure_count": self._failure_count},
                )
                self._state = _CBState.OPEN
                self._opened_at = time.monotonic()
                self._failure_count = 0

    @property
    def state(self) -> str:
        """Current state label (for observability)."""
        return self._state.value


# ---------------------------------------------------------------------------
# Global registry
# ---------------------------------------------------------------------------

_registry: dict[str, CircuitBreaker] = {}
_registry_lock = threading.Lock()


def get_breaker(key: str) -> CircuitBreaker:
    """Return the singleton :class:`CircuitBreaker` for *key*, creating if absent."""
    with _registry_lock:
        if key not in _registry:
            _registry[key] = CircuitBreaker()
        return _registry[key]


# ---------------------------------------------------------------------------
# httpx transport with circuit-breaker gate
# ---------------------------------------------------------------------------


class _CircuitBreakerTransport(httpx.AsyncBaseTransport):
    """httpx transport wrapper that gates every request through a CircuitBreaker.

    Raises :exc:`httpx.ConnectError` immediately when the breaker is open,
    giving LiveKit's ``FallbackAdapter`` an instant signal to switch tiers
    rather than waiting for a full request timeout.
    """

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        breaker: CircuitBreaker,
        provider_key: str,
    ) -> None:
        self._inner = inner
        self._breaker = breaker
        self._provider_key = provider_key

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._breaker.is_open():
            logger.warning(
                "circuit_open_blocking_request",
                extra={"provider": self._provider_key},
            )
            raise httpx.ConnectError(
                f"Circuit breaker open for {self._provider_key!r}",
                request=request,
            )
        try:
            response = await self._inner.handle_async_request(request)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError):
            self._breaker.record_failure()
            raise

        # 5xx → provider-side fault → counts toward tripping.
        # 4xx → client error (bad creds, quota exceeded) → do not count;
        #   the circuit breaker is for infrastructure faults, not
        #   misconfiguration that a retry cannot fix.
        if response.status_code >= 500:
            self._breaker.record_failure()
        else:
            self._breaker.record_success()
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------


def make_resilient_client(*, provider_key: str, kind: str) -> httpx.AsyncClient:
    """Return an :class:`httpx.AsyncClient` wired with timeout + circuit breaker.

    Pass the result as ``http_client`` to :class:`openai.AsyncOpenAI` so that
    every LiveKit openai-plugin request benefits from both protections.

    Args:
        provider_key: Stable identifier for this provider endpoint, typically
            its ``base_url`` or provider slug.  Used as the circuit-breaker
            registry key (combined with *kind* to allow per-stage state).
        kind: Pipeline stage — ``"stt"``, ``"llm"``, or ``"tts"``.
    """
    timeout = _STAGE_TIMEOUTS.get(kind, _DEFAULT_TIMEOUT)
    breaker = get_breaker(f"{provider_key}:{kind}")
    inner = httpx.AsyncHTTPTransport(
        limits=httpx.Limits(max_keepalive_connections=5, max_connections=10)
    )
    transport = _CircuitBreakerTransport(inner, breaker, provider_key)
    return httpx.AsyncClient(transport=transport, timeout=timeout)


# ---------------------------------------------------------------------------
# Retry with exponential backoff
# ---------------------------------------------------------------------------


@dataclass
class RetryPolicy:
    """Parameters for :func:`async_retry`.

    Attributes:
        max_attempts:  Total tries (first attempt + retries).
        base_delay:    Seconds to wait after the first failure.
        max_delay:     Cap on the inter-attempt delay.
        multiplier:    Exponential growth factor per attempt.
        is_retryable:  Predicate returning ``True`` when the error is transient
                       and a retry might succeed.
    """

    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    multiplier: float = 2.0
    is_retryable: Callable[[BaseException], bool] = field(
        default_factory=lambda: (lambda _: False)
    )


async def async_retry(
    fn: Callable[[], Coroutine[Any, Any, Any]],
    policy: RetryPolicy,
) -> Any:
    """Retry *fn* with exponential back-off according to *policy*.

    Intended for non-realtime callers (health probes, setup steps).  Do
    **not** use inside the streaming voice pipeline — retrying mid-turn
    introduces silence the caller will notice, and failing over to the next
    tier is always preferable.

    Args:
        fn:     Zero-argument async callable to invoke and retry.
        policy: Retry configuration.

    Returns:
        The first successful return value of *fn*.

    Raises:
        The last exception raised by *fn* after all attempts are exhausted.
    """
    delay = policy.base_delay
    last_exc: BaseException | None = None

    for attempt in range(policy.max_attempts):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if not policy.is_retryable(exc) or attempt == policy.max_attempts - 1:
                raise
            # 10 % jitter keeps simultaneous retries from thundering together.
            jitter = delay * 0.1
            wait = min(delay + jitter, policy.max_delay)
            logger.warning(
                "provider_retry_backoff",
                extra={
                    "attempt": attempt + 1,
                    "max_attempts": policy.max_attempts,
                    "wait_seconds": round(wait, 2),
                    "error": str(exc),
                },
            )
            await asyncio.sleep(wait)
            delay = min(delay * policy.multiplier, policy.max_delay)

    raise last_exc  # type: ignore[misc]
