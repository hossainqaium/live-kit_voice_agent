"""Provider resilience tests (spec §55 — Plan 4b.10).

Covers all three resilience layers:

  Layer 1 — Per-request timeouts: ``make_resilient_client`` returns an
    ``httpx.AsyncClient`` with the correct stage-specific ``Timeout``.

  Layer 2 — Circuit breaker: ``CircuitBreaker`` state machine, the global
    registry, and ``_CircuitBreakerTransport`` — including instant open-circuit
    rejection and proper success/failure recording.

  Layer 3 — Retry with exponential backoff: ``async_retry`` retries transient
    failures with configurable back-off and gives up on non-retryable errors.

Additionally verifies that each openai-compatible adapter uses
``make_resilient_client`` and that ``_build_session`` passes ``attempt_timeout``
to ``FallbackAdapter``.
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from worker.resilience import (
    ATTEMPT_TIMEOUT,
    CircuitBreaker,
    RetryPolicy,
    _CircuitBreakerTransport,
    _STAGE_TIMEOUTS,
    async_retry,
    get_breaker,
    make_resilient_client,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class _StaticTransport(httpx.AsyncBaseTransport):
    """Returns a fixed sequence of responses / exceptions."""

    def __init__(self, responses: list) -> None:
        self._responses = iter(responses)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        item = next(self._responses)
        if isinstance(item, BaseException):
            raise item
        return item


def _fresh_key() -> str:
    return f"test-provider-{uuid.uuid4()}"


def _open_breaker(failure_threshold: int = 5) -> CircuitBreaker:
    """Return a CircuitBreaker that has been driven into the OPEN state."""
    b = CircuitBreaker(failure_threshold=failure_threshold)
    for _ in range(failure_threshold):
        b.record_failure()
    assert b.state == "open"
    return b


# --------------------------------------------------------------------------- #
# Layer 2 — CircuitBreaker state machine
# --------------------------------------------------------------------------- #


class TestCircuitBreakerStateMachine:
    def test_initial_state_is_closed(self):
        b = CircuitBreaker()
        assert b.state == "closed"
        assert not b.is_open()

    def test_opens_after_reaching_threshold(self):
        b = CircuitBreaker(failure_threshold=3)
        b.record_failure()
        b.record_failure()
        assert b.state == "closed"
        b.record_failure()
        assert b.state == "open"
        assert b.is_open()

    def test_does_not_open_before_threshold(self):
        b = CircuitBreaker(failure_threshold=5)
        for _ in range(4):
            b.record_failure()
        assert b.state == "closed"
        assert not b.is_open()

    def test_success_bleeds_off_failures_in_closed(self):
        b = CircuitBreaker(failure_threshold=3)
        b.record_failure()
        b.record_failure()
        b.record_success()
        # Back to 1 — still needs 2 more failures to open
        b.record_failure()
        assert b.state == "closed"
        b.record_failure()
        assert b.state == "open"

    def test_half_open_transition_after_recovery_timeout(self):
        b = _open_breaker()
        # Backdate the opened_at so the recovery window has elapsed
        b._opened_at = time.monotonic() - b.recovery_timeout - 1
        assert not b.is_open()  # transitions to HALF_OPEN
        assert b.state == "half_open"

    def test_half_open_closes_on_success(self):
        b = _open_breaker()
        b._opened_at = time.monotonic() - b.recovery_timeout - 1
        b.is_open()  # trigger HALF_OPEN
        assert b.state == "half_open"
        b.record_success()
        assert b.state == "closed"

    def test_half_open_reopens_on_failure(self):
        b = _open_breaker()
        b._opened_at = time.monotonic() - b.recovery_timeout - 1
        b.is_open()  # trigger HALF_OPEN
        b.record_failure()
        assert b.state == "open"

    def test_is_open_returns_true_within_recovery_window(self):
        b = _open_breaker()
        assert b.is_open()
        # State must still be open (window not elapsed)
        assert b.state == "open"

    def test_record_failure_resets_count_on_trip(self):
        b = CircuitBreaker(failure_threshold=2)
        b.record_failure()
        b.record_failure()
        assert b.state == "open"
        # After tripping, count resets — further failures don't double-count
        b.record_failure()
        assert b.state == "open"

    def test_success_in_closed_does_not_go_below_zero(self):
        b = CircuitBreaker()
        for _ in range(5):
            b.record_success()
        assert b._failure_count == 0


# --------------------------------------------------------------------------- #
# Layer 2 — Global registry
# --------------------------------------------------------------------------- #


class TestCircuitBreakerRegistry:
    def test_same_key_returns_same_instance(self):
        key = _fresh_key()
        assert get_breaker(key) is get_breaker(key)

    def test_different_keys_return_different_instances(self):
        a, b = get_breaker(_fresh_key()), get_breaker(_fresh_key())
        assert a is not b

    def test_breaker_state_is_preserved_across_calls(self):
        key = _fresh_key()
        b = get_breaker(key)
        b.record_failure()
        b.record_failure()
        assert get_breaker(key)._failure_count == 2


# --------------------------------------------------------------------------- #
# Layer 2 — _CircuitBreakerTransport
# --------------------------------------------------------------------------- #


class TestCircuitBreakerTransport:
    def _request(self) -> httpx.Request:
        return httpx.Request("GET", "https://provider.example/v1/test")

    @pytest.mark.asyncio
    async def test_success_response_records_success(self):
        breaker = CircuitBreaker()
        inner = _StaticTransport([httpx.Response(200)])
        transport = _CircuitBreakerTransport(inner, breaker, "test-provider")

        resp = await transport.handle_async_request(self._request())
        assert resp.status_code == 200
        assert breaker.state == "closed"

    @pytest.mark.asyncio
    async def test_5xx_response_records_failure(self):
        breaker = CircuitBreaker(failure_threshold=1)
        inner = _StaticTransport([httpx.Response(500)])
        transport = _CircuitBreakerTransport(inner, breaker, "test-provider")

        resp = await transport.handle_async_request(self._request())
        assert resp.status_code == 500
        assert breaker.state == "open"

    @pytest.mark.asyncio
    async def test_4xx_does_not_count_as_failure(self):
        breaker = CircuitBreaker(failure_threshold=1)
        inner = _StaticTransport([httpx.Response(401)])
        transport = _CircuitBreakerTransport(inner, breaker, "test-provider")

        await transport.handle_async_request(self._request())
        assert breaker.state == "closed"

    @pytest.mark.asyncio
    async def test_network_error_records_failure(self):
        breaker = CircuitBreaker(failure_threshold=1)
        inner = _StaticTransport([httpx.ConnectError("refused")])
        transport = _CircuitBreakerTransport(inner, breaker, "test-provider")

        with pytest.raises(httpx.ConnectError):
            await transport.handle_async_request(self._request())
        assert breaker.state == "open"

    @pytest.mark.asyncio
    async def test_timeout_error_records_failure(self):
        breaker = CircuitBreaker(failure_threshold=1)
        inner = _StaticTransport([httpx.TimeoutException("timeout")])
        transport = _CircuitBreakerTransport(inner, breaker, "test-provider")

        with pytest.raises(httpx.TimeoutException):
            await transport.handle_async_request(self._request())
        assert breaker.state == "open"

    @pytest.mark.asyncio
    async def test_open_circuit_raises_connect_error_immediately(self):
        breaker = _open_breaker()
        inner = _StaticTransport([])  # would fail if called
        transport = _CircuitBreakerTransport(inner, breaker, "test-provider")

        with pytest.raises(httpx.ConnectError, match="Circuit breaker open"):
            await transport.handle_async_request(self._request())

    @pytest.mark.asyncio
    async def test_open_circuit_does_not_call_inner_transport(self):
        breaker = _open_breaker()
        mock_inner = AsyncMock(spec=httpx.AsyncBaseTransport)
        transport = _CircuitBreakerTransport(mock_inner, breaker, "test-provider")

        with pytest.raises(httpx.ConnectError):
            await transport.handle_async_request(self._request())
        mock_inner.handle_async_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_full_trip_cycle(self):
        """CLOSED → (failures) → OPEN → (timeout) → HALF_OPEN → (success) → CLOSED."""
        threshold = 3
        breaker = CircuitBreaker(failure_threshold=threshold, recovery_timeout=0.1)
        inner_responses = [httpx.Response(500)] * threshold + [httpx.Response(200)]
        inner = _StaticTransport(inner_responses)
        transport = _CircuitBreakerTransport(inner, breaker, "full-cycle")

        for _ in range(threshold):
            await transport.handle_async_request(self._request())
        assert breaker.state == "open"

        await asyncio.sleep(0.15)  # let recovery window elapse

        # Probe (HALF_OPEN)
        inner2 = _StaticTransport([httpx.Response(200)])
        transport2 = _CircuitBreakerTransport(inner2, breaker, "full-cycle")
        await transport2.handle_async_request(self._request())
        assert breaker.state == "closed"


# --------------------------------------------------------------------------- #
# Layer 1 — make_resilient_client / ATTEMPT_TIMEOUT constants
# --------------------------------------------------------------------------- #


class TestMakeResilientClient:
    def test_returns_httpx_async_client(self):
        client = make_resilient_client(provider_key=_fresh_key(), kind="stt")
        assert isinstance(client, httpx.AsyncClient)

    def test_stt_read_timeout(self):
        client = make_resilient_client(provider_key=_fresh_key(), kind="stt")
        assert client.timeout.read == _STAGE_TIMEOUTS["stt"].read

    def test_llm_read_timeout_is_longest(self):
        client = make_resilient_client(provider_key=_fresh_key(), kind="llm")
        assert client.timeout.read == _STAGE_TIMEOUTS["llm"].read
        assert _STAGE_TIMEOUTS["llm"].read > _STAGE_TIMEOUTS["stt"].read

    def test_tts_read_timeout(self):
        client = make_resilient_client(provider_key=_fresh_key(), kind="tts")
        assert client.timeout.read == _STAGE_TIMEOUTS["tts"].read

    def test_same_key_and_kind_share_circuit_breaker(self):
        key = _fresh_key()
        client_a = make_resilient_client(provider_key=key, kind="llm")
        client_b = make_resilient_client(provider_key=key, kind="llm")
        # Both clients route through the same circuit breaker instance
        transport_a = client_a._transport  # type: ignore[attr-defined]
        transport_b = client_b._transport  # type: ignore[attr-defined]
        assert transport_a._breaker is transport_b._breaker

    def test_different_kinds_have_separate_breakers(self):
        key = _fresh_key()
        stt_client = make_resilient_client(provider_key=key, kind="stt")
        llm_client = make_resilient_client(provider_key=key, kind="llm")
        stt_breaker = stt_client._transport._breaker  # type: ignore[attr-defined]
        llm_breaker = llm_client._transport._breaker  # type: ignore[attr-defined]
        assert stt_breaker is not llm_breaker


class TestAttemptTimeoutConstants:
    def test_all_stages_present(self):
        for stage in ("stt", "llm", "tts"):
            assert stage in ATTEMPT_TIMEOUT, f"ATTEMPT_TIMEOUT missing {stage!r}"

    def test_values_are_positive(self):
        for stage, val in ATTEMPT_TIMEOUT.items():
            assert val > 0, f"ATTEMPT_TIMEOUT[{stage!r}] must be positive"

    def test_llm_timeout_is_shortest(self):
        # LLM TTFT matters most; it should not be the longest
        assert ATTEMPT_TIMEOUT["llm"] <= ATTEMPT_TIMEOUT["stt"]


# --------------------------------------------------------------------------- #
# Layer 3 — async_retry
# --------------------------------------------------------------------------- #


class TestAsyncRetry:
    @pytest.mark.asyncio
    async def test_returns_on_first_success(self):
        calls = 0

        async def fn():
            nonlocal calls
            calls += 1
            return "ok"

        result = await async_retry(fn, RetryPolicy(max_attempts=3))
        assert result == "ok"
        assert calls == 1

    @pytest.mark.asyncio
    async def test_does_not_retry_non_retryable(self):
        calls = 0

        async def fn():
            nonlocal calls
            calls += 1
            raise ValueError("fatal")

        policy = RetryPolicy(max_attempts=3, is_retryable=lambda _: False)
        with pytest.raises(ValueError, match="fatal"):
            await async_retry(fn, policy)
        assert calls == 1

    @pytest.mark.asyncio
    async def test_retries_retryable_errors(self):
        calls = 0

        async def fn():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ConnectionError("transient")
            return "recovered"

        policy = RetryPolicy(
            max_attempts=3,
            base_delay=0,
            is_retryable=lambda e: isinstance(e, ConnectionError),
        )
        result = await async_retry(fn, policy)
        assert result == "recovered"
        assert calls == 3

    @pytest.mark.asyncio
    async def test_raises_after_exhausting_attempts(self):
        calls = 0

        async def fn():
            nonlocal calls
            calls += 1
            raise ConnectionError("always fails")

        policy = RetryPolicy(
            max_attempts=3,
            base_delay=0,
            is_retryable=lambda _: True,
        )
        with pytest.raises(ConnectionError, match="always fails"):
            await async_retry(fn, policy)
        assert calls == 3

    @pytest.mark.asyncio
    async def test_exponential_backoff_delays(self):
        """Verify sleep is called with exponentially increasing waits."""
        delays: list[float] = []

        async def fake_sleep(secs: float) -> None:
            delays.append(secs)

        calls = 0

        async def fn():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ConnectionError("transient")
            return "ok"

        policy = RetryPolicy(
            max_attempts=3,
            base_delay=1.0,
            multiplier=2.0,
            max_delay=30.0,
            is_retryable=lambda _: True,
        )
        with patch("worker.resilience.asyncio.sleep", side_effect=fake_sleep):
            await async_retry(fn, policy)

        assert len(delays) == 2
        # Second wait is (base * multiplier) before jitter cap
        assert delays[1] > delays[0]

    @pytest.mark.asyncio
    async def test_delay_is_capped_at_max(self):
        delays: list[float] = []

        async def fake_sleep(secs: float) -> None:
            delays.append(secs)

        calls = 0

        async def fn():
            nonlocal calls
            calls += 1
            if calls < 5:
                raise ConnectionError()
            return "ok"

        policy = RetryPolicy(
            max_attempts=5,
            base_delay=10.0,
            multiplier=10.0,
            max_delay=15.0,
            is_retryable=lambda _: True,
        )
        with patch("worker.resilience.asyncio.sleep", side_effect=fake_sleep):
            await async_retry(fn, policy)

        for d in delays:
            assert d <= 15.0


# --------------------------------------------------------------------------- #
# Adapter source inspection — resilient client wired in
# --------------------------------------------------------------------------- #


class TestAdaptersUseResilientClient:
    """Verify each openai-compatible adapter's build_livekit_component uses
    make_resilient_client.  Source inspection avoids the need to import the
    full LiveKit stack.
    """

    def test_stt_adapter(self):
        from worker.providers.stt.openai_compatible import OpenAICompatibleSTT

        src = inspect.getsource(OpenAICompatibleSTT.build_livekit_component)
        assert "make_resilient_client" in src
        assert '"stt"' in src or "'stt'" in src
        assert "http_client" in src

    def test_llm_adapter(self):
        from worker.providers.llm.openai_compatible import OpenAICompatibleLLM

        src = inspect.getsource(OpenAICompatibleLLM.build_livekit_component)
        assert "make_resilient_client" in src
        assert '"llm"' in src or "'llm'" in src
        assert "http_client" in src

    def test_tts_adapter(self):
        from worker.providers.tts.openai_compatible import OpenAICompatibleTTS

        src = inspect.getsource(OpenAICompatibleTTS.build_livekit_component)
        assert "make_resilient_client" in src
        assert '"tts"' in src or "'tts'" in src
        assert "http_client" in src

    def test_max_retries_zero_in_openai_client(self):
        """All adapters disable the openai library's built-in retry so our
        circuit breaker and FallbackAdapter control all retry decisions."""
        from worker.providers.llm.openai_compatible import OpenAICompatibleLLM
        from worker.providers.stt.openai_compatible import OpenAICompatibleSTT
        from worker.providers.tts.openai_compatible import OpenAICompatibleTTS

        for cls in (OpenAICompatibleSTT, OpenAICompatibleLLM, OpenAICompatibleTTS):
            src = inspect.getsource(cls.build_livekit_component)
            assert "max_retries=0" in src, (
                f"{cls.__name__}.build_livekit_component must set max_retries=0"
            )


# --------------------------------------------------------------------------- #
# _build_session source inspection — attempt_timeout wired in
# --------------------------------------------------------------------------- #


class TestBuildSessionAttemptTimeout:
    """Verify _build_session passes attempt_timeout to FallbackAdapter."""

    def test_attempt_timeout_in_build_session(self):
        import worker.entrypoint as ep

        src = inspect.getsource(ep._build_session)
        assert "attempt_timeout" in src
        assert "ATTEMPT_TIMEOUT" in src

    def test_all_three_stages_covered(self):
        import worker.entrypoint as ep

        src = inspect.getsource(ep._build_session)
        assert 'ATTEMPT_TIMEOUT["stt"]' in src or "ATTEMPT_TIMEOUT['stt']" in src
        assert 'ATTEMPT_TIMEOUT["llm"]' in src or "ATTEMPT_TIMEOUT['llm']" in src
        assert 'ATTEMPT_TIMEOUT["tts"]' in src or "ATTEMPT_TIMEOUT['tts']" in src
