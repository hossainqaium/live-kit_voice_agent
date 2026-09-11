"""Authentication and RBAC (spec 8).

Covers the token contract and the permission checks directly, without a
database: what is being tested is which claims a token carries, which it
refuses, and how a permission decision is made.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.core.security import (
    MAX_PASSWORD_BYTES,
    MIN_PASSWORD_LENGTH,
    AuthError,
    Principal,
    TokenExpiredError,
    TokenInvalidError,
    create_token,
    decode_token,
    hash_password,
    token_issued_at,
    verify_password,
)
from shared.models import Permission, PlatformRole, TenantRole

TENANT_A = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
TENANT_B = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
USER = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")


def tenant_principal(
    *,
    tenant_id: uuid.UUID = TENANT_A,
    roles: tuple[str, ...] = (TenantRole.TENANT_ADMIN.value,),
    permissions: tuple[str, ...] = (),
) -> Principal:
    return Principal(
        user_id=USER,
        email="admin@dev.example.com",
        tenant_id=tenant_id,
        roles=frozenset(roles),
        permissions=frozenset(permissions),
        is_platform_user=False,
    )


def platform_principal(*, roles: tuple[str, ...] = (PlatformRole.SUPER_ADMIN.value,)) -> Principal:
    return Principal(
        user_id=USER,
        email="super@platform.example.com",
        tenant_id=None,
        roles=frozenset(roles),
        permissions=frozenset(),
        is_platform_user=True,
    )


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #


class TestPasswordHashing:
    def test_a_password_verifies_against_its_hash(self) -> None:
        digest = hash_password("CorrectHorseBattery1")
        assert verify_password("CorrectHorseBattery1", digest)

    def test_a_wrong_password_does_not_verify(self) -> None:
        digest = hash_password("CorrectHorseBattery1")
        assert not verify_password("CorrectHorseBattery2", digest)

    def test_the_hash_is_salted(self) -> None:
        """Two hashes of one password must differ, or identical passwords are
        identifiable across accounts from the stored values alone."""
        assert hash_password("SamePassword12345") != hash_password("SamePassword12345")

    def test_the_plaintext_never_appears_in_the_hash(self) -> None:
        assert "CorrectHorseBattery1" not in hash_password("CorrectHorseBattery1")

    def test_a_short_password_is_refused(self) -> None:
        with pytest.raises(ValueError, match=str(MIN_PASSWORD_LENGTH)):
            hash_password("short")

    def test_an_over_long_password_is_refused_rather_than_truncated(self) -> None:
        """bcrypt silently ignores everything past 72 bytes.

        Accepting such a password would mean the tail did nothing while the
        user believed it added strength.
        """
        with pytest.raises(ValueError, match="truncate"):
            hash_password("a" * (MAX_PASSWORD_BYTES + 1))

    def test_a_malformed_hash_denies_rather_than_raising(self) -> None:
        """A corrupt row should deny access, not produce a 500 that
        distinguishes it from a wrong password."""
        assert not verify_password("anything", "not-a-bcrypt-hash")


# --------------------------------------------------------------------------- #
# Token claims
# --------------------------------------------------------------------------- #


class TestTokenClaims:
    def test_a_round_trip_preserves_identity(self) -> None:
        principal = tenant_principal(permissions=(Permission.AGENTS_READ.value,))
        decoded = decode_token(create_token(principal=principal))
        assert decoded.user_id == principal.user_id
        assert decoded.email == principal.email
        assert decoded.tenant_id == TENANT_A
        assert decoded.permissions == frozenset({Permission.AGENTS_READ.value})

    def test_the_tenant_is_a_signed_claim(self) -> None:
        """Spec 7: tenant identity comes from the token, not the request."""
        token = create_token(principal=tenant_principal(tenant_id=TENANT_B))
        assert decode_token(token).tenant_id == TENANT_B

    def test_a_platform_token_carries_no_tenant(self) -> None:
        decoded = decode_token(create_token(principal=platform_principal()))
        assert decoded.tenant_id is None
        assert decoded.is_platform_user is True

    def test_a_tampered_token_is_refused(self) -> None:
        token = create_token(principal=tenant_principal())
        header, payload, signature = token.split(".")
        forged = f"{header}.{payload}.{'a' * len(signature)}"
        with pytest.raises(TokenInvalidError):
            decode_token(forged)

    def test_an_expired_token_is_refused(self) -> None:
        token = create_token(principal=tenant_principal(), expires_in=timedelta(seconds=-10))
        with pytest.raises(TokenExpiredError):
            decode_token(token)

    def test_a_refresh_token_is_not_accepted_as_an_access_token(self) -> None:
        """A refresh token carries no permissions.

        Without the kind check it would authenticate successfully as a
        principal holding nothing, which passes any endpoint that only checks
        authentication.
        """
        refresh = create_token(principal=tenant_principal(), kind="refresh")
        with pytest.raises(TokenInvalidError, match="expected a access token"):
            decode_token(refresh, expected="access")

    def test_an_access_token_is_not_accepted_as_a_refresh_token(self) -> None:
        access = create_token(principal=tenant_principal())
        with pytest.raises(TokenInvalidError):
            decode_token(access, expected="refresh")

    def test_a_refresh_token_carries_no_permissions(self) -> None:
        """A stolen refresh token must not confer authority for its lifetime."""
        principal = tenant_principal(permissions=(Permission.AGENTS_WRITE.value,))
        decoded = decode_token(
            create_token(principal=principal, kind="refresh"), expected="refresh"
        )
        assert decoded.permissions == frozenset()

    def test_a_token_claiming_both_scopes_is_refused(self) -> None:
        """Platform staff belong to no tenant, and the database enforces the
        same rule. A token claiming both would leave every authorization
        decision guessing which wins."""
        import jwt

        from app.core.settings import get_settings

        settings = get_settings()
        forged = jwt.encode(
            {
                "sub": str(USER),
                "typ": "access",
                "exp": 9999999999,
                "plat": True,
                "tid": str(TENANT_A),
            },
            settings.jwt_secret.get_secret_value(),
            algorithm=settings.jwt_algorithm,
        )
        with pytest.raises(TokenInvalidError, match="both platform scope and a tenant"):
            decode_token(forged)

    def test_tokens_are_individually_identifiable(self) -> None:
        """A jti per token is what allows revoking one without the rest."""
        import jwt

        principal = tenant_principal()
        first = jwt.decode(create_token(principal=principal), options={"verify_signature": False})
        second = jwt.decode(create_token(principal=principal), options={"verify_signature": False})
        assert first["jti"] != second["jti"]

    def test_issued_at_is_readable_without_verifying(self) -> None:
        """Used to compare against tokens_valid_from, so a revocation takes
        effect immediately rather than at expiry."""
        assert token_issued_at(create_token(principal=tenant_principal())) is not None

    def test_issued_at_of_garbage_is_none(self) -> None:
        assert token_issued_at("not-a-token") is None


# --------------------------------------------------------------------------- #
# Permission decisions (spec 8)
# --------------------------------------------------------------------------- #


class TestPermissionChecks:
    def test_a_held_permission_is_granted(self) -> None:
        principal = tenant_principal(permissions=(Permission.AGENTS_READ.value,))
        assert principal.has_permission(Permission.AGENTS_READ)

    def test_an_unheld_permission_is_denied(self) -> None:
        principal = tenant_principal(permissions=(Permission.AGENTS_READ.value,))
        assert not principal.has_permission(Permission.AGENTS_WRITE)

    def test_super_admin_holds_everything(self) -> None:
        principal = platform_principal()
        assert all(principal.has_permission(p) for p in Permission)

    def test_platform_operator_does_not_hold_everything(self) -> None:
        """Only SUPER_ADMIN gets the blanket grant; PLATFORM_OPERATOR must not
        inherit tenant-data permissions by virtue of being platform staff."""
        principal = platform_principal(roles=(PlatformRole.PLATFORM_OPERATOR.value,))
        assert not principal.has_permission(Permission.BILLING_READ)

    def test_a_string_permission_works_too(self) -> None:
        principal = tenant_principal(permissions=("agents.read",))
        assert principal.has_permission("agents.read")

    def test_an_unknown_permission_is_denied(self) -> None:
        assert not tenant_principal().has_permission("agents.delete_everything")

    def test_a_principal_cannot_be_mutated(self) -> None:
        """A handler must not be able to widen its own authority mid-request."""
        principal = tenant_principal()
        with pytest.raises(Exception):  # noqa: B017 - frozen dataclass
            principal.permissions = frozenset({Permission.USERS_MANAGE.value})  # type: ignore[misc]


class TestTenantRequirement:
    def test_a_tenant_principal_yields_its_tenant(self) -> None:
        assert tenant_principal().require_tenant() == TENANT_A

    def test_a_platform_principal_has_no_tenant_to_act_in(self) -> None:
        """Returning None instead would let a tenant-scoped query run unscoped."""
        with pytest.raises(AuthError, match="requires a tenant context"):
            platform_principal().require_tenant()
