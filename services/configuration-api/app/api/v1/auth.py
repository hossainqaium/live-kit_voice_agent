"""Authentication endpoints (spec 8, 67)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import ClientIp, CurrentPrincipal
from app.core.security import TokenInvalidError, decode_token
from app.core.settings import get_settings
from app.db.session import get_session
from app.schemas.auth import (
    LoginRequest,
    PrincipalResponse,
    RefreshRequest,
    TokenResponse,
)
from app.services import audit, auth_service
from shared.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Sign in and receive a token pair",
    responses={401: {"description": "Email or password is incorrect"}},
)
async def login(
    payload: LoginRequest,
    request: Request,
    session: SessionDep,
    client_ip: ClientIp,
) -> TokenResponse:
    """Exchange credentials for tokens.

    The response is identical for an unknown email and a wrong password, and
    both take the same time, so the endpoint cannot be used to discover which
    addresses have accounts.
    """
    settings = get_settings()
    try:
        pair = await auth_service.authenticate(
            session, email=str(payload.email), password=payload.password
        )
    except auth_service.AccountDisabledError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except auth_service.AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="email or password is incorrect",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    # Audited without a principal: at this point the sign-in is what created
    # one, and the email is the useful identifier.
    await audit.record(
        session,
        principal=None,
        action="user.signed_in",
        resource_type="user",
        new_value={"email": str(payload.email)},
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()

    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=settings.access_token_ttl_minutes * 60,
    )


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Exchange a refresh token for a new pair",
    responses={401: {"description": "Refresh token is missing, invalid or expired"}},
)
async def refresh(payload: RefreshRequest, session: SessionDep) -> TokenResponse:
    """Rotate tokens.

    Permissions are re-resolved from the database rather than copied from the
    old token, so a role revoked since sign-in stops applying here instead of
    lasting until the refresh token expires.
    """
    settings = get_settings()
    try:
        principal = decode_token(payload.refresh_token, expected="refresh")
    except TokenInvalidError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except Exception as exc:  # expired
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="refresh token has expired; sign in again",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    try:
        pair = await auth_service.refresh(session, principal)
    except auth_service.AuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=settings.access_token_ttl_minutes * 60,
    )


@router.get(
    "/me",
    response_model=PrincipalResponse,
    summary="Describe the authenticated caller",
)
async def me(principal: CurrentPrincipal) -> PrincipalResponse:
    """Return the caller's identity, roles and permissions.

    For rendering navigation. The frontend may use this to decide what to show;
    it does not decide what is allowed (spec 8).
    """
    return PrincipalResponse(
        user_id=principal.user_id,
        email=principal.email,
        tenant_id=principal.tenant_id,
        is_platform_user=principal.is_platform_user,
        roles=sorted(principal.roles),
        permissions=sorted(principal.permissions),
    )
