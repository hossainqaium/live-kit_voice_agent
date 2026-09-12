"""Platform console endpoints (spec 60).

Every route here requires a platform user, and the tenant-affecting ones
require SUPER_ADMIN. The distinction matters: reading capacity is an operator
task, whereas creating a tenant or retiring a provider changes what the
platform will accept, and spec 8 puts that behind the highest role.

Nothing in this module goes through ``TenantRepository``, because there is no
single tenant to scope to. That is the one legitimate reason to query without a
tenant filter, so each such query says why in a comment.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import Select, func, select, text
from sqlalchemy import delete as sql_delete
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.dependencies import (
    ClientIp,
    CurrentPrincipal,
    require_platform_user,
    require_super_admin,
)
from app.core.security import hash_password
from app.core.settings import get_settings
from app.db.models import (
    Agent,
    AuditLog,
    Call,
    LiveKitDispatchRule,
    Model,
    Pbx,
    PhoneNumber,
    Provider,
    ProviderCredential,
    Role,
    SipTrunk,
    Tenant,
    User,
    UserRole,
    Voice,
)
from app.db.session import get_session
from app.db.util import as_lookup
from app.livekit import LiveKitAdminClient, RoomSnapshot
from app.livekit.drift import DriftReport, detect_drift, last_report
from app.livekit.jobs import enqueue_dispatch_rule_sync, enqueue_trunk_sync
from app.livekit.sip import SipResourceManager
from app.services.fleet import collect_fleet
from app.schemas.common import Page
from app.schemas.platform import (
    AuditLogResponse,
    CapacityResponse,
    ComponentHealth,
    DriftCheckResponse,
    DriftedResource,
    LiveKitAdminSection,
    LiveKitOverview,
    LiveKitRoom,
    LiveKitSyncActionResponse,
    ModelCreate,
    ModelResponse,
    ModelUpdate,
    OrphanedResource,
    PlatformSettingsResponse,
    ProviderHealthStatus,
    ResourceUsage,
    PlatformUserCreate,
    PlatformUserResponse,
    PlatformUserUpdate,
    ProviderCreate,
    ProviderResponse,
    ProviderUpdate,
    SettingEntry,
    TenantCreate,
    TenantSummary,
    TenantUpdate,
    VoiceCreate,
    VoiceResponse,
    VoiceTestRequest,
    VoiceTestResponse,
    VoiceUpdate,
)
from app.services import audit, voice_preview
from shared.logging import get_logger
from shared.models import (
    CallState,
    PlatformRole,
    ProviderKind,
    ResourceStatus,
    RoleScope,
    SyncStatus,
    TenantRole,
    TenantStatus,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/platform", tags=["platform"])

#: Calls that are still using a concurrency slot.
_LIVE_STATES = (
    CallState.NEW,
    CallState.RINGING,
    CallState.ANSWERED,
    CallState.AI_CONNECTED,
    CallState.IN_PROGRESS,
    CallState.TRANSFERRING,
    CallState.HUMAN_AGENT,
)

#: Platform endpoints take a bare session: there is no single tenant to
#: scope to, which is the one legitimate reason not to use TenantRepository.
SessionDep = Annotated[AsyncSession, Depends(get_session)]


# --------------------------------------------------------------------------- #
# Tenants
# --------------------------------------------------------------------------- #


async def _tenant_counts(
    session: AsyncSession, tenant_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, int]]:
    """Per-tenant counts for the platform list view.

    One grouped query per table rather than a per-tenant loop: the platform
    list is the only place that legitimately reads across tenants, and N+1 here
    would scale with the customer base.
    """
    counts: dict[uuid.UUID, dict[str, int]] = {tid: {} for tid in tenant_ids}
    if not tenant_ids:
        return counts

    since = datetime.now(UTC) - timedelta(days=30)

    # ``Any`` for the first column: ``users.tenant_id`` is nullable and the
    # rest are not, and Select is invariant, so a precise union would reject
    # the non-null queries.
    queries: list[tuple[str, Select[tuple[Any, int]]]] = [
        (
            "user_count",
            select(User.tenant_id, func.count())
            .where(User.tenant_id.in_(tenant_ids))
            .group_by(User.tenant_id),
        ),
        (
            "agent_count",
            select(Agent.tenant_id, func.count())
            .where(Agent.tenant_id.in_(tenant_ids))
            .group_by(Agent.tenant_id),
        ),
        (
            "pbx_count",
            select(Pbx.tenant_id, func.count())
            .where(Pbx.tenant_id.in_(tenant_ids))
            .group_by(Pbx.tenant_id),
        ),
        (
            "phone_number_count",
            select(PhoneNumber.tenant_id, func.count())
            .where(PhoneNumber.tenant_id.in_(tenant_ids))
            .group_by(PhoneNumber.tenant_id),
        ),
        (
            "calls_last_30_days",
            select(Call.tenant_id, func.count())
            .where(Call.tenant_id.in_(tenant_ids), Call.start_time >= since)
            .group_by(Call.tenant_id),
        ),
        (
            "active_calls",
            select(Call.tenant_id, func.count())
            .where(Call.tenant_id.in_(tenant_ids), Call.state.in_(_LIVE_STATES))
            .group_by(Call.tenant_id),
        ),
    ]

    for field, query in queries:
        for tenant_id, value in (await session.execute(query)).all():
            if tenant_id in counts:
                counts[tenant_id][field] = int(value)

    return counts


@router.get(
    "/tenants",
    response_model=Page[TenantSummary],
    summary="List every tenant",
    dependencies=[Depends(require_platform_user())],
)
async def list_tenants(
    session: SessionDep,
    q: Annotated[str | None, Query(max_length=100)] = None,
    tenant_status: Annotated[TenantStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[TenantSummary]:
    """List tenants.

    Unscoped by design: ``tenants`` is a platform table with no tenant
    dimension, and this is the platform console.
    """
    query = select(Tenant)
    count_query = select(func.count()).select_from(Tenant)

    if q:
        pattern = f"%{q.strip()}%"
        condition = Tenant.name.ilike(pattern) | Tenant.slug.ilike(pattern)
        query = query.where(condition)
        count_query = count_query.where(condition)
    if tenant_status is not None:
        query = query.where(Tenant.status == tenant_status)
        count_query = count_query.where(Tenant.status == tenant_status)

    rows = list(
        (await session.execute(query.order_by(Tenant.name).limit(limit).offset(offset)))
        .scalars()
        .all()
    )
    total = int((await session.execute(count_query)).scalar_one())
    counts = await _tenant_counts(session, [r.id for r in rows])

    return Page(
        items=[
            TenantSummary.model_validate(row, from_attributes=True).model_copy(
                update=counts.get(row.id, {})
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/tenants",
    response_model=TenantSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Create a tenant",
    dependencies=[Depends(require_super_admin())],
)
async def create_tenant(
    payload: TenantCreate,
    session: SessionDep,
    principal: CurrentPrincipal,
    request: Request,
    client_ip: ClientIp,
) -> TenantSummary:
    """Create a tenant, optionally with its first administrator.

    The administrator is created in the same transaction as the tenant. A
    tenant that exists with nobody able to sign in is a support ticket waiting
    to happen, and doing it in two requests means the second one sometimes
    never arrives.
    """
    clash = (
        await session.execute(select(Tenant.id).where(Tenant.slug == payload.slug))
    ).scalar_one_or_none()
    if clash is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"the slug {payload.slug!r} is already taken",
        )

    admin_fields = (payload.admin_email, payload.admin_full_name, payload.admin_password)
    if any(admin_fields) and not all(admin_fields):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "to create the first administrator, supply admin_email, "
                "admin_full_name and admin_password together"
            ),
        )

    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(payload.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{payload.timezone!r} is not a known IANA timezone",
        ) from exc

    tenant = Tenant(
        name=payload.name,
        slug=payload.slug,
        status=TenantStatus.ACTIVE,
        timezone=payload.timezone,
        default_language=payload.default_language,
        max_concurrent_calls=payload.max_concurrent_calls,
        max_daily_calls=payload.max_daily_calls,
        max_monthly_minutes=payload.max_monthly_minutes,
        notes=payload.notes,
    )
    session.add(tenant)
    await session.flush()

    admin_email: str | None = None
    if payload.admin_email:
        email = payload.admin_email.strip().lower()
        taken = (
            await session.execute(select(User.id).where(User.email == email))
        ).scalar_one_or_none()
        if taken is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="an account with that email already exists",
            )

        role = (
            await session.execute(
                select(Role).where(
                    Role.name == TenantRole.TENANT_ADMIN.value, Role.scope == RoleScope.TENANT
                )
            )
        ).scalar_one_or_none()
        if role is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="the TENANT_ADMIN role is not configured; run seed-rbac first",
            )

        try:
            password_hash = hash_password(payload.admin_password or "")
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        user = User(
            tenant_id=tenant.id,
            is_platform_user=False,
            email=email,
            full_name=payload.admin_full_name or email,
            password_hash=password_hash,
        )
        session.add(user)
        await session.flush()
        session.add(UserRole(user_id=user.id, role_id=role.id, tenant_id=tenant.id))
        admin_email = email

    await audit.record(
        session,
        principal=principal,
        action="tenant.created",
        resource_type="tenant",
        resource_id=tenant.id,
        tenant_id=tenant.id,
        new_value={
            "name": tenant.name,
            "slug": tenant.slug,
            "max_concurrent_calls": tenant.max_concurrent_calls,
            "admin_email": admin_email,
        },
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()

    logger.info("tenant_created", extra={"tenant_slug": tenant.slug})
    return TenantSummary.model_validate(tenant, from_attributes=True).model_copy(
        update={"admin_email": admin_email, "user_count": 1 if admin_email else 0}
    )


async def _load_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    row = (await session.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no tenant with id {tenant_id}")
    return row


@router.get(
    "/tenants/{tenant_id}",
    response_model=TenantSummary,
    summary="Read one tenant",
    dependencies=[Depends(require_platform_user())],
)
async def get_tenant(tenant_id: uuid.UUID, session: SessionDep) -> TenantSummary:
    row = await _load_tenant(session, tenant_id)
    counts = await _tenant_counts(session, [row.id])
    return TenantSummary.model_validate(row, from_attributes=True).model_copy(
        update=counts.get(row.id, {})
    )


@router.put(
    "/tenants/{tenant_id}",
    response_model=TenantSummary,
    summary="Update a tenant",
    dependencies=[Depends(require_super_admin())],
)
async def update_tenant(
    tenant_id: uuid.UUID,
    payload: TenantUpdate,
    session: SessionDep,
    principal: CurrentPrincipal,
    request: Request,
    client_ip: ClientIp,
) -> TenantSummary:
    """Update a tenant's details, limits or status.

    Suspending a tenant revokes its users' sessions. Without that, a suspended
    tenant keeps working for up to the access-token lifetime, which makes
    "suspended" mean "suspended soon".
    """
    row = await _load_tenant(session, tenant_id)
    before = audit.snapshot(
        row,
        "name",
        "status",
        "timezone",
        "default_language",
        "max_concurrent_calls",
        "max_daily_calls",
        "max_monthly_minutes",
    )

    if payload.timezone:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(payload.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{payload.timezone!r} is not a known IANA timezone",
            ) from exc

    changes = payload.model_dump(exclude_unset=True)
    suspending = (
        changes.get("status") in (TenantStatus.SUSPENDED, TenantStatus.ARCHIVED)
        and row.status is TenantStatus.ACTIVE
    )

    for field, value in changes.items():
        setattr(row, field, value)

    if suspending:
        await session.execute(
            sql_update(User)
            .where(User.tenant_id == tenant_id)
            .values(tokens_valid_from=datetime.now(UTC))
        )

    await audit.record(
        session,
        principal=principal,
        action="tenant.updated",
        resource_type="tenant",
        resource_id=row.id,
        tenant_id=row.id,
        old_value=before,
        new_value={
            **{k: (v.value if hasattr(v, "value") else v) for k, v in changes.items()},
            "sessions_revoked": suspending,
        },
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()

    counts = await _tenant_counts(session, [row.id])
    return TenantSummary.model_validate(row, from_attributes=True).model_copy(
        update=counts.get(row.id, {})
    )


# --------------------------------------------------------------------------- #
# Provider catalog (spec 24, 25)
# --------------------------------------------------------------------------- #


@router.get(
    "/providers",
    response_model=Page[ProviderResponse],
    summary="List the AI provider catalog",
    dependencies=[Depends(require_platform_user())],
)
async def list_providers(
    session: SessionDep,
    kind: Annotated[ProviderKind | None, Query()] = None,
) -> Page[ProviderResponse]:
    """The provider catalog with usage counts.

    ``credential_count`` counts across tenants, which is the point: it is what
    tells an operator that disabling a provider would break live tenants. The
    keys themselves are never read here.
    """
    query = select(Provider).order_by(Provider.kind, Provider.display_name)
    if kind is not None:
        query = query.where(Provider.kind == kind)

    rows = list((await session.execute(query)).scalars().all())
    ids = [r.id for r in rows]

    models = (
        as_lookup(
            (
                await session.execute(
                    select(Model.provider_id, func.count())
                    .where(Model.provider_id.in_(ids))
                    .group_by(Model.provider_id)
                )
            ).all()
        )
        if ids
        else {}
    )
    voices = (
        as_lookup(
            (
                await session.execute(
                    select(Voice.provider_id, func.count())
                    .where(Voice.provider_id.in_(ids))
                    .group_by(Voice.provider_id)
                )
            ).all()
        )
        if ids
        else {}
    )
    credentials = (
        as_lookup(
            (
                await session.execute(
                    select(ProviderCredential.provider_id, func.count())
                    .where(ProviderCredential.provider_id.in_(ids))
                    .group_by(ProviderCredential.provider_id)
                )
            ).all()
        )
        if ids
        else {}
    )

    return Page(
        items=[
            ProviderResponse.model_validate(row, from_attributes=True).model_copy(
                update={
                    "model_count": int(models.get(row.id, 0)),
                    "voice_count": int(voices.get(row.id, 0)),
                    "credential_count": int(credentials.get(row.id, 0)),
                }
            )
            for row in rows
        ],
        total=len(rows),
        limit=len(rows),
        offset=0,
    )


@router.post(
    "/providers",
    response_model=ProviderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a provider to the catalog",
    dependencies=[Depends(require_super_admin())],
)
async def create_provider(
    payload: ProviderCreate,
    session: SessionDep,
    principal: CurrentPrincipal,
    request: Request,
    client_ip: ClientIp,
) -> ProviderResponse:
    """Add a provider.

    The slug is unique per kind, not globally: one vendor can supply STT and
    TTS, and forcing ``openai-stt``/``openai-tts`` would make the agent
    configuration read worse for no benefit.
    """
    clash = (
        await session.execute(
            select(Provider.id).where(Provider.slug == payload.slug, Provider.kind == payload.kind)
        )
    ).scalar_one_or_none()
    if clash is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a {payload.kind.value} provider with slug {payload.slug!r} already exists",
        )

    row = Provider(**payload.model_dump())
    session.add(row)
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="provider.created",
        resource_type="provider",
        resource_id=row.id,
        new_value={"kind": row.kind.value, "slug": row.slug},
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
    return ProviderResponse.model_validate(row, from_attributes=True)


@router.put(
    "/providers/{provider_id}",
    response_model=ProviderResponse,
    summary="Update a provider",
    dependencies=[Depends(require_super_admin())],
)
async def update_provider(
    provider_id: uuid.UUID,
    payload: ProviderUpdate,
    session: SessionDep,
    principal: CurrentPrincipal,
    request: Request,
    client_ip: ClientIp,
) -> ProviderResponse:
    """Update a provider.

    Disabling one that tenants still hold credentials for is refused rather
    than allowed silently: the failure would otherwise surface as calls that
    stop working, with the cause several screens away.
    """
    row = (
        await session.execute(select(Provider).where(Provider.id == provider_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no provider with id {provider_id}")

    before = audit.snapshot(row, "display_name", "status", "requires_credential")
    changes = payload.model_dump(exclude_unset=True)

    new_status = changes.get("status")
    if new_status is not None and new_status is not ResourceStatus.ACTIVE:
        in_use = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(ProviderCredential)
                    .where(ProviderCredential.provider_id == provider_id)
                )
            ).scalar_one()
        )
        if in_use:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"{in_use} tenant credential(s) still reference this provider; "
                    "remove them before disabling it"
                ),
            )

    for field, value in changes.items():
        setattr(row, field, value)

    await audit.record(
        session,
        principal=principal,
        action="provider.updated",
        resource_type="provider",
        resource_id=row.id,
        old_value=before,
        new_value={k: (v.value if hasattr(v, "value") else v) for k, v in changes.items()},
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
    return ProviderResponse.model_validate(row, from_attributes=True)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


@router.get(
    "/models",
    response_model=Page[ModelResponse],
    summary="List catalog models",
    dependencies=[Depends(require_platform_user())],
)
async def list_models(
    session: SessionDep,
    provider_id: Annotated[uuid.UUID | None, Query()] = None,
) -> Page[ModelResponse]:
    query = (
        select(Model, Provider.slug, Provider.kind)
        .join(Provider, Provider.id == Model.provider_id)
        .order_by(Provider.kind, Provider.slug, Model.display_name)
    )
    if provider_id is not None:
        query = query.where(Model.provider_id == provider_id)

    rows = (await session.execute(query)).all()
    items = [
        ModelResponse.model_validate(model, from_attributes=True).model_copy(
            update={"provider_slug": slug, "provider_kind": kind}
        )
        for model, slug, kind in rows
    ]
    return Page(items=items, total=len(items), limit=len(items), offset=0)


@router.post(
    "/models",
    response_model=ModelResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a model",
    dependencies=[Depends(require_super_admin())],
)
async def create_model(
    payload: ModelCreate,
    session: SessionDep,
    principal: CurrentPrincipal,
    request: Request,
    client_ip: ClientIp,
) -> ModelResponse:
    """Add a model to a provider.

    Setting it as default clears the previous default for the same provider in
    the same transaction, so the catalog cannot hold two defaults — a state the
    agent validator would have to break a tie in arbitrarily.
    """
    provider = (
        await session.execute(select(Provider).where(Provider.id == payload.provider_id))
    ).scalar_one_or_none()
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"no provider with id {payload.provider_id}",
        )

    clash = (
        await session.execute(
            select(Model.id).where(
                Model.provider_id == payload.provider_id, Model.slug == payload.slug
            )
        )
    ).scalar_one_or_none()
    if clash is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{provider.slug} already has a model with slug {payload.slug!r}",
        )

    if payload.is_default:
        await session.execute(
            sql_update(Model)
            .where(Model.provider_id == payload.provider_id, Model.is_default.is_(True))
            .values(is_default=False)
        )

    row = Model(**payload.model_dump())
    session.add(row)
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="model.created",
        resource_type="model",
        resource_id=row.id,
        new_value={"provider": provider.slug, "slug": row.slug, "is_default": row.is_default},
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()

    return ModelResponse.model_validate(row, from_attributes=True).model_copy(
        update={"provider_slug": provider.slug, "provider_kind": provider.kind}
    )


@router.put(
    "/models/{model_id}",
    response_model=ModelResponse,
    summary="Update a model",
    dependencies=[Depends(require_super_admin())],
)
async def update_model(
    model_id: uuid.UUID,
    payload: ModelUpdate,
    session: SessionDep,
    principal: CurrentPrincipal,
    request: Request,
    client_ip: ClientIp,
) -> ModelResponse:
    found = (
        await session.execute(
            select(Model, Provider.slug, Provider.kind)
            .join(Provider, Provider.id == Model.provider_id)
            .where(Model.id == model_id)
        )
    ).one_or_none()
    if found is None:
        raise HTTPException(status_code=404, detail=f"no model with id {model_id}")
    row, provider_slug, provider_kind = found

    before = audit.snapshot(row, "display_name", "status", "is_default", "languages")
    changes = payload.model_dump(exclude_unset=True)

    if changes.get("is_default"):
        await session.execute(
            sql_update(Model)
            .where(
                Model.provider_id == row.provider_id,
                Model.id != row.id,
                Model.is_default.is_(True),
            )
            .values(is_default=False)
        )

    for field, value in changes.items():
        setattr(row, field, value)

    await audit.record(
        session,
        principal=principal,
        action="model.updated",
        resource_type="model",
        resource_id=row.id,
        old_value=before,
        new_value={k: (v.value if hasattr(v, "value") else v) for k, v in changes.items()},
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()

    return ModelResponse.model_validate(row, from_attributes=True).model_copy(
        update={"provider_slug": provider_slug, "provider_kind": provider_kind}
    )


@router.delete(
    "/models/{model_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Remove a model",
    dependencies=[Depends(require_super_admin())],
)
async def delete_model(
    model_id: uuid.UUID,
    session: SessionDep,
    principal: CurrentPrincipal,
    request: Request,
    client_ip: ClientIp,
) -> Response:
    """Remove a model from the catalog.

    Refused while an agent version still names it. Agent versions are
    immutable once published (spec 19), so deleting the model out from under
    one would leave a published version that can never run again.
    """
    row = (await session.execute(select(Model).where(Model.id == model_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no model with id {model_id}")

    # Cross-tenant on purpose: a platform deletion must consider every tenant's
    # agents, not just one.
    in_use = int(
        (
            await session.execute(
                text(
                    "SELECT count(*) FROM agent_versions "
                    "WHERE stt_model_id = :id OR llm_model_id = :id OR tts_model_id = :id"
                ),
                {"id": str(model_id)},
            )
        ).scalar_one()
    )
    if in_use:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{in_use} agent version(s) reference this model; disable it instead",
        )

    await session.execute(sql_delete(Model).where(Model.id == model_id))
    await audit.record(
        session,
        principal=principal,
        action="model.deleted",
        resource_type="model",
        resource_id=model_id,
        old_value={"slug": row.slug},
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Voices (spec 30)
# --------------------------------------------------------------------------- #


@router.get(
    "/voices",
    response_model=Page[VoiceResponse],
    summary="List catalog voices",
    dependencies=[Depends(require_platform_user())],
)
async def list_voices(
    session: SessionDep,
    provider_id: Annotated[uuid.UUID | None, Query()] = None,
    language: Annotated[str | None, Query(max_length=16)] = None,
) -> Page[VoiceResponse]:
    query = (
        select(Voice, Provider.slug)
        .join(Provider, Provider.id == Voice.provider_id)
        .order_by(Provider.slug, Voice.name)
    )
    if provider_id is not None:
        query = query.where(Voice.provider_id == provider_id)
    if language:
        query = query.where(Voice.language == language)

    rows = (await session.execute(query)).all()
    items = [
        VoiceResponse.model_validate(voice, from_attributes=True).model_copy(
            update={"provider_slug": slug}
        )
        for voice, slug in rows
    ]
    return Page(items=items, total=len(items), limit=len(items), offset=0)


@router.post(
    "/voices",
    response_model=VoiceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a voice",
    dependencies=[Depends(require_super_admin())],
)
async def create_voice(
    payload: VoiceCreate,
    session: SessionDep,
    principal: CurrentPrincipal,
    request: Request,
    client_ip: ClientIp,
) -> VoiceResponse:
    provider = (
        await session.execute(select(Provider).where(Provider.id == payload.provider_id))
    ).scalar_one_or_none()
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"no provider with id {payload.provider_id}",
        )
    if provider.kind is not ProviderKind.TTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{provider.slug} is a {provider.kind.value} provider, so it has no voices",
        )

    clash = (
        await session.execute(
            select(Voice.id).where(
                Voice.provider_id == payload.provider_id, Voice.voice_id == payload.voice_id
            )
        )
    ).scalar_one_or_none()
    if clash is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{provider.slug} already has voice {payload.voice_id!r}",
        )

    if payload.is_default:
        await session.execute(
            sql_update(Voice)
            .where(Voice.provider_id == payload.provider_id, Voice.is_default.is_(True))
            .values(is_default=False)
        )

    row = Voice(**payload.model_dump())
    session.add(row)
    await session.flush()

    await audit.record(
        session,
        principal=principal,
        action="voice.created",
        resource_type="voice",
        resource_id=row.id,
        new_value={"provider": provider.slug, "voice_id": row.voice_id, "name": row.name},
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()

    return VoiceResponse.model_validate(row, from_attributes=True).model_copy(
        update={"provider_slug": provider.slug}
    )


@router.put(
    "/voices/{voice_id}",
    response_model=VoiceResponse,
    summary="Update a voice",
    dependencies=[Depends(require_super_admin())],
)
async def update_voice(
    voice_id: uuid.UUID,
    payload: VoiceUpdate,
    session: SessionDep,
    principal: CurrentPrincipal,
    request: Request,
    client_ip: ClientIp,
) -> VoiceResponse:
    found = (
        await session.execute(
            select(Voice, Provider.slug)
            .join(Provider, Provider.id == Voice.provider_id)
            .where(Voice.id == voice_id)
        )
    ).one_or_none()
    if found is None:
        raise HTTPException(status_code=404, detail=f"no voice with id {voice_id}")
    row, provider_slug = found

    before = audit.snapshot(row, "name", "language", "status", "is_default")
    changes = payload.model_dump(exclude_unset=True)

    if changes.get("is_default"):
        await session.execute(
            sql_update(Voice)
            .where(
                Voice.provider_id == row.provider_id,
                Voice.id != row.id,
                Voice.is_default.is_(True),
            )
            .values(is_default=False)
        )

    for field, value in changes.items():
        setattr(row, field, value)

    await audit.record(
        session,
        principal=principal,
        action="voice.updated",
        resource_type="voice",
        resource_id=row.id,
        old_value=before,
        new_value={k: (v.value if hasattr(v, "value") else v) for k, v in changes.items()},
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()

    return VoiceResponse.model_validate(row, from_attributes=True).model_copy(
        update={"provider_slug": provider_slug}
    )


@router.post(
    "/voices/{voice_id}/test",
    response_model=VoiceTestResponse,
    summary="Synthesise a preview sample and store it",
    dependencies=[Depends(require_super_admin())],
)
async def test_voice(
    voice_id: uuid.UUID,
    payload: VoiceTestRequest,
    session: SessionDep,
    principal: CurrentPrincipal,
    request: Request,
    client_ip: ClientIp,
) -> VoiceTestResponse:
    """Speak a short phrase with this catalog voice (spec 27, 4b.5).

    The audio is written to object storage and ``sample_object_key`` is set
    so the next Test dialog can replay it without calling the provider again.
    Playback goes through ``GET .../sample`` so the browser never needs a
    MinIO URL.
    """
    found = (
        await session.execute(
            select(Voice, Provider)
            .join(Provider, Provider.id == Voice.provider_id)
            .where(Voice.id == voice_id)
        )
    ).one_or_none()
    if found is None:
        raise HTTPException(status_code=404, detail=f"no voice with id {voice_id}")
    row, provider = found

    if provider.requires_credential and not payload.api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "this provider requires an API key for preview; "
                "the key is used once and is not stored"
            ),
        )

    model_slug = payload.model
    if not model_slug:
        model_row = (
            await session.execute(
                select(Model)
                .where(
                    Model.provider_id == provider.id,
                    Model.status == ResourceStatus.ACTIVE,
                )
                .order_by(Model.is_default.desc(), Model.slug)
            )
        ).scalars().first()
        model_slug = model_row.slug if model_row is not None else None

    text = (payload.text or voice_preview.DEFAULT_PREVIEW_TEXT).strip()
    try:
        audio, content_type = await voice_preview.synthesize(
            adapter=provider.adapter_key,
            provider_voice_id=row.voice_id,
            text=text,
            api_key=payload.api_key,
            base_url=provider.default_base_url,
            model=model_slug,
        )
        key = voice_preview.sample_object_key(row.id)
        await voice_preview.store_sample(key, audio, content_type)
    except voice_preview.PreviewError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    row.sample_object_key = key
    await audit.record(
        session,
        principal=principal,
        action="voice.previewed",
        resource_type="voice",
        resource_id=row.id,
        new_value={
            "sample_object_key": key,
            "bytes": len(audio),
            "content_type": content_type,
        },
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()

    return VoiceTestResponse(
        sample_object_key=key, content_type=content_type, bytes=len(audio)
    )


@router.get(
    "/voices/{voice_id}/sample",
    summary="Stream the stored preview sample",
    dependencies=[Depends(require_platform_user())],
)
async def get_voice_sample(
    voice_id: uuid.UUID,
    session: SessionDep,
) -> StreamingResponse:
    """Return the last stored preview so the console can play it.

    Streamed through this API because a MinIO presigned URL would point at
    ``minio:9000``, which a browser cannot reach.
    """
    row = (
        await session.execute(select(Voice).where(Voice.id == voice_id))
    ).scalar_one_or_none()
    if row is None or not row.sample_object_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no stored sample for this voice — run Test first",
        )
    try:
        audio, content_type = await voice_preview.fetch_sample(row.sample_object_key)
    except voice_preview.PreviewError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return StreamingResponse(
        iter([audio]),
        media_type=content_type,
        headers={"Content-Disposition": f'inline; filename="{voice_id}.mp3"'},
    )


# --------------------------------------------------------------------------- #
# Audit log (spec 69)
# --------------------------------------------------------------------------- #


@router.get(
    "/audit-logs",
    response_model=Page[AuditLogResponse],
    summary="Read the audit trail",
    dependencies=[Depends(require_platform_user())],
)
async def list_audit_logs(
    session: SessionDep,
    # Named ``for_tenant`` rather than ``tenant_id`` on purpose: the
    # router-wide guard from spec 7 refuses a client-supplied ``tenant_id``
    # anywhere, and that guard is right to be unconditional. This is a
    # platform-only filter over a log that spans tenants, not a scoping input,
    # so it gets a name that cannot be mistaken for one.
    for_tenant: Annotated[uuid.UUID | None, Query()] = None,
    action: Annotated[str | None, Query(max_length=64)] = None,
    resource_type: Annotated[str | None, Query(max_length=64)] = None,
    user_email: Annotated[str | None, Query(max_length=320)] = None,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[AuditLogResponse]:
    """The audit trail, newest first.

    Read-only with no write or delete route anywhere in the API: spec 69 makes
    the log append-only, and an endpoint that could edit it would defeat the
    point of having it.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    conditions = [AuditLog.occurred_at >= since]

    if for_tenant is not None:
        conditions.append(AuditLog.tenant_id == for_tenant)
    if action:
        conditions.append(AuditLog.action.ilike(f"%{action.strip()}%"))
    if resource_type:
        conditions.append(AuditLog.resource_type == resource_type.strip())
    if user_email:
        conditions.append(AuditLog.user_email.ilike(f"%{user_email.strip()}%"))

    rows = list(
        (
            await session.execute(
                select(AuditLog)
                .where(*conditions)
                .order_by(AuditLog.occurred_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    total = int(
        (
            await session.execute(select(func.count()).select_from(AuditLog).where(*conditions))
        ).scalar_one()
    )

    tenant_ids = {r.tenant_id for r in rows if r.tenant_id}
    names = (
        as_lookup(
            (
                await session.execute(
                    select(Tenant.id, Tenant.name).where(Tenant.id.in_(tenant_ids))
                )
            ).all()
        )
        if tenant_ids
        else {}
    )

    return Page(
        items=[
            AuditLogResponse.model_validate(row, from_attributes=True).model_copy(
                update={"tenant_name": names.get(row.tenant_id) if row.tenant_id else None}
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


# --------------------------------------------------------------------------- #
# Capacity and infrastructure (spec 48)
# --------------------------------------------------------------------------- #


async def _probe_postgres(session: AsyncSession) -> ComponentHealth:
    started = time.perf_counter()
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        return ComponentHealth(name="postgresql", reachable=False, detail=str(exc)[:200])
    return ComponentHealth(
        name="postgresql",
        reachable=True,
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
    )


async def _probe_livekit() -> tuple[ComponentHealth, list[RoomSnapshot]]:
    """Probe LiveKit and list rooms in the same call.

    Room count is the closest thing LiveKit will tell us to live concurrency.
    Listing once serves both the count and the spec-11 Rooms / Participants
    section — a second call would only add latency.
    """
    client = LiveKitAdminClient()
    started = time.perf_counter()
    try:
        rooms = await client.list_rooms()
    except Exception as exc:
        return (
            ComponentHealth(name="livekit", reachable=False, detail=str(exc)[:200]),
            [],
        )
    return (
        ComponentHealth(
            name="livekit",
            reachable=True,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            detail=f"{len(rooms)} room(s)",
        ),
        rooms,
    )


async def _probe_redis() -> ComponentHealth:
    settings = get_settings()
    started = time.perf_counter()
    try:
        import redis.asyncio as redis_asyncio

        client = redis_asyncio.from_url(settings.redis_url)
        try:
            await client.ping()
        finally:
            await client.aclose()
    except Exception as exc:
        return ComponentHealth(name="redis", reachable=False, detail=str(exc)[:200])
    return ComponentHealth(
        name="redis",
        reachable=True,
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
    )


@router.get(
    "/capacity",
    response_model=CapacityResponse,
    summary="Platform inventory, live load and dependency health",
    dependencies=[Depends(require_platform_user())],
)
async def capacity(session: SessionDep) -> CapacityResponse:
    """Capacity and infrastructure health (spec 48).

    Every number is measured, not estimated. The dependency probes are the
    real thing — one round trip each — because a health page that reports a
    cached value is worse than no health page.
    """

    async def scalar(query) -> int:
        return int((await session.execute(query)).scalar_one())

    tenant_count = await scalar(select(func.count()).select_from(Tenant))
    active_tenants = await scalar(
        select(func.count()).select_from(Tenant).where(Tenant.status == TenantStatus.ACTIVE)
    )
    agent_count = await scalar(select(func.count()).select_from(Agent))
    # "Published" is the pointer being set, not a status column: spec 19 makes
    # publishing an agent mean it has a published version, and an agent can be
    # ACTIVE with only a draft.
    published_agents = await scalar(
        select(func.count()).select_from(Agent).where(Agent.published_version_id.is_not(None))
    )
    numbers = await scalar(select(func.count()).select_from(PhoneNumber))
    trunks = await scalar(select(func.count()).select_from(SipTrunk))
    active_calls = await scalar(
        select(func.count()).select_from(Call).where(Call.state.in_(_LIVE_STATES))
    )
    calls_24h = await scalar(
        select(func.count())
        .select_from(Call)
        .where(Call.start_time >= datetime.now(UTC) - timedelta(hours=24))
    )

    # Null when any tenant is uncapped: summing the capped ones would present a
    # floor as if it were the platform's ceiling.
    uncapped = await scalar(
        select(func.count()).select_from(Tenant).where(Tenant.max_concurrent_calls.is_(None))
    )
    licensed = (
        None
        if uncapped
        else int(
            (
                await session.execute(
                    select(func.coalesce(func.sum(Tenant.max_concurrent_calls), 0))
                )
            ).scalar_one()
            or 0
        )
    )

    # The PostgreSQL probe shares this request's session, so it runs first
    # rather than concurrently with the others.
    postgres = await _probe_postgres(session)
    (livekit, room_list), redis = await asyncio.gather(_probe_livekit(), _probe_redis())
    fleet = await collect_fleet(session, livekit_reachable=livekit.reachable)

    total_capacity = licensed if licensed is not None else fleet.worker_capacity
    available = (
        max(0, total_capacity - active_calls) if total_capacity is not None else None
    )

    return CapacityResponse(
        tenant_count=tenant_count,
        active_tenant_count=active_tenants,
        agent_count=agent_count,
        published_agent_count=published_agents,
        phone_number_count=numbers,
        sip_trunk_count=trunks,
        active_calls=active_calls,
        calls_last_24h=calls_24h,
        licensed_concurrent_calls=licensed,
        total_capacity=total_capacity,
        available_capacity=available,
        livekit_rooms=len(room_list) if livekit.reachable else None,
        livekit_nodes=fleet.livekit_nodes,
        sip_nodes=fleet.sip_nodes,
        ai_workers=fleet.ai_workers,
        worker_utilization=fleet.worker_utilization,
        cpu=_usage(fleet.cpu),
        memory=_usage(fleet.memory),
        network=_usage(fleet.network),
        providers=[
            ProviderHealthStatus(
                slug=item.slug,
                kind=item.kind,
                display_name=item.display_name,
                status=item.status,
                credentialed_tenants=item.credentialed_tenants,
                detail=item.detail,
            )
            for item in fleet.providers
        ],
        components=[postgres, livekit, redis],
    )


def _usage(sample) -> ResourceUsage | None:
    if sample is None:
        return None
    return ResourceUsage(
        source=sample.source,
        value=sample.value,
        unit=sample.unit,
        detail=sample.detail,
    )


# --------------------------------------------------------------------------- #
# LiveKit administration (spec 12, 46)
# --------------------------------------------------------------------------- #


@router.get(
    "/livekit",
    response_model=LiveKitOverview,
    summary="LiveKit reachability and mirror agreement",
    dependencies=[Depends(require_platform_user())],
)
async def livekit_overview(session: SessionDep) -> LiveKitOverview:
    """How far LiveKit has drifted from PostgreSQL.

    PostgreSQL is written first and LiveKit second (spec 12), so a row can be
    ``PENDING`` or ``FAILED`` while the database is already correct. Listing
    only those rows is the point: a healthy platform shows an empty list, and
    anything here is something a retry or a re-sync has to fix.
    """
    settings = get_settings()
    health, room_snaps = await _probe_livekit()

    async def counts(column: InstrumentedAttribute[SyncStatus]) -> dict[str, int]:
        """Group by sync status.

        Takes the column rather than the model: the two models share it only
        through ``LiveKitSyncMixin``, and a bare ``type`` parameter hides that
        from the type checker.
        """
        rows = (
            await session.execute(
                # Cross-tenant on purpose: drift is a platform-level concern
                # and a per-tenant view would hide the pattern.
                select(column, func.count()).group_by(column)
            )
        ).all()
        return {status_value.value: int(count) for status_value, count in rows}

    trunk_counts = await counts(SipTrunk.sync_status)
    rule_counts = await counts(LiveKitDispatchRule.sync_status)

    attention: list[DriftedResource] = []
    unhealthy = (SyncStatus.PENDING, SyncStatus.FAILED, SyncStatus.DRIFTED)

    for kind, model, label_column in (
        ("sip_trunk", SipTrunk, SipTrunk.name),
        ("dispatch_rule", LiveKitDispatchRule, LiveKitDispatchRule.name),
    ):
        rows = (
            await session.execute(
                select(model, label_column, Tenant.name)
                .join(Tenant, Tenant.id == model.tenant_id)
                .where(model.sync_status.in_(unhealthy))
                .order_by(model.sync_attempts.desc())
                .limit(50)
            )
        ).all()
        for row, name, tenant_name in rows:
            attention.append(
                DriftedResource(
                    kind=kind,
                    id=row.id,
                    tenant_id=row.tenant_id,
                    tenant_name=tenant_name,
                    name=name,
                    sync_status=row.sync_status.value,
                    livekit_resource_id=row.livekit_resource_id,
                    sync_error=row.sync_error,
                    sync_attempts=row.sync_attempts,
                    last_synced_at=row.last_synced_at,
                )
            )

    report = last_report()
    orphans = _orphans_from_report(report)
    drifted = trunk_counts.get(SyncStatus.DRIFTED.value, 0) + rule_counts.get(
        SyncStatus.DRIFTED.value, 0
    )
    dispatch_names = sorted(
        {
            name
            for name in (
                await session.execute(select(LiveKitDispatchRule.agent_dispatch_name).distinct())
            ).scalars()
            if name
        }
    )

    return LiveKitOverview(
        url=settings.livekit_url,
        sip_uri=settings.livekit_sip_uri,
        reachable=health.reachable,
        room_count=len(room_snaps) if health.reachable else None,
        detail=health.detail,
        trunk_sync=trunk_counts,
        dispatch_rule_sync=rule_counts,
        needs_attention=attention,
        last_drift_check_at=report.checked_at if report else None,
        drift_check_reachable=report.livekit_reachable if report else None,
        orphans=orphans,
        configuration_drift_detected=bool(orphans) or drifted > 0,
        sections=_livekit_admin_sections(
            trunk_count=sum(trunk_counts.values()),
            rule_count=sum(rule_counts.values()),
            room_count=len(room_snaps),
            participant_count=sum(item.num_participants for item in room_snaps),
            dispatch_names=dispatch_names,
        ),
        rooms=[
            LiveKitRoom(
                name=item.name,
                sid=item.sid,
                num_participants=item.num_participants,
                created_at=item.created_at,
                metadata=item.metadata,
            )
            for item in room_snaps
        ],
        participant_count=sum(item.num_participants for item in room_snaps),
        agent_dispatch_names=dispatch_names,
        public_url=settings.livekit_public_url,
    )


def _livekit_admin_sections(
    *,
    trunk_count: int,
    rule_count: int,
    room_count: int,
    participant_count: int,
    dispatch_names: list[str],
) -> list[LiveKitAdminSection]:
    """The thirteen spec-11 topics. Nothing here writes to LiveKit."""
    names = ", ".join(dispatch_names) if dispatch_names else "none configured"
    return [
        LiveKitAdminSection(
            id="clusters",
            title="Clusters",
            summary="One self-hosted cluster. Node topology lives in livekit.yaml, not this console.",
            scope="infra",
            href="/platform/infrastructure",
        ),
        LiveKitAdminSection(
            id="sip_configuration",
            title="SIP Configuration",
            summary="Tenant SIP is the wizard and trunk form. The advertised SIP URI is infrastructure.",
            scope="tenant",
            href="/sip-wizard",
        ),
        LiveKitAdminSection(
            id="sip_trunks",
            title="SIP Trunks",
            summary=f"{trunk_count} mirrored trunk(s). Tenants create them; this screen tracks sync.",
            scope="tenant",
            href="/sip-trunks",
        ),
        LiveKitAdminSection(
            id="dispatch_rules",
            title="Dispatch Rules",
            summary=f"{rule_count} long-lived rule(s). Never created per call.",
            scope="tenant",
            href="/dispatch-rules",
        ),
        LiveKitAdminSection(
            id="agent_dispatch",
            title="Agent Dispatch",
            summary=f"Worker identities LiveKit will dispatch: {names}.",
            scope="tenant",
            href="/dispatch-rules",
        ),
        LiveKitAdminSection(
            id="rooms",
            title="Rooms",
            summary=f"{room_count} room(s) currently open. Ephemeral — one per call.",
            scope="observe",
        ),
        LiveKitAdminSection(
            id="participants",
            title="Participants",
            summary=f"{participant_count} participant(s) across those rooms.",
            scope="observe",
        ),
        LiveKitAdminSection(
            id="media",
            title="Media",
            summary="RTC media is one muxed UDP port in this deployment. Not tenant-editable.",
            scope="infra",
            href="/platform/infrastructure",
        ),
        LiveKitAdminSection(
            id="codecs",
            title="Codecs",
            summary="Per-trunk codec preference is stored on the SIP trunk. Offer PCMU for telephony.",
            scope="tenant",
            href="/sip-trunks",
        ),
        LiveKitAdminSection(
            id="recording_egress",
            title="Recording/Egress",
            summary="Room-composite egress to object storage when an agent has recording enabled.",
            scope="observe",
            href="/recordings",
        ),
        LiveKitAdminSection(
            id="turn_ice",
            title="TURN/ICE",
            summary="Advertised ICE address is LIVEKIT_NODE_IP / SIP_NAT_IP. A container IP fails every call.",
            scope="infra",
            href="/platform/infrastructure",
        ),
        LiveKitAdminSection(
            id="health",
            title="Health",
            summary="Admin API reachability on this page; dependency probes on Infrastructure.",
            scope="observe",
            href="/platform/infrastructure",
        ),
        LiveKitAdminSection(
            id="metrics",
            title="Metrics",
            summary="Voice-latency dashboards in Grafana. This page is the mirror, not a second inventory.",
            scope="observe",
            href="/platform/infrastructure",
        ),
    ]


@router.post(
    "/livekit/drift-check",
    response_model=DriftCheckResponse,
    summary="Compare PostgreSQL against LiveKit now",
    dependencies=[Depends(require_platform_user())],
)
async def run_drift_check(
    session: SessionDep,
    request: Request,
    client_ip: ClientIp,
    principal: CurrentPrincipal,
) -> DriftCheckResponse:
    """On-demand drift detection (spec 46).

    Marks missing or mismatched rows ``DRIFTED``. Does not Repair — use the
    per-row Synchronize / Retry / Repair actions, because a platform-wide
    recreate would act across tenants with no review.
    """
    report = await detect_drift(
        session, SipResourceManager(), metrics=request.app.state.metrics
    )
    await audit.record(
        session,
        principal=principal,
        action="livekit.drift_checked",
        resource_type="livekit",
        new_value={
            "reachable": report.livekit_reachable,
            "drifted": len(report.drifted_findings),
            "orphans": len(report.orphans),
            "error": report.error,
        },
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
    return _drift_check_response(report)


@router.post(
    "/livekit/{kind}/{resource_id}/{action}",
    response_model=LiveKitSyncActionResponse,
    summary="Synchronize, retry, or repair one mirrored LiveKit resource",
    dependencies=[Depends(require_platform_user())],
)
async def livekit_resource_action(
    kind: str,
    resource_id: uuid.UUID,
    action: str,
    session: SessionDep,
    request: Request,
    client_ip: ClientIp,
    principal: CurrentPrincipal,
) -> LiveKitSyncActionResponse:
    """Per-row spec-12 actions. The UI returns immediately (spec 80)."""
    if kind not in {"sip_trunk", "dispatch_rule"}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown kind {kind}")
    if action not in {"synchronize", "retry", "repair"}:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown action {action}"
        )

    model = SipTrunk if kind == "sip_trunk" else LiveKitDispatchRule
    row = await session.get(model, resource_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no {kind} with id {resource_id}",
        )

    row.sync_status = SyncStatus.PENDING
    row.sync_error = None
    await audit.record(
        session,
        principal=principal,
        action=f"livekit.{kind}.{action}",
        resource_type=kind,
        resource_id=row.id,
        new_value={"action": action},
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()

    repair = action == "repair"
    if kind == "sip_trunk":
        enqueue_trunk_sync(row.id, repair=repair)
    else:
        enqueue_dispatch_rule_sync(row.id, repair=repair)

    return LiveKitSyncActionResponse(
        kind=kind,
        id=row.id,
        action=action,
        sync_status=row.sync_status.value,
    )


def _orphans_from_report(report: DriftReport | None) -> list[OrphanedResource]:
    if report is None:
        return []
    return [
        OrphanedResource(
            kind=item.resource,
            name=item.name,
            livekit_resource_id=item.livekit_resource_id or "",
            reason=item.reason,
        )
        for item in report.orphans
        if item.livekit_resource_id
    ]


def _drift_check_response(report: DriftReport) -> DriftCheckResponse:
    return DriftCheckResponse(
        checked_at=report.checked_at,
        livekit_reachable=report.livekit_reachable,
        trunks_compared=report.trunks_compared,
        rules_compared=report.rules_compared,
        drifted=len(report.drifted_findings),
        orphan_count=len(report.orphans),
        configuration_drift_detected=report.configuration_drift_detected,
        error=report.error,
        orphans=_orphans_from_report(report),
    )


# --------------------------------------------------------------------------- #
# Platform staff (spec 8, 60)
# --------------------------------------------------------------------------- #


async def _platform_roles(
    session: AsyncSession, user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    if not user_ids:
        return {}
    rows = (
        await session.execute(
            select(UserRole.user_id, Role.name)
            .join(Role, Role.id == UserRole.role_id)
            .where(UserRole.user_id.in_(user_ids), UserRole.tenant_id.is_(None))
        )
    ).all()
    grouped: dict[uuid.UUID, list[str]] = {}
    for user_id, role_name in rows:
        grouped.setdefault(user_id, []).append(role_name)
    return grouped


@router.get(
    "/users",
    response_model=Page[PlatformUserResponse],
    summary="List platform staff",
    dependencies=[Depends(require_platform_user())],
)
async def list_platform_users(session: SessionDep) -> Page[PlatformUserResponse]:
    """Platform accounts only.

    Tenant users are listed by the tenant's own endpoint. Mixing them here
    would put every customer's user list in front of platform staff for no
    operational reason.
    """
    rows = list(
        (
            await session.execute(
                select(User).where(User.is_platform_user.is_(True)).order_by(User.email)
            )
        )
        .scalars()
        .all()
    )
    roles = await _platform_roles(session, [r.id for r in rows])
    items = [
        PlatformUserResponse.model_validate(row, from_attributes=True).model_copy(
            update={
                "roles": sorted(roles.get(row.id, [])),
                "sessions_revoked": row.tokens_valid_from is not None,
            }
        )
        for row in rows
    ]
    return Page(items=items, total=len(items), limit=len(items), offset=0)


@router.post(
    "/users",
    response_model=PlatformUserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a platform account",
    dependencies=[Depends(require_super_admin())],
)
async def create_platform_user(
    payload: PlatformUserCreate,
    session: SessionDep,
    principal: CurrentPrincipal,
    request: Request,
    client_ip: ClientIp,
) -> PlatformUserResponse:
    """Create platform staff.

    Restricted to SUPER_ADMIN even though listing is not: a PLATFORM_OPERATOR
    being able to mint a SUPER_ADMIN would make the role distinction
    decorative.
    """
    email = str(payload.email).strip().lower()
    taken = (await session.execute(select(User.id).where(User.email == email))).scalar_one_or_none()
    if taken is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="an account with that email already exists",
        )

    role = (
        await session.execute(
            select(Role).where(Role.name == payload.role.value, Role.scope == RoleScope.PLATFORM)
        )
    ).scalar_one_or_none()
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"the role {payload.role.value!r} is not configured; run seed-rbac",
        )

    try:
        password_hash = hash_password(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    user = User(
        tenant_id=None,
        is_platform_user=True,
        email=email,
        full_name=payload.full_name,
        password_hash=password_hash,
    )
    session.add(user)
    await session.flush()
    # A platform role assignment has no tenant, which is why user_roles carries
    # a nullable tenant_id.
    session.add(UserRole(user_id=user.id, role_id=role.id, tenant_id=None))

    await audit.record(
        session,
        principal=principal,
        action="platform_user.created",
        resource_type="user",
        resource_id=user.id,
        new_value={"email": email, "role": payload.role.value},
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()

    logger.info("platform_user_created", extra={"created_user_id": str(user.id)})
    return PlatformUserResponse.model_validate(user, from_attributes=True).model_copy(
        update={"roles": [payload.role.value]}
    )


@router.put(
    "/users/{user_id}",
    response_model=PlatformUserResponse,
    summary="Update a platform account",
    dependencies=[Depends(require_super_admin())],
)
async def update_platform_user(
    user_id: uuid.UUID,
    payload: PlatformUserUpdate,
    session: SessionDep,
    principal: CurrentPrincipal,
    request: Request,
    client_ip: ClientIp,
) -> PlatformUserResponse:
    """Update platform staff.

    Both disabling an account and changing its password revoke its sessions,
    so neither takes effect only after the access token expires.
    """
    user = (
        await session.execute(
            select(User).where(User.id == user_id, User.is_platform_user.is_(True))
        )
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail=f"no platform account with id {user_id}")

    before = {"full_name": user.full_name, "is_active": user.is_active}
    revoked = False

    if payload.full_name is not None:
        user.full_name = payload.full_name

    if payload.password is not None:
        try:
            user.password_hash = hash_password(payload.password)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        revoked = True

    if payload.role is not None:
        role = (
            await session.execute(
                select(Role).where(
                    Role.name == payload.role.value, Role.scope == RoleScope.PLATFORM
                )
            )
        ).scalar_one_or_none()
        if role is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"the role {payload.role.value!r} is not configured",
            )
        if user.id == principal.user_id and payload.role is not PlatformRole.SUPER_ADMIN:
            # Demoting yourself out of SUPER_ADMIN can leave the platform with
            # nobody able to promote anyone back.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="you cannot remove your own SUPER_ADMIN role",
            )
        await session.execute(
            sql_delete(UserRole).where(UserRole.user_id == user_id, UserRole.tenant_id.is_(None))
        )
        session.add(UserRole(user_id=user_id, role_id=role.id, tenant_id=None))

    if payload.is_active is not None and payload.is_active != user.is_active:
        if not payload.is_active and user.id == principal.user_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="you cannot disable your own account",
            )
        user.is_active = payload.is_active
        revoked = revoked or not payload.is_active

    if revoked:
        user.tokens_valid_from = datetime.now(UTC)

    await audit.record(
        session,
        principal=principal,
        action="platform_user.updated",
        resource_type="user",
        resource_id=user.id,
        old_value=before,
        new_value={
            "full_name": user.full_name,
            "is_active": user.is_active,
            "role": payload.role.value if payload.role else None,
            "sessions_revoked": revoked,
        },
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()

    roles = await _platform_roles(session, [user.id])
    return PlatformUserResponse.model_validate(user, from_attributes=True).model_copy(
        update={
            "roles": sorted(roles.get(user.id, [])),
            "sessions_revoked": user.tokens_valid_from is not None,
        }
    )


# --------------------------------------------------------------------------- #
# Effective configuration (spec 60)
# --------------------------------------------------------------------------- #


@router.get(
    "/settings",
    response_model=PlatformSettingsResponse,
    summary="What the running process is configured with",
    dependencies=[Depends(require_platform_user())],
)
async def platform_settings() -> PlatformSettingsResponse:
    """Report the effective configuration, with secrets withheld.

    Read-only on purpose: these values come from the environment, so an
    editable screen would write somewhere the next restart overwrites. Secrets
    report only whether they are set — that is a real operational question,
    and the value itself must not leave the process (spec 70).
    """
    settings = get_settings()

    def secret(name: str, value: object, *, note: str | None = None) -> SettingEntry:
        raw = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value or "")
        return SettingEntry(name=name, value=None, is_secret=True, is_set=bool(raw), note=note)

    def plain(name: str, value: object, *, note: str | None = None) -> SettingEntry:
        return SettingEntry(name=name, value=str(value), note=note)

    weak_jwt = settings.jwt_secret.get_secret_value().startswith("change-me")

    return PlatformSettingsResponse(
        environment=str(settings.environment),
        groups={
            "Service": [
                plain("SERVICE_NAME", settings.service_name),
                plain("ENVIRONMENT", settings.environment),
                plain("LOG_LEVEL", settings.log_level),
                plain("API_BASE_PATH", settings.api_base_path),
                plain("CORS_ALLOW_ORIGINS", ", ".join(settings.cors_allow_origins)),
            ],
            "PostgreSQL": [
                plain("POSTGRES_HOST", settings.postgres_host),
                plain("POSTGRES_PORT", settings.postgres_port),
                plain("POSTGRES_DB", settings.postgres_db),
                plain("POSTGRES_USER", settings.postgres_user),
                secret("POSTGRES_PASSWORD", settings.postgres_password),
                plain("DB_POOL_SIZE", settings.db_pool_size),
                plain("DB_MAX_OVERFLOW", settings.db_max_overflow),
            ],
            "Redis": [
                plain("REDIS_URL", settings.redis_url),
                plain(
                    "REDIS_CALL_STATE_TTL_SECONDS",
                    settings.redis_call_state_ttl_seconds,
                    note="How long live call state survives a worker restart.",
                ),
            ],
            "Authentication": [
                plain("JWT_ALGORITHM", settings.jwt_algorithm),
                plain("ACCESS_TOKEN_TTL_MINUTES", settings.access_token_ttl_minutes),
                plain("REFRESH_TOKEN_TTL_DAYS", settings.refresh_token_ttl_days),
                secret(
                    "JWT_SECRET",
                    settings.jwt_secret,
                    note=(
                        "Still the built-in development value — every token this "
                        "platform issues is forgeable until it is replaced."
                        if weak_jwt
                        else None
                    ),
                ),
                secret(
                    "CREDENTIAL_ENCRYPTION_KEY",
                    settings.credential_encryption_key,
                    note=(
                        "Provider keys cannot be stored or read until this is set."
                        if not settings.credential_encryption_key.get_secret_value()
                        else "Fernet key that encrypts every stored provider credential."
                    ),
                ),
            ],
            "LiveKit": [
                plain("LIVEKIT_URL", settings.livekit_url),
                plain("LIVEKIT_SIP_URI", settings.livekit_sip_uri),
                secret("LIVEKIT_API_KEY", settings.livekit_api_key),
                secret("LIVEKIT_API_SECRET", settings.livekit_api_secret),
                plain(
                    "LIVEKIT_DRIFT_CHECK_ENABLED",
                    settings.livekit_drift_check_enabled,
                    note="Scheduled compare of PostgreSQL against LiveKit (spec 46).",
                ),
                plain(
                    "LIVEKIT_DRIFT_CHECK_INTERVAL_SECONDS",
                    settings.livekit_drift_check_interval_seconds,
                ),
                plain(
                    "LIVEKIT_DRIFT_CHECK_JITTER_SECONDS",
                    settings.livekit_drift_check_jitter_seconds,
                ),
                plain(
                    "WORKER_HEALTH_URLS",
                    ", ".join(settings.worker_health_urls),
                    note="Capacity dashboard probes these worker /ready endpoints.",
                ),
                plain("LIVEKIT_METRICS_URL", settings.livekit_metrics_url),
                plain(
                    "SIP_METRICS_URL",
                    settings.sip_metrics_url or "(not set)",
                    note="Optional. Without it, SIP node count is 1 when LIVEKIT_SIP_URI is set.",
                ),
            ],
            "Object storage": [
                plain("S3_ENDPOINT_URL", settings.s3_endpoint_url or "(AWS default)"),
                plain("S3_BUCKET_RECORDINGS", settings.s3_bucket_recordings),
                plain("S3_REGION", settings.s3_region),
                secret("S3_ACCESS_KEY_ID", settings.s3_access_key_id),
                secret("S3_SECRET_ACCESS_KEY", settings.s3_secret_access_key),
            ],
        },
    )
