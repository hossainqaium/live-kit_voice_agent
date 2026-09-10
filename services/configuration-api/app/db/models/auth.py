"""Users, roles and permissions (spec 8).

Roles are rows rather than hard-coded strings so the platform can add one
without a deployment, and so ``role_permissions`` is inspectable — an
authorization model you cannot query is one nobody can audit.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, enum_column
from shared.models import RoleScope


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A person who signs in.

    ``tenant_id`` is nullable because platform staff (SUPER_ADMIN,
    PLATFORM_OPERATOR) belong to no tenant. This is the one deliberate
    exception to spec 6's tenant_id rule, and the check constraint below
    prevents the ambiguous middle ground.
    """

    __tablename__ = "users"
    __table_args__ = (
        # A user is either platform staff or belongs to exactly one tenant.
        # Without this, "platform user that also has a tenant" would be
        # representable, and every authorization query would need to guess
        # which one wins.
        CheckConstraint(
            "(is_platform_user AND tenant_id IS NULL) "
            "OR (NOT is_platform_user AND tenant_id IS NOT NULL)",
            name="platform_user_has_no_tenant",
        ),
        # Email is unique per tenant, and unique among platform users. The
        # same person can hold accounts in two tenants.
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
    )

    is_platform_user: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: CITEXT so lookups are case-insensitive without every query remembering
    #: to lower() — a forgotten lower() is a duplicate-account bug.
    email: Mapped[str] = mapped_column(postgresql.CITEXT(320), nullable=False, index=True)

    full_name: Mapped[str] = mapped_column(String(255), nullable=False)

    #: Argon2/bcrypt hash. Never a plaintext or reversible value (spec 53).
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Invalidates tokens issued before this moment, so deactivating a user or
    #: rotating their password takes effect immediately instead of at token
    #: expiry.
    tokens_valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<User {self.email}>"


class Role(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A named set of permissions, scoped to the platform or to a tenant."""

    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("name", name="uq_roles_name"),)

    #: Matches PlatformRole or TenantRole values.
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scope: Mapped[RoleScope] = enum_column(RoleScope, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)

    #: Built-in roles ship with the platform and must not be deleted, unlike
    #: any custom role added later.
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    def __repr__(self) -> str:
        return f"<Role {self.name}>"


class Permission(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A granular capability such as ``agents.publish`` (spec 8)."""

    __tablename__ = "permissions"
    __table_args__ = (UniqueConstraint("code", name="uq_permissions_code"),)

    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<Permission {self.code}>"


class RolePermission(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Which permissions a role grants."""

    __tablename__ = "role_permissions"
    __table_args__ = (
        UniqueConstraint("role_id", "permission_id", name="uq_role_permissions_role_permission"),
    )

    role_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("roles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("permissions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


class UserRole(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A role assignment.

    ``tenant_id`` records which tenant the assignment applies to, so one user
    row can hold different roles in different tenants without duplicating the
    account.
    """

    __tablename__ = "user_roles"
    __table_args__ = (
        UniqueConstraint("user_id", "role_id", "tenant_id", name="uq_user_roles_user_role_tenant"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("roles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Null for a platform-scoped role assignment.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
    )
