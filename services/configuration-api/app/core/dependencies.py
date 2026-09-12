"""Request dependencies: authentication, RBAC and tenant scoping.

Spec 7 (tenant isolation), 8 (RBAC enforced independently of the frontend).

The design intent is that isolation is **structural rather than remembered**.
A handler does not receive a tenant ID it could get wrong; it receives a
``TenantScope`` that already knows which tenant it may touch, derived from the
token. There is no supported way for a handler to act on a tenant the caller
does not belong to, so a forgotten filter cannot become a cross-tenant leak.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    AuthError,
    Principal,
    TokenExpiredError,
    TokenInvalidError,
    decode_token,
    token_issued_at,
)
from app.db.models import User
from app.db.session import get_session
from shared.logging import bind, get_logger
from shared.models import Permission

logger = get_logger(__name__)

#: auto_error=False so a missing header produces our own 401 with a
#: WWW-Authenticate challenge rather than FastAPI's bare 403.
_bearer = HTTPBearer(auto_error=False, description="JWT access token")

#: Request fields that would carry a client-supplied tenant identity. Spec 7
#: says never trust one from the browser, so their presence is rejected
#: outright rather than ignored: ignoring teaches a client that sending it is
#: acceptable, and the next reader of the code has to prove it is unused.
_FORBIDDEN_TENANT_FIELDS = ("tenant_id", "tenantId", "tenant")


def _unauthenticated(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    session: Annotated[AsyncSession, Depends(get_session)] = None,  # type: ignore[assignment]
) -> Principal:
    """Resolve the authenticated caller from the bearer token.

    Beyond signature and expiry, the user row is re-checked on every request so
    that deactivating an account or rotating a password takes effect at once
    rather than whenever the token happens to expire. That costs one indexed
    primary-key lookup, which is the right trade for being able to revoke
    access immediately.
    """
    if credentials is None or not credentials.credentials:
        raise _unauthenticated("an access token is required")

    token = credentials.credentials

    try:
        principal = decode_token(token, expected="access")
    except TokenExpiredError as exc:
        raise _unauthenticated("access token has expired") from exc
    except TokenInvalidError as exc:
        raise _unauthenticated(str(exc)) from exc

    user = (
        await session.execute(select(User).where(User.id == principal.user_id))
    ).scalar_one_or_none()

    if user is None:
        # A valid signature over a deleted user. Treated as unauthenticated
        # rather than 404: the caller should learn nothing about which user ids
        # exist.
        raise _unauthenticated("account no longer exists")

    if not user.is_active:
        raise _unauthenticated("account is disabled")

    if user.tokens_valid_from is not None:
        issued = token_issued_at(token)
        if issued is None or issued < user.tokens_valid_from:
            raise _unauthenticated("token has been revoked; sign in again")

    # The token is a snapshot; the database is authoritative. A user moved
    # between tenants would otherwise keep acting in the old one until expiry.
    if user.is_platform_user != principal.is_platform_user or user.tenant_id != principal.tenant_id:
        raise _unauthenticated("token no longer matches the account; sign in again")

    # Correlate the rest of the request's logs with who made it.
    bind(user_id=str(principal.user_id))
    if principal.tenant_id is not None:
        bind(tenant_id=str(principal.tenant_id))

    request.state.principal = principal
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


async def reject_client_tenant_id(request: Request) -> None:
    """Refuse any request that tries to supply its own tenant identity (spec 7).

    Applied to the whole versioned router. Checks the query string, a
    convenience header, and a JSON body's top level. Nested occurrences are not
    searched: the body is read here before validation and walking arbitrary
    depth on every request is not worth the cost, and a nested tenant_id cannot
    influence scoping anyway because handlers never read one.
    """
    for field in _FORBIDDEN_TENANT_FIELDS:
        if field in request.query_params:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"{field!r} may not be supplied by the client; "
                    "tenant identity comes from the authenticated session"
                ),
            )

    if request.headers.get("X-Tenant-Id"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "X-Tenant-Id may not be supplied by the client; "
                "tenant identity comes from the authenticated session"
            ),
        )

    if request.method in ("POST", "PUT", "PATCH"):
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("application/json"):
            # Starlette caches the body, so reading it here does not prevent
            # the handler from reading it again.
            raw = await request.body()
            if raw:
                import json

                try:
                    payload = json.loads(raw)
                except ValueError:
                    return  # malformed JSON is the validator's problem
                if isinstance(payload, dict):
                    for field in _FORBIDDEN_TENANT_FIELDS:
                        if field in payload:
                            raise HTTPException(
                                status_code=status.HTTP_400_BAD_REQUEST,
                                detail=(
                                    f"{field!r} may not be supplied in the request body; "
                                    "tenant identity comes from the authenticated session"
                                ),
                            )


# --------------------------------------------------------------------------- #
# Permissions (spec 8)
# --------------------------------------------------------------------------- #


def require_permission(*permissions: Permission) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory requiring every named permission.

    Enforced here, at the API layer, independently of the frontend (spec 8).
    Hiding a control in the UI is presentation, never access control.

    Requiring *all* rather than any: an endpoint that needs two capabilities
    needs both, and "any" would make a compound requirement quietly weaker
    than it reads.
    """

    async def dependency(principal: CurrentPrincipal) -> Principal:
        missing = [p.value for p in permissions if not principal.has_permission(p)]
        if missing:
            logger.warning(
                "authorization_denied",
                extra={"missing_permissions": missing, "held": sorted(principal.permissions)},
            )
            # 403, not 404: the caller is authenticated and the resource class
            # exists. Masking authorisation as absence would make a permissions
            # bug indistinguishable from a missing route.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"missing required permission(s): {', '.join(missing)}",
            )
        return principal

    return dependency


