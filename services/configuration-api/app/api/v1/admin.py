"""Tenant administration: users, settings, analytics, usage, recordings.

Spec 8 (users and roles), 39 (recordings), 47 (limits), 57 (business metrics),
61 (tenant console sections).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select

from app.core.dependencies import ClientIp, CurrentTenant, require_permission
from app.core.security import hash_password
from app.db.models import (
    Agent,
    Call,
    CallRecording,
    Role,
    Tenant,
    Usage,
    User,
    UserRole,
)
from app.db.repository import TenantRepository
from app.db.util import as_lookup
from app.schemas.admin import (
    AnalyticsResponse,
    PasswordReset,
    RecordingResponse,
    TenantSettingsResponse,
    TenantSettingsUpdate,
    UsageResponse,
    UsageSummary,
    UserCreate,
    UserResponse,
    UserUpdate,
)
from app.schemas.common import Page
from app.services import audit
from shared.logging import get_logger
from shared.models import CallState, Permission, RoleScope, TransferStatus

logger = get_logger(__name__)

router = APIRouter(tags=["administration"])

#: Terminal states that mean the call was answered and ran (spec 42).
_ANSWERED_STATES = (
    CallState.ANSWERED,
    CallState.AI_CONNECTED,
    CallState.IN_PROGRESS,
    CallState.TRANSFERRING,
    CallState.HUMAN_AGENT,
    CallState.COMPLETED,
)
_FAILED_STATES = (
    CallState.FAILED,
    CallState.TIMEOUT,
    CallState.NO_ANSWER,
    CallState.BUSY,
)
#: Calls still in flight, which is what a concurrency limit counts.
_LIVE_STATES = (
    CallState.NEW,
    CallState.RINGING,
    CallState.ANSWERED,
    CallState.AI_CONNECTED,
    CallState.IN_PROGRESS,
    CallState.TRANSFERRING,
    CallState.HUMAN_AGENT,
)


# --------------------------------------------------------------------------- #
# Users (spec 8)
# --------------------------------------------------------------------------- #


async def _user_roles(
    tenant: CurrentTenant, user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    if not user_ids:
        return {}
    rows = (
        await tenant.session.execute(
            select(UserRole.user_id, Role.name)
            .join(Role, Role.id == UserRole.role_id)
            .where(UserRole.user_id.in_(user_ids), UserRole.tenant_id == tenant.tenant_id)
        )
    ).all()
    grouped: dict[uuid.UUID, list[str]] = {}
    for user_id, role_name in rows:
        grouped.setdefault(user_id, []).append(role_name)
    return grouped


@router.get(
    "/users",
    response_model=Page[UserResponse],
    summary="List this tenant's users",
    dependencies=[Depends(require_permission(Permission.USERS_MANAGE))],
)
async def list_users(
    tenant: CurrentTenant,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[UserResponse]:
    """List users.

    ``users`` has a nullable ``tenant_id`` because platform staff belong to no
    tenant, so the repository refuses to scope it automatically. The filter is
    therefore explicit here, and deliberately excludes platform accounts — a
    tenant administrator has no business seeing them.
    """
    rows = list(
        (
            await tenant.session.execute(
                select(User)
                .where(User.tenant_id == tenant.tenant_id, User.is_platform_user.is_(False))
                .order_by(User.email)
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    total = int(
        (
            await tenant.session.execute(
                select(func.count())
                .select_from(User)
                .where(User.tenant_id == tenant.tenant_id, User.is_platform_user.is_(False))
            )
        ).scalar_one()
    )
    roles = await _user_roles(tenant, [r.id for r in rows])
    return Page(
        items=[
            UserResponse.model_validate(row, from_attributes=True).model_copy(
                update={
                    "roles": sorted(roles.get(row.id, [])),
                    "sessions_revoked": row.tokens_valid_from is not None,
                }
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user in this tenant",
    dependencies=[Depends(require_permission(Permission.USERS_MANAGE))],
)
async def create_user(
    payload: UserCreate,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> UserResponse:
    """Create a tenant user.

    Only tenant roles are assignable here: a tenant administrator granting a
    platform role would be a privilege escalation out of their own tenant,
    which is why ``UserCreate.role`` is typed as ``TenantRole``.
    """
    existing = (
        await tenant.session.execute(select(User.id).where(User.email == str(payload.email)))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="an account with that email already exists",
        )

    try:
        password_hash = hash_password(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    role = (
        await tenant.session.execute(
            select(Role).where(Role.name == payload.role.value, Role.scope == RoleScope.TENANT)
        )
    ).scalar_one_or_none()
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"the role {payload.role.value!r} is not configured; run seed-rbac",
        )

    user = User(
        tenant_id=tenant.tenant_id,
        is_platform_user=False,
        email=str(payload.email),
        full_name=payload.full_name,
        password_hash=password_hash,
    )
    tenant.session.add(user)
    await tenant.session.flush()

    tenant.session.add(UserRole(user_id=user.id, role_id=role.id, tenant_id=tenant.tenant_id))

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="user.created",
        resource_type="user",
        resource_id=user.id,
        new_value={"email": str(payload.email), "role": payload.role.value},
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()

    logger.info("tenant_user_created", extra={"created_user_id": str(user.id)})
    return UserResponse.model_validate(user, from_attributes=True).model_copy(
        update={"roles": [payload.role.value]}
    )


async def _load_tenant_user(tenant: CurrentTenant, user_id: uuid.UUID) -> User:
    user = (
        await tenant.session.execute(
            select(User).where(
                User.id == user_id,
                User.tenant_id == tenant.tenant_id,
                User.is_platform_user.is_(False),
            )
        )
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail=f"no user with id {user_id}")
    return user


@router.put(
    "/users/{user_id}",
    response_model=UserResponse,
    summary="Update a user",
    dependencies=[Depends(require_permission(Permission.USERS_MANAGE))],
)
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> UserResponse:
    """Update a user's name, role or active flag.

    Disabling an account revokes its sessions immediately rather than waiting
    for token expiry — otherwise "disabled" would mean "disabled in up to
    thirty minutes".
    """
    user = await _load_tenant_user(tenant, user_id)
    before = {"full_name": user.full_name, "is_active": user.is_active}

    if payload.full_name is not None:
        user.full_name = payload.full_name

    if payload.role is not None:
        role = (
            await tenant.session.execute(
                select(Role).where(Role.name == payload.role.value, Role.scope == RoleScope.TENANT)
            )
        ).scalar_one_or_none()
        if role is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"the role {payload.role.value!r} is not configured",
            )
        await tenant.session.execute(
            sql_delete(UserRole).where(
                UserRole.user_id == user_id, UserRole.tenant_id == tenant.tenant_id
            )
        )
        tenant.session.add(UserRole(user_id=user_id, role_id=role.id, tenant_id=tenant.tenant_id))

    if payload.is_active is not None and payload.is_active != user.is_active:
        if not payload.is_active and user.id == tenant.principal.user_id:
            # Locking yourself out leaves a tenant with no way back in if this
            # is its only administrator.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="you cannot disable your own account",
            )
        user.is_active = payload.is_active
        if not payload.is_active:
            user.tokens_valid_from = datetime.now(UTC)

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="user.updated",
        resource_type="user",
        resource_id=user.id,
        old_value=before,
        new_value={
            "full_name": user.full_name,
            "is_active": user.is_active,
            "role": payload.role.value if payload.role else None,
        },
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()

    roles = await _user_roles(tenant, [user.id])
    return UserResponse.model_validate(user, from_attributes=True).model_copy(
        update={
            "roles": sorted(roles.get(user.id, [])),
            "sessions_revoked": user.tokens_valid_from is not None,
        }
    )


@router.post(
    "/users/{user_id}/password",
    response_model=UserResponse,
    summary="Set a user's password and revoke their sessions",
    dependencies=[Depends(require_permission(Permission.USERS_MANAGE))],
)
async def reset_password(
    user_id: uuid.UUID,
    payload: PasswordReset,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> UserResponse:
    """Set a new password.

    Existing sessions are revoked as part of the same change: a password reset
    that leaves old tokens working is not a reset.
    """
    user = await _load_tenant_user(tenant, user_id)
    try:
        user.password_hash = hash_password(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    user.tokens_valid_from = datetime.now(UTC)

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="user.password_reset",
        resource_type="user",
        resource_id=user.id,
        new_value={"sessions_revoked": True},
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()

    roles = await _user_roles(tenant, [user.id])
    return UserResponse.model_validate(user, from_attributes=True).model_copy(
        update={"roles": sorted(roles.get(user.id, [])), "sessions_revoked": True}
    )


@router.delete(
    "/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a user",
    dependencies=[Depends(require_permission(Permission.USERS_MANAGE))],
)
async def delete_user(
    user_id: uuid.UUID, tenant: CurrentTenant, request: Request, client_ip: ClientIp
) -> Response:
    user = await _load_tenant_user(tenant, user_id)
    if user.id == tenant.principal.user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="you cannot delete your own account",
        )

    email = str(user.email)
    await tenant.session.execute(sql_delete(UserRole).where(UserRole.user_id == user_id))
    await tenant.session.execute(sql_delete(User).where(User.id == user_id))

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="user.deleted",
        resource_type="user",
        resource_id=user_id,
        old_value={"email": email},
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Tenant settings
# --------------------------------------------------------------------------- #


@router.get(
    "/settings",
    response_model=TenantSettingsResponse,
    summary="Read this tenant's settings",
    dependencies=[Depends(require_permission(Permission.AGENTS_READ))],
)
async def get_settings_endpoint(tenant: CurrentTenant) -> TenantSettingsResponse:
    row = (
        await tenant.session.execute(select(Tenant).where(Tenant.id == tenant.tenant_id))
    ).scalar_one()
    return TenantSettingsResponse.model_validate(row, from_attributes=True)


@router.put(
    "/settings",
    response_model=TenantSettingsResponse,
    summary="Update this tenant's settings",
    dependencies=[Depends(require_permission(Permission.USERS_MANAGE))],
)
async def update_settings(
    payload: TenantSettingsUpdate,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> TenantSettingsResponse:
    """Update settings a tenant owns.

    The call limits are absent by design: they are commercial terms the
    platform sets, and a tenant raising its own concurrency cap would make
    spec 47 meaningless.
    """
    row = (
        await tenant.session.execute(select(Tenant).where(Tenant.id == tenant.tenant_id))
    ).scalar_one()

    if payload.timezone:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(payload.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{payload.timezone!r} is not a known IANA timezone",
            ) from exc

    before = {
        "name": row.name,
        "timezone": row.timezone,
        "default_language": row.default_language,
    }
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field, value)

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="tenant.settings_updated",
        resource_type="tenant",
        resource_id=row.id,
        old_value=before,
        new_value={
            "name": row.name,
            "timezone": row.timezone,
            "default_language": row.default_language,
        },
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return TenantSettingsResponse.model_validate(row, from_attributes=True)


# --------------------------------------------------------------------------- #
# Analytics (spec 57)
# --------------------------------------------------------------------------- #


@router.get(
    "/analytics",
    response_model=AnalyticsResponse,
    summary="Business metrics over a window",
    dependencies=[Depends(require_permission(Permission.ANALYTICS_READ))],
)
async def analytics(
    tenant: CurrentTenant,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> AnalyticsResponse:
    """Counts and durations from the calls table.

    Deliberately excludes voice latency: those percentiles live in Prometheus,
    and computing them here would produce a second number that disagrees with
    the Grafana dashboard.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    repository = TenantRepository(tenant.session, tenant.tenant_id)

    base = repository.scoped(Call).where(Call.start_time >= since)

    rows = list((await tenant.session.execute(base)).scalars().all())

    total = len(rows)
    answered = sum(1 for r in rows if r.state in _ANSWERED_STATES)
    failed = sum(1 for r in rows if r.state in _FAILED_STATES)
    transferred = sum(1 for r in rows if r.transfer_status is TransferStatus.BRIDGED)
    total_seconds = sum(r.duration_seconds or 0 for r in rows)
    with_duration = [r.duration_seconds for r in rows if r.duration_seconds]

    by_state: dict[str, int] = {}
    for row in rows:
        by_state[row.state.value] = by_state.get(row.state.value, 0) + 1

    agent_ids = {r.agent_id for r in rows if r.agent_id}
    agent_names = (
        as_lookup(
            (
                await tenant.session.execute(
                    select(Agent.id, Agent.name).where(
                        Agent.tenant_id == tenant.tenant_id, Agent.id.in_(agent_ids)
                    )
                )
            ).all()
        )
        if agent_ids
        else {}
    )

    per_agent: dict[str, dict] = {}
    for row in rows:
        key = agent_names.get(row.agent_id, "unassigned") if row.agent_id else "unassigned"
        entry = per_agent.setdefault(key, {"agent": key, "calls": 0, "seconds": 0})
        entry["calls"] += 1
        entry["seconds"] += row.duration_seconds or 0

    per_day: dict[str, dict] = {}
    for row in rows:
        if row.start_time is None:
            continue
        key = row.start_time.date().isoformat()
        entry = per_day.setdefault(key, {"date": key, "calls": 0, "answered": 0, "failed": 0})
        entry["calls"] += 1
        if row.state in _ANSWERED_STATES:
            entry["answered"] += 1
        if row.state in _FAILED_STATES:
            entry["failed"] += 1

    return AnalyticsResponse(
        window_days=days,
        total_calls=total,
        answered_calls=answered,
        failed_calls=failed,
        transferred_calls=transferred,
        total_seconds=total_seconds,
        average_duration_seconds=(sum(with_duration) / len(with_duration))
        if with_duration
        else None,
        answer_rate=(answered / total) if total else None,
        by_state=by_state,
        by_agent=sorted(per_agent.values(), key=lambda e: -e["calls"]),
        by_day=sorted(per_day.values(), key=lambda e: e["date"]),
    )


