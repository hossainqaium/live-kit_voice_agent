"""Password hashing and JWT issuance (spec 8, 53).

Two rules this module exists to make structural:

1. **Tenant identity comes from the token, never from the request.** A tenant
   ID in a body, query string or header is ignored — spec 7 says never trust
   one supplied by the browser, so the only path by which a tenant can be
   identified is a signed claim.
2. **Permissions are resolved server-side** from the user's role assignments
   and embedded in the token at issue time. The frontend cannot widen them
   (spec 8).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from passlib.context import CryptContext

from app.core.settings import get_settings
from shared.models import Permission, PlatformRole

TokenType = Literal["access", "refresh"]

#: bcrypt rather than a faster hash on purpose: password verification should be
#: slow. The cost is paid once per login, not per request, because subsequent
#: requests carry a token.
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

#: bcrypt silently truncates at 72 bytes, so a longer password would have its
#: tail ignored. Rejecting is honest; truncating is not.
MAX_PASSWORD_BYTES = 72

MIN_PASSWORD_LENGTH = 12


class AuthError(Exception):
    """Authentication or token validation failure."""


class TokenExpiredError(AuthError):
    pass


class TokenInvalidError(AuthError):
    pass


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #


def hash_password(password: str) -> str:
    """Hash a password for storage.

    Raises on a password bcrypt cannot represent, rather than storing a hash of
    a silently truncated value.
    """
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(
            f"password exceeds {MAX_PASSWORD_BYTES} bytes, which bcrypt would truncate"
        )
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return _pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Check a password against a stored hash.

    Returns False rather than raising on a malformed hash: a corrupt row should
    deny access, not produce a 500 that distinguishes it from a wrong password.
    """
    try:
        return _pwd_context.verify(password, password_hash)
    except Exception:
        return False


def needs_rehash(password_hash: str) -> bool:
    """Whether a stored hash uses outdated parameters."""
    return _pwd_context.needs_update(password_hash)


# --------------------------------------------------------------------------- #
# The authenticated caller
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is making a request, and what they may do.

    Built only from a verified token. Frozen because a request handler must not
    be able to widen its own authority mid-request.
    """

    user_id: uuid.UUID
    email: str

    #: The tenant this request acts within. ``None`` for platform staff, who
    #: belong to no tenant.
    tenant_id: uuid.UUID | None

    #: Role names held in this context.
    roles: frozenset[str] = field(default_factory=frozenset)

    #: Resolved permission codes (spec 8).
    permissions: frozenset[str] = field(default_factory=frozenset)

    is_platform_user: bool = False

    #: Correlates a token with the session that issued it, so revoking one
    #: session does not require invalidating every token for the user.
    session_id: str | None = None

    @property
    def is_super_admin(self) -> bool:
        return PlatformRole.SUPER_ADMIN.value in self.roles

    def has_permission(self, permission: Permission | str) -> bool:
        """Whether this principal holds a permission.

        SUPER_ADMIN is the single deliberate shortcut: it holds everything, so
        platform operations do not need a permission row per action.
        """
        if self.is_super_admin:
            return True
        code = permission.value if isinstance(permission, Permission) else permission
        return code in self.permissions

    def require_tenant(self) -> uuid.UUID:
        """Return the acting tenant, or fail.

        Used by tenant-scoped handlers. A platform user with no tenant context
        reaching one is a routing mistake, and returning None would let the
        query run unscoped.
        """
        if self.tenant_id is None:
            raise AuthError("this operation requires a tenant context")
        return self.tenant_id


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #


def create_token(
    *,
    principal: Principal,
    kind: TokenType = "access",
    expires_in: timedelta | None = None,
) -> str:
    """Issue a signed token for a principal.

    The tenant and the permission set are claims, so a request cannot alter
    either. ``jti`` and ``sid`` exist so a single token or a whole session can
    be revoked without invalidating every token the user holds.
    """
    settings = get_settings()
    now = datetime.now(UTC)

    if expires_in is None:
        expires_in = (
            timedelta(minutes=settings.access_token_ttl_minutes)
            if kind == "access"
            else timedelta(days=settings.refresh_token_ttl_days)
        )

    claims: dict[str, Any] = {
        "sub": str(principal.user_id),
        "email": principal.email,
        "typ": kind,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_in).timestamp()),
        "jti": uuid.uuid4().hex,
        "sid": principal.session_id or uuid.uuid4().hex,
        "plat": principal.is_platform_user,
    }

    # Only present for a tenant-scoped principal. An absent claim is
    # unambiguous; an empty string would have to be interpreted.
    if principal.tenant_id is not None:
        claims["tid"] = str(principal.tenant_id)

    # A refresh token carries no authority: it only proves identity long enough
    # to mint a new access token. Embedding permissions in it would mean a
    # stolen refresh token conferred them for its whole lifetime, and would
    # freeze a revoked permission until it expired.
    if kind == "access":
        claims["roles"] = sorted(principal.roles)
        claims["perms"] = sorted(principal.permissions)

    return jwt.encode(
        claims,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def decode_token(token: str, *, expected: TokenType = "access") -> Principal:
    """Verify a token and return its principal.

    Signature, expiry and token kind are all checked. The kind check matters:
    without it a refresh token — which carries no permissions — would be
    accepted as an access token and authorise a permission-free principal
    against endpoints that only check authentication.
    """
    settings = get_settings()
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub", "typ"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError("token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenInvalidError(f"token is not valid: {exc}") from exc

    if claims.get("typ") != expected:
        raise TokenInvalidError(f"expected a {expected} token, got {claims.get('typ')!r}")

    try:
        user_id = uuid.UUID(str(claims["sub"]))
    except (KeyError, ValueError) as exc:
        raise TokenInvalidError("token subject is not a user id") from exc

    tenant_id: uuid.UUID | None = None
    raw_tenant = claims.get("tid")
    if raw_tenant is not None:
        try:
            tenant_id = uuid.UUID(str(raw_tenant))
        except ValueError as exc:
            raise TokenInvalidError("token tenant claim is not a uuid") from exc

    is_platform = bool(claims.get("plat", False))

    # A token may not claim both: platform staff belong to no tenant, and the
    # database enforces the same rule. Accepting both would leave every
    # authorization decision to guess which one wins.
    if is_platform and tenant_id is not None:
        raise TokenInvalidError("token claims both platform scope and a tenant")

    return Principal(
        user_id=user_id,
        email=str(claims.get("email", "")),
        tenant_id=tenant_id,
        roles=frozenset(claims.get("roles") or ()),
        permissions=frozenset(claims.get("perms") or ()),
        is_platform_user=is_platform,
        session_id=claims.get("sid"),
    )


def token_issued_at(token: str) -> datetime | None:
    """Issue time of a token, without verifying it.

    Used only to compare against a user's ``tokens_valid_from``, so that
    deactivating a user or rotating their password takes effect immediately
    instead of at token expiry. The token itself is verified separately.
    """
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.InvalidTokenError:
        return None
    issued = claims.get("iat")
    if issued is None:
        return None
    return datetime.fromtimestamp(int(issued), tz=UTC)