def require_platform_user() -> Callable[..., Awaitable[Principal]]:
    """Dependency requiring platform staff (spec 60)."""

    async def dependency(principal: CurrentPrincipal) -> Principal:
        if not principal.is_platform_user:
            logger.warning("authorization_denied", extra={"reason": "not a platform user"})
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="this operation is restricted to platform administrators",
            )
        return principal

    return dependency


def require_super_admin() -> Callable[..., Awaitable[Principal]]:
    """Dependency requiring SUPER_ADMIN (spec 48)."""

    async def dependency(principal: CurrentPrincipal) -> Principal:
        if not principal.is_super_admin:
            logger.warning("authorization_denied", extra={"reason": "not a super admin"})
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="this operation is restricted to SUPER_ADMIN",
            )
        return principal

    return dependency


# --------------------------------------------------------------------------- #
# Tenant scope (spec 6, 7)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TenantScope:
    """The tenant a request may act on, and the session to act with.

    Handlers take this instead of a raw session plus a tenant ID they have to
    remember to apply. The tenant is already fixed from the token, so the
    unsafe version — a query with no tenant filter — is not expressible through
    the normal path.
    """

    tenant_id: uuid.UUID
    session: AsyncSession
    principal: Principal


async def get_tenant_scope(
    principal: CurrentPrincipal,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TenantScope:
    """Build the tenant scope for a tenant-scoped endpoint."""
    try:
        tenant_id = principal.require_tenant()
    except AuthError as exc:
        # A platform user has no tenant of their own. Acting inside one is a
        # separate, explicit operation rather than something that happens by
        # default, so that a SUPER_ADMIN cannot accidentally write to a tenant
        # simply by calling a tenant endpoint.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "this endpoint acts within a tenant, and this account has none. "
                "Platform administrators must use the platform endpoints."
            ),
        ) from exc

    # Set the PostgreSQL RLS context GUC for this transaction (spec 7, 3b.1).
    # `set_config(name, value, is_local=true)` is transaction-scoped: it
    # resets when the transaction ends, so one request cannot bleed into
    # another.  PostgreSQL's `tenant_isolation` policy on every tenant-owned
    # table reads this GUC and excludes rows that do not match.
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(tenant_id)},
    )

    return TenantScope(tenant_id=tenant_id, session=session, principal=principal)


CurrentTenant = Annotated[TenantScope, Depends(get_tenant_scope)]


async def get_client_ip(
    request: Request,
    x_forwarded_for: Annotated[str | None, Header()] = None,
) -> str | None:
    """Best-effort client address for the audit log (spec 69).

    Takes the left-most entry of ``X-Forwarded-For`` when present, since that
    is the original client. The header is client-controllable, so this is
    evidence for an audit trail rather than an access-control input.
    """
    if x_forwarded_for:
        first = x_forwarded_for.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else None


ClientIp = Annotated[str | None, Depends(get_client_ip)]