# --------------------------------------------------------------------------- #
# Usage (spec 47)
# --------------------------------------------------------------------------- #


@router.get(
    "/usage",
    response_model=UsageSummary,
    summary="Usage against this tenant's limits",
    dependencies=[Depends(require_permission(Permission.BILLING_READ))],
)
async def usage(
    tenant: CurrentTenant,
    days: Annotated[int, Query(ge=1, le=180)] = 30,
) -> UsageSummary:
    """Standing against each limit.

    Reports which limits are actually enforced rather than implying all three
    are. ``max_concurrent_calls`` is checked before a call is accepted; the
    daily and monthly limits read the ``usage`` rollup, which nothing writes
    yet, so they are reported as unenforced (Plan 1b.1).
    """
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = (
        await tenant.session.execute(select(Tenant).where(Tenant.id == tenant.tenant_id))
    ).scalar_one()

    today = datetime.now(UTC).date()
    month_start = today.replace(day=1)

    calls_today = int(
        (
            await tenant.session.execute(
                select(func.count())
                .select_from(Call)
                .where(
                    Call.tenant_id == tenant.tenant_id,
                    func.date(Call.start_time) == today,
                )
            )
        ).scalar_one()
    )

    seconds_this_month = int(
        (
            await tenant.session.execute(
                select(func.coalesce(func.sum(Call.duration_seconds), 0)).where(
                    Call.tenant_id == tenant.tenant_id,
                    func.date(Call.start_time) >= month_start,
                )
            )
        ).scalar_one()
        or 0
    )

    active = int(
        (
            await tenant.session.execute(
                select(func.count())
                .select_from(Call)
                .where(Call.tenant_id == tenant.tenant_id, Call.state.in_(_LIVE_STATES))
            )
        ).scalar_one()
    )

    since = today - timedelta(days=days)
    daily = list(
        (
            await tenant.session.execute(
                repository.scoped(Usage)
                .where(Usage.usage_date >= since)
                .order_by(Usage.usage_date.desc())
            )
        )
        .scalars()
        .all()
    )

    return UsageSummary(
        max_concurrent_calls=row.max_concurrent_calls,
        max_daily_calls=row.max_daily_calls,
        max_monthly_minutes=row.max_monthly_minutes,
        calls_today=calls_today,
        minutes_this_month=seconds_this_month // 60,
        active_calls=active,
        days=[UsageResponse.model_validate(d, from_attributes=True) for d in daily],
    )


# --------------------------------------------------------------------------- #
# Recordings (spec 39)
# --------------------------------------------------------------------------- #


@router.get(
    "/recordings",
    response_model=Page[RecordingResponse],
    summary="List call recordings",
    dependencies=[Depends(require_permission(Permission.RECORDINGS_READ))],
)
async def list_recordings(
    tenant: CurrentTenant,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[RecordingResponse]:
    """Recording metadata.

    Metadata only: the audio lives in object storage and PostgreSQL holds a
    pointer (spec 3, 39). Empty until the worker starts egress, which is
    Phase 2b.3 — the endpoint exists so the UI can say that rather than 404.
    """
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    rows = list(
        (
            await tenant.session.execute(
                repository.scoped(CallRecording)
                .order_by(CallRecording.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    total = await repository.count(CallRecording)

    return Page(
        items=[RecordingResponse.model_validate(r, from_attributes=True) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
