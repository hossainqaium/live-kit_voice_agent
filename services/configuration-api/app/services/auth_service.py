"""Authentication: sign-in, token issuance, permission resolution (spec 8).

Permissions are resolved from role assignments **here**, server-side, and then
embedded in the access token. The frontend never contributes to the set.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    AuthError,
    Principal,
    create_token,
    hash_password,
    needs_rehash,
    verify_password,
)
from app.db.models import Permission as PermissionRow
from app.db.models import Role, RolePermission, User, UserRole
from shared.logging import get_logger
from shared.models import RoleScope

logger = get_logger(__name__)

#: A hash of a throwaway password, used to keep the verification cost of an
#: unknown email the same as a known one. Without it, response timing
#: distinguishes registered addresses from unregistered ones.
_DUMMY_HASH = hash_password("not-a-real-password-000")


class InvalidCredentialsError(AuthError):
    """Wrong email or password.

    Deliberately one error for both. Telling a caller which half was wrong
    turns the login endpoint into an account-enumeration oracle.
    """


class AccountDisabledError(AuthError):
    pass


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth2 response field, not a secret


async def resolve_permissions(
    session: AsyncSession, user: User
) -> tuple[frozenset[str], frozenset[str]]:
    """Return the role names and permission codes a user holds.

    Resolved from ``user_roles`` → ``roles`` → ``role_permissions`` →
    ``permissions``, scoped to the user's own tenant so that a stale assignment
    to a different tenant cannot grant anything here.
    """
    statement = (
        select(Role.name, Role.scope, PermissionRow.code)
        .select_from(UserRole)
        .join(Role, Role.id == UserRole.role_id)
        .outerjoin(RolePermission, RolePermission.role_id == Role.id)
        .outerjoin(PermissionRow, PermissionRow.id == RolePermission.permission_id)
        .where(UserRole.user_id == user.id)
    )

    rows = (await session.execute(statement)).all()

    roles: set[str] = set()
    permissions: set[str] = set()

    for name, scope, code in rows:
        # A tenant-scoped role only counts for a user who belongs to a tenant,
        # and a platform-scoped role only for platform staff. Without this a
        # user moved out of a tenant could keep tenant authority.
        if scope == RoleScope.TENANT and user.tenant_id is None:
            continue
        if scope == RoleScope.PLATFORM and not user.is_platform_user:
            continue
        roles.add(name)
        if code:
            permissions.add(code)

    return frozenset(roles), frozenset(permissions)


async def authenticate(session: AsyncSession, *, email: str, password: str) -> TokenPair:
    """Verify credentials and issue a token pair.

    The password is always hashed-and-compared, even for an unknown email, so
    that the endpoint takes the same time either way.
    """
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()

    if user is None:
        verify_password(password, _DUMMY_HASH)
        logger.warning("login_failed", extra={"reason": "unknown email"})
        raise InvalidCredentialsError("email or password is incorrect")

    if not verify_password(password, user.password_hash):
        logger.warning("login_failed", extra={"reason": "bad password"})
        raise InvalidCredentialsError("email or password is incorrect")

    if not user.is_active:
        logger.warning("login_failed", extra={"reason": "account disabled"})
        raise AccountDisabledError("this account is disabled")

    # Upgrade the stored hash opportunistically when bcrypt's parameters have
    # moved on. This is the only moment the plaintext is available.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        logger.info("password_hash_upgraded")

    roles, permissions = await resolve_permissions(session, user)

    principal = Principal(
        user_id=user.id,
        email=str(user.email),
        tenant_id=user.tenant_id,
        roles=roles,
        permissions=permissions,
        is_platform_user=user.is_platform_user,
        session_id=uuid.uuid4().hex,
    )

    user.last_login_at = datetime.now(UTC)
    await session.commit()

    logger.info(
        "login_succeeded",
        extra={"roles": sorted(roles), "permission_count": len(permissions)},
    )

    return TokenPair(
        access_token=create_token(principal=principal, kind="access"),
        refresh_token=create_token(principal=principal, kind="refresh"),
    )


async def refresh(session: AsyncSession, principal: Principal) -> TokenPair:
    """Mint a new token pair from a verified refresh token.

    Permissions are resolved again rather than copied, so a role revoked since
    sign-in takes effect at the next refresh instead of persisting for the
    refresh token's whole lifetime.
    """
    user = (
        await session.execute(select(User).where(User.id == principal.user_id))
    ).scalar_one_or_none()

    if user is None or not user.is_active:
        raise InvalidCredentialsError("account is no longer active")

    roles, permissions = await resolve_permissions(session, user)

    refreshed = Principal(
        user_id=user.id,
        email=str(user.email),
        tenant_id=user.tenant_id,
        roles=roles,
        permissions=permissions,
        is_platform_user=user.is_platform_user,
        # Same session id, so rotating tokens does not look like a new sign-in.
        session_id=principal.session_id or uuid.uuid4().hex,
    )

    return TokenPair(
        access_token=create_token(principal=refreshed, kind="access"),
        refresh_token=create_token(principal=refreshed, kind="refresh"),
    )


async def revoke_sessions(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Invalidate every token already issued to a user.

    Sets ``tokens_valid_from`` to now; ``get_principal`` refuses tokens issued
    before it. Used when disabling an account or rotating a password, so access
    stops immediately rather than at token expiry.
    """
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        return
    user.tokens_valid_from = datetime.now(UTC)
    await session.commit()
    logger.info("sessions_revoked", extra={"revoked_user_id": str(user_id)})
