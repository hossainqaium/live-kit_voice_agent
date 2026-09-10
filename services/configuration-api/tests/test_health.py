"""Tests for the liveness and readiness probes (spec 59)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.api import health
from app.core.settings import get_settings


class TestLiveness:
    @pytest.mark.asyncio
    async def test_health_returns_ok(self, client) -> None:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    @pytest.mark.asyncio
    async def test_health_does_not_touch_dependencies(self, client) -> None:
        """Liveness must not depend on PostgreSQL or Redis.

        If it did, a brief database blip would get every container killed and
        turn a partial outage into a full one.
        """
        with (
            patch("app.api.health._check_postgres", new=AsyncMock()) as pg,
            patch("app.api.health._check_redis", new=AsyncMock()) as redis,
        ):
            response = await client.get("/health")

        assert response.status_code == 200
        pg.assert_not_called()
        redis.assert_not_called()

    @pytest.mark.asyncio
    async def test_health_reports_service_identity(self, client) -> None:
        body = (await client.get("/health")).json()
        assert body["service"] == "configuration-api"
        assert "environment" in body


class TestReadiness:
    """Readiness must verify required dependencies (spec 59).

    ``_REQUIRED_CHECKS`` holds direct function references captured at import
    time, so these tests patch the registry itself rather than the module
    attributes — patching the latter would leave the real checks in place and
    the tests would pass against a live database instead of the intended stub.
    """

    @staticmethod
    def _ok() -> AsyncMock:
        return AsyncMock(return_value=("ok", None))

    @staticmethod
    def _failing(detail: str) -> AsyncMock:
        return AsyncMock(return_value=("error", detail))

    @pytest.mark.asyncio
    async def test_ready_when_all_dependencies_pass(self, client) -> None:
        checks = {"postgresql": self._ok(), "redis": self._ok()}
        with patch.dict(health._REQUIRED_CHECKS, checks, clear=True):
            response = await client.get("/ready")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert body["checks"]["postgresql"]["status"] == "ok"
        assert body["checks"]["redis"]["status"] == "ok"
        # Prove the stubs were actually consulted, so this cannot pass by
        # accidentally reaching the real database.
        for check in checks.values():
            check.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_503_when_postgres_is_down(self, client) -> None:
        checks = {
            "postgresql": self._failing("connection refused"),
            "redis": self._ok(),
        }
        with patch.dict(health._REQUIRED_CHECKS, checks, clear=True):
            response = await client.get("/ready")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        assert body["checks"]["postgresql"]["detail"] == "connection refused"

    @pytest.mark.asyncio
    async def test_returns_503_when_redis_is_down(self, client) -> None:
        checks = {"postgresql": self._ok(), "redis": self._failing("timeout")}
        with patch.dict(health._REQUIRED_CHECKS, checks, clear=True):
            response = await client.get("/ready")

        assert response.status_code == 503
        assert response.json()["checks"]["redis"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_one_failure_is_enough_to_be_not_ready(self, client) -> None:
        """Serving traffic without every required dependency is worse than
        being taken out of rotation."""
        checks = {"postgresql": self._failing("down"), "redis": self._failing("down")}
        with patch.dict(health._REQUIRED_CHECKS, checks, clear=True):
            response = await client.get("/ready")
        assert response.status_code == 503

    @pytest.mark.asyncio
    async def test_slow_dependency_is_reported_as_timeout(self, client) -> None:
        """A hanging dependency must not hang the probe.

        A readiness check that outlasts the probe timeout is indistinguishable
        from an outage, and it holds the connection open while the orchestrator
        retries.
        """

        async def never_returns() -> tuple[str, str | None]:
            await asyncio.sleep(60)
            return "ok", None

        fast_timeout = get_settings().model_copy(update={"readiness_timeout_seconds": 0.05})

        with (
            patch.dict(
                health._REQUIRED_CHECKS,
                {"postgresql": never_returns, "redis": self._ok()},
                clear=True,
            ),
            patch.object(health, "get_settings", return_value=fast_timeout),
        ):
            response = await client.get("/ready")

        assert response.status_code == 503
        assert response.json()["checks"]["postgresql"]["status"] == "timeout"

    @pytest.mark.asyncio
    async def test_reports_latency_per_check(self, client) -> None:
        with patch.dict(
            health._REQUIRED_CHECKS,
            {"postgresql": self._ok(), "redis": self._ok()},
            clear=True,
        ):
            body = (await client.get("/ready")).json()

        for check in body["checks"].values():
            assert isinstance(check["latency_ms"], int | float)

    @pytest.mark.asyncio
    async def test_livekit_is_not_a_readiness_dependency(self, client) -> None:
        """The API still serves configuration when LiveKit is unreachable.

        Making LiveKit a readiness dependency would take the whole Control
        Plane out of rotation during a media-layer incident, exactly when
        operators need it to diagnose and repair. LiveKit health belongs on
        the capacity dashboard instead (spec 48).
        """
        assert set(health._REQUIRED_CHECKS) == {"postgresql", "redis"}
