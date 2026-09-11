"""Request-level enforcement: the tenant-ID guard and RBAC dependencies.

Spec 7 (never trust a browser-supplied tenant ID), 8 (RBAC enforced at the API
layer, independently of the frontend).

These drive the dependencies directly with fake requests, because what matters
is the decision each one makes — which requests are refused and with what
status — not the routing that reaches it.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi import HTTPException

from app.core.dependencies import (
    get_client_ip,
    get_tenant_scope,
    reject_client_tenant_id,
    require_permission,
    require_platform_user,
    require_super_admin,
)
from app.core.security import Principal
from shared.models import Permission, PlatformRole, TenantRole

TENANT_A = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


class FakeURL:
    def __init__(self, path: str = "/api/v1/agents") -> None:
        self.path = path


class FakeClient:
    def __init__(self, host: str = "10.0.0.5") -> None:
        self.host = host


class FakeRequest:
    """Minimal stand-in for the parts of Request the guard reads."""

    def __init__(
        self,
        *,
        method: str = "GET",
        query: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        body: object | None = None,
    ) -> None:
        self.method = method
        self.query_params = query or {}
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        if body is not None and "content-type" not in self.headers:
            self.headers["content-type"] = "application/json"
        self._body = json.dumps(body).encode() if body is not None else b""
        self.url = FakeURL()
        self.client = FakeClient()
        self.state = type("S", (), {})()

    async def body(self) -> bytes:
        return self._body

    # Starlette's headers are case-insensitive; mimic .get only.
    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


def _patch_headers(request: FakeRequest) -> FakeRequest:
    """Give the fake a Starlette-like case-insensitive ``headers.get``."""

    class Headers(dict):
        def get(self, key: str, default: object = None) -> object:  # type: ignore[override]
            return dict.get(self, key.lower(), default)

    request.headers = Headers(request.headers)  # type: ignore[assignment]
    return request


def tenant_principal(
    *,
    roles: tuple[str, ...] = (TenantRole.TENANT_ADMIN.value,),
    permissions: tuple[str, ...] = (),
    tenant_id: uuid.UUID | None = TENANT_A,
    is_platform: bool = False,
) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        email="user@dev.example.com",
        tenant_id=tenant_id,
        roles=frozenset(roles),
        permissions=frozenset(permissions),
        is_platform_user=is_platform,
    )


# --------------------------------------------------------------------------- #
# Spec 7: the client may not supply a tenant identity
# --------------------------------------------------------------------------- #


class TestTenantIdGuard:
    @pytest.mark.asyncio
    async def test_a_clean_request_passes(self) -> None:
        await reject_client_tenant_id(_patch_headers(FakeRequest()))  # must not raise

    @pytest.mark.parametrize("field", ["tenant_id", "tenantId", "tenant"])
    @pytest.mark.asyncio
    async def test_a_tenant_field_in_the_query_string_is_refused(self, field: str) -> None:
        request = _patch_headers(FakeRequest(query={field: str(TENANT_A)}))
        with pytest.raises(HTTPException) as exc:
            await reject_client_tenant_id(request)
        assert exc.value.status_code == 400
        assert field in exc.value.detail

    @pytest.mark.asyncio
    async def test_the_tenant_header_is_refused(self) -> None:
        request = _patch_headers(FakeRequest(headers={"X-Tenant-Id": str(TENANT_A)}))
        with pytest.raises(HTTPException) as exc:
            await reject_client_tenant_id(request)
        assert exc.value.status_code == 400

    @pytest.mark.parametrize("field", ["tenant_id", "tenantId", "tenant"])
    @pytest.mark.asyncio
    async def test_a_tenant_field_in_a_json_body_is_refused(self, field: str) -> None:
        request = _patch_headers(
            FakeRequest(method="POST", body={"name": "x", field: str(TENANT_A)})
        )
        with pytest.raises(HTTPException) as exc:
            await reject_client_tenant_id(request)
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_refused_rather_than_ignored(self) -> None:
        """Ignoring would teach clients that sending it is acceptable, and
        leave the next reader to prove it is unused."""
        request = _patch_headers(FakeRequest(query={"tenant_id": str(TENANT_A)}))
        with pytest.raises(HTTPException):
            await reject_client_tenant_id(request)

    @pytest.mark.asyncio
    async def test_a_clean_json_body_passes(self) -> None:
        request = _patch_headers(FakeRequest(method="POST", body={"name": "Support"}))
        await reject_client_tenant_id(request)

    @pytest.mark.asyncio
    async def test_malformed_json_is_left_to_the_validator(self) -> None:
        """The guard is not a body validator; a 422 from the schema is a
        better error than a 400 from here."""
        request = _patch_headers(
            FakeRequest(method="POST", headers={"content-type": "application/json"})
        )
        request._body = b"{not json"
        await reject_client_tenant_id(request)

    @pytest.mark.asyncio
    async def test_a_non_json_body_is_not_parsed(self) -> None:
        request = _patch_headers(FakeRequest(method="POST", headers={"content-type": "text/plain"}))
        request._body = b"tenant_id=whatever"
        await reject_client_tenant_id(request)

    @pytest.mark.asyncio
    async def test_a_json_array_body_is_accepted(self) -> None:
        """Only a top-level object can carry the field."""
        request = _patch_headers(FakeRequest(method="POST", body=[{"tenant_id": "x"}]))
        await reject_client_tenant_id(request)


# --------------------------------------------------------------------------- #
# Spec 8: permissions
# --------------------------------------------------------------------------- #


class TestRequirePermission:
    @pytest.mark.asyncio
    async def test_a_held_permission_passes(self) -> None:
        dependency = require_permission(Permission.AGENTS_READ)
        principal = tenant_principal(permissions=(Permission.AGENTS_READ.value,))
        assert await dependency(principal) is principal

    @pytest.mark.asyncio
    async def test_a_missing_permission_is_403_not_404(self) -> None:
        """The caller is authenticated and the resource class exists.

        Masking authorization as absence would make a permissions bug
        indistinguishable from a missing route.
        """
        dependency = require_permission(Permission.AGENTS_WRITE)
        with pytest.raises(HTTPException) as exc:
            await dependency(tenant_principal(permissions=(Permission.AGENTS_READ.value,)))
        assert exc.value.status_code == 403
        assert "agents.write" in exc.value.detail

    @pytest.mark.asyncio
    async def test_every_named_permission_is_required_not_any(self) -> None:
        """An endpoint needing two capabilities needs both.

        "Any" would make a compound requirement quietly weaker than it reads.
        """
        dependency = require_permission(Permission.AGENTS_WRITE, Permission.AGENTS_PUBLISH)
        with pytest.raises(HTTPException) as exc:
            await dependency(tenant_principal(permissions=(Permission.AGENTS_WRITE.value,)))
        assert "agents.publish" in exc.value.detail

    @pytest.mark.asyncio
    async def test_super_admin_passes_any_permission(self) -> None:
        dependency = require_permission(Permission.BILLING_READ, Permission.USERS_MANAGE)
        principal = tenant_principal(
            roles=(PlatformRole.SUPER_ADMIN.value,), tenant_id=None, is_platform=True
        )
        assert await dependency(principal) is principal

    @pytest.mark.asyncio
    async def test_a_viewer_cannot_write(self) -> None:
        """The seeded VIEWER role holds only read permissions."""
        from shared.models import TENANT_ROLE_PERMISSIONS

        viewer_permissions = tuple(p.value for p in TENANT_ROLE_PERMISSIONS[TenantRole.VIEWER])
        dependency = require_permission(Permission.AGENTS_WRITE)
        with pytest.raises(HTTPException) as exc:
            await dependency(
                tenant_principal(roles=(TenantRole.VIEWER.value,), permissions=viewer_permissions)
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_an_analyst_cannot_manage_users(self) -> None:
        from shared.models import TENANT_ROLE_PERMISSIONS

        analyst_permissions = tuple(p.value for p in TENANT_ROLE_PERMISSIONS[TenantRole.ANALYST])
        dependency = require_permission(Permission.USERS_MANAGE)
        with pytest.raises(HTTPException):
            await dependency(
                tenant_principal(roles=(TenantRole.ANALYST.value,), permissions=analyst_permissions)
            )


class TestPlatformGuards:
    @pytest.mark.asyncio
    async def test_a_platform_user_passes(self) -> None:
        dependency = require_platform_user()
        principal = tenant_principal(tenant_id=None, is_platform=True, roles=())
        assert await dependency(principal) is principal

    @pytest.mark.asyncio
    async def test_a_tenant_user_is_refused(self) -> None:
        dependency = require_platform_user()
        with pytest.raises(HTTPException) as exc:
            await dependency(tenant_principal())
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_super_admin_passes_the_super_admin_guard(self) -> None:
        dependency = require_super_admin()
        principal = tenant_principal(
            roles=(PlatformRole.SUPER_ADMIN.value,), tenant_id=None, is_platform=True
        )
        assert await dependency(principal) is principal

    @pytest.mark.asyncio
    async def test_a_platform_operator_is_refused_super_admin_endpoints(self) -> None:
        dependency = require_super_admin()
        principal = tenant_principal(
            roles=(PlatformRole.PLATFORM_OPERATOR.value,), tenant_id=None, is_platform=True
        )
        with pytest.raises(HTTPException) as exc:
            await dependency(principal)
        assert exc.value.status_code == 403


# --------------------------------------------------------------------------- #
# Tenant scope
# --------------------------------------------------------------------------- #


class TestTenantScope:
    @pytest.mark.asyncio
    async def test_a_tenant_user_gets_a_scope_bound_to_their_tenant(self) -> None:
        scope = await get_tenant_scope(tenant_principal(), session=object())  # type: ignore[arg-type]
        assert scope.tenant_id == TENANT_A

    @pytest.mark.asyncio
    async def test_a_platform_user_is_refused_a_tenant_scope(self) -> None:
        """A SUPER_ADMIN must not write into a tenant simply by calling a
        tenant endpoint; acting inside a tenant is a separate, explicit
        operation."""
        principal = tenant_principal(
            roles=(PlatformRole.SUPER_ADMIN.value,), tenant_id=None, is_platform=True
        )
        with pytest.raises(HTTPException) as exc:
            await get_tenant_scope(principal, session=object())  # type: ignore[arg-type]
        assert exc.value.status_code == 403
        assert "tenant" in exc.value.detail.lower()


class TestClientIp:
    @pytest.mark.asyncio
    async def test_the_socket_address_is_used_by_default(self) -> None:
        assert await get_client_ip(FakeRequest(), x_forwarded_for=None) == "10.0.0.5"  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_the_left_most_forwarded_address_wins(self) -> None:
        """The left-most entry is the original client."""
        result = await get_client_ip(
            FakeRequest(),  # type: ignore[arg-type]
            x_forwarded_for="203.0.113.9, 10.0.0.1, 10.0.0.2",
        )
        assert result == "203.0.113.9"

    @pytest.mark.asyncio
    async def test_an_empty_forwarded_header_falls_back(self) -> None:
        assert await get_client_ip(FakeRequest(), x_forwarded_for="  ") == "10.0.0.5"  # type: ignore[arg-type]
