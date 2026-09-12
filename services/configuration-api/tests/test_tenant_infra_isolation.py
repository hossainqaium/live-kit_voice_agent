"""Tenant administrators cannot see or edit infrastructure (spec 13, Plan 5.8)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.admin import TenantSettingsResponse, TenantSettingsUpdate
from tests.conftest import frontend_file

_TENANT_SETTINGS_FIELDS = {
    "id",
    "name",
    "slug",
    "status",
    "timezone",
    "default_language",
    "max_concurrent_calls",
    "max_daily_calls",
    "max_monthly_minutes",
    "notes",
}

_INFRA_FIELD_NAMES = {
    "redis_url",
    "redis",
    "node_ip",
    "external_ip",
    "rtp_port",
    "rtc_port",
    "kubernetes",
    "load_balancer",
    "firewall",
    "tls_cert",
    "livekit_api_secret",
}

_INFRA_SOURCE_TOKENS = (
    "REDIS_URL",
    "NODE_IP",
    "LIVEKIT_API_SECRET",
    "rtp_port",
    "kubernetes",
    "load_balancer",
    "K8s",
)


class TestTenantSettingsSchema:
    def test_response_is_exactly_the_tenant_fields(self) -> None:
        assert set(TenantSettingsResponse.model_fields) == _TENANT_SETTINGS_FIELDS

    def test_update_refuses_infra_and_limits(self) -> None:
        for field in _INFRA_FIELD_NAMES | {"max_concurrent_calls", "max_daily_calls"}:
            with pytest.raises(ValidationError):
                TenantSettingsUpdate.model_validate({field: "x"})

    def test_update_accepts_only_identity_fields(self) -> None:
        assert set(TenantSettingsUpdate.model_fields) == {
            "name",
            "timezone",
            "default_language",
            "notes",
        }


class TestTenantOpenApi:
    async def test_settings_schema_has_no_infra_properties(self, client) -> None:
        spec = (await client.get("/openapi.json")).json()
        schemas = spec["components"]["schemas"]
        for name in ("TenantSettingsResponse", "TenantSettingsUpdate"):
            props = set(schemas[name].get("properties", {}))
            assert not (props & _INFRA_FIELD_NAMES), name

    async def test_tenant_routes_do_not_document_cluster_settings(self, client) -> None:
        spec = (await client.get("/openapi.json")).json()
        tenant_paths = [
            path
            for path in spec["paths"]
            if not path.startswith("/api/v1/platform")
        ]
        blob = " ".join(tenant_paths).lower()
        assert "redis" not in blob
        assert "kubernetes" not in blob
        assert "rtp" not in blob


class TestTenantFrontend:
    def test_settings_page_says_infra_is_platform_controlled(self) -> None:
        path = frontend_file("app", "settings", "page.tsx")
        if path is None:
            pytest.skip("services/frontend is not mounted in this container")
        src = path.read_text()
        assert "platform-controlled" in src
        assert "not visible" in src

    def test_tenant_pages_do_not_configure_infra(self) -> None:
        root = frontend_file("app", "settings", "page.tsx")
        if root is None:
            pytest.skip("services/frontend is not mounted in this container")
        app = root.parents[1]
        leaks: list[str] = []
        for path in app.rglob("*.tsx"):
            if "platform" in path.parts or "settings" in path.parts:
                continue
            text = path.read_text()
            for token in _INFRA_SOURCE_TOKENS:
                if token in text:
                    leaks.append(f"{path}:{token}")
        assert not leaks, f"tenant pages leaked infra tokens: {leaks}"
