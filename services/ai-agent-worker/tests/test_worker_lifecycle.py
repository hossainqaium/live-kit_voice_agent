"""Tests for worker capacity gating and graceful shutdown (spec 51)."""

from __future__ import annotations

import pytest

from worker.health import WorkerState
from worker.settings import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestCapacityGating:
    def test_a_fresh_worker_accepts_calls(self) -> None:
        assert WorkerState().accepting is True

    def test_a_worker_at_capacity_stops_accepting(self) -> None:
        state = WorkerState()
        state.active_calls = get_settings().worker_max_concurrent_calls
        assert state.accepting is False

    def test_capacity_is_configuration_not_a_constant(self) -> None:
        """Spec 49: no fixed number of workers or calls-per-worker is assumed.

        The real figure comes from load testing in Phase 8.
        """
        settings = get_settings()
        assert settings.worker_max_concurrent_calls > 0


class TestGracefulShutdown:
    def test_draining_stops_new_calls_immediately(self) -> None:
        """Readiness must fail before in-flight calls are touched (spec 51)."""
        state = WorkerState()
        state.draining = True
        assert state.accepting is False

    def test_draining_does_not_discard_active_calls(self) -> None:
        """Deployments must not needlessly terminate active calls (spec 51)."""
        state = WorkerState()
        state.active_calls = 3
        state.draining = True
        assert state.accepting is False
        assert state.active_calls == 3

    def test_drain_budget_exceeds_a_realistic_call(self) -> None:
        """A grace period shorter than a call would cut conversations off."""
        assert get_settings().worker_drain_timeout_seconds >= 300


class TestWorkerSettings:
    def test_no_tenant_configuration_is_present(self) -> None:
        """Spec 23: the worker holds no tenant-specific configuration.

        Everything about how a call behaves is loaded per call from the
        Control Plane. If a tenant, agent, prompt or voice field ever appears
        here, the worker has stopped being generic.
        """
        forbidden = ("tenant", "agent_id", "prompt", "voice_id", "greeting", "system_prompt")
        fields = set(get_settings().model_dump().keys())
        for name in fields:
            for token in forbidden:
                assert token not in name, f"tenant-specific setting leaked into the worker: {name}"

    def test_dsn_uses_the_async_driver(self) -> None:
        assert get_settings().sqlalchemy_dsn.startswith("postgresql+asyncpg://")

    def test_secrets_are_not_exposed_by_repr(self) -> None:
        assert "SecretStr" in repr(get_settings().livekit_api_secret)
