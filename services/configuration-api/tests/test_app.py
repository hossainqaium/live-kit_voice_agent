"""Tests for application wiring: correlation, metrics, OpenAPI, CORS."""

from __future__ import annotations

import pytest


class TestCorrelation:
    @pytest.mark.asyncio
    async def test_response_carries_a_request_id(self, client) -> None:
        response = await client.get("/health")
        assert response.headers.get("X-Request-ID")

    @pytest.mark.asyncio
    async def test_inbound_request_id_is_preserved(self, client) -> None:
        """A trace started by the frontend must survive into the API's logs."""
        response = await client.get("/health", headers={"X-Request-ID": "trace-abc-123"})
        assert response.headers["X-Request-ID"] == "trace-abc-123"

    @pytest.mark.asyncio
    async def test_request_ids_are_unique_per_request(self, client) -> None:
        first = (await client.get("/health")).headers["X-Request-ID"]
        second = (await client.get("/health")).headers["X-Request-ID"]
        assert first != second


class TestMetrics:
    @pytest.mark.asyncio
    async def test_metrics_endpoint_serves_prometheus_format(self, client) -> None:
        response = await client.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]

    @pytest.mark.asyncio
    async def test_requests_are_counted(self, client) -> None:
        await client.get("/health")
        body = (await client.get("/metrics")).text
        assert "api_http_requests_total" in body

    @pytest.mark.asyncio
    async def test_path_parameters_do_not_inflate_label_cardinality(self, client) -> None:
        """Metrics must label by route template, not by concrete path.

        Labelling by path would create one time series per call ID, which is
        how a Prometheus instance gets taken down by its own metrics.
        """
        await client.get("/api/v1/does-not-exist-12345")
        body = (await client.get("/metrics")).text
        assert "does-not-exist-12345" not in body


class TestOpenAPI:
    @pytest.mark.asyncio
    async def test_openapi_schema_is_generated(self, client) -> None:
        """Spec 67 requires complete OpenAPI documentation."""
        response = await client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        assert schema["info"]["title"].startswith("AI Voice Agent Platform")

    @pytest.mark.asyncio
    async def test_docs_are_served(self, client) -> None:
        assert (await client.get("/docs")).status_code == 200

    @pytest.mark.asyncio
    async def test_probes_are_excluded_from_the_versioned_surface(self, client) -> None:
        """Orchestrators should not need to know the API version to probe."""
        paths = (await client.get("/openapi.json")).json()["paths"]
        assert "/health" in paths
        assert "/ready" in paths
        assert not any(p.startswith("/api/v1/health") for p in paths)


class TestSettings:
    def test_dsn_uses_the_async_driver(self) -> None:
        from app.core.settings import Settings

        settings = Settings(postgres_host="db", postgres_db="x", postgres_user="u")
        assert settings.sqlalchemy_dsn.startswith("postgresql+asyncpg://")

    def test_explicit_database_url_is_upgraded_to_asyncpg(self) -> None:
        from app.core.settings import Settings

        settings = Settings(database_url="postgresql://u:p@h:5432/db")
        assert settings.sqlalchemy_dsn.startswith("postgresql+asyncpg://")

    def test_secrets_are_not_exposed_by_repr(self) -> None:
        """A settings object landing in a traceback must not leak credentials."""
        from app.core.settings import Settings

        settings = Settings(postgres_password="hunter2", jwt_secret="topsecret")
        rendered = repr(settings)
        assert "hunter2" not in rendered
        assert "topsecret" not in rendered

    def test_cors_origins_accept_a_comma_separated_string(self) -> None:
        from app.core.settings import Settings

        settings = Settings(cors_allow_origins="http://a.test, http://b.test")
        assert settings.cors_allow_origins == ["http://a.test", "http://b.test"]
