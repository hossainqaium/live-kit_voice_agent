"""Request and response models for authentication (spec 67)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    #: Not length-validated on the way in. A minimum here would tell an
    #: attacker which passwords are too short to be real, and the stored hash
    #: is what actually gates access.
    password: str = Field(min_length=1, max_length=256)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    #: OAuth2 names this field ``token_type``; it is a discriminator, not a
    #: secret, but ruff's hardcoded-password rule matches on the name.
    token_type: str = "bearer"  # noqa: S105
    expires_in: int = Field(description="Access token lifetime in seconds")


class PrincipalResponse(BaseModel):
    """Who the caller is, as the server sees them.

    Returned so a frontend can render the right navigation. It is a
    convenience, not a security boundary: the backend enforces every
    permission independently (spec 8).
    """

    user_id: uuid.UUID
    email: str
    #: Absent for platform staff, who belong to no tenant.
    tenant_id: uuid.UUID | None
    is_platform_user: bool
    roles: list[str]
    permissions: list[str]
