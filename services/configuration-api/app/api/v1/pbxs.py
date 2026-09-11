"""PBX endpoints (spec 14, 67).

This is the reference pattern for every tenant-scoped resource router:

* the handler receives a ``TenantScope``, never a tenant ID it could get wrong
* every route declares its permission, enforced here rather than in the UI
* mutations write an audit row in the same transaction as the change
* a row belonging to another tenant returns 404, not 403

Following it makes the isolation and authorization guarantees a property of the
route declaration rather than of the handler body.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app.core.dependencies import ClientIp, CurrentTenant, require_permission
from app.db.models import Pbx, PhoneNumber, SipTrunk
from app.db.repository import TenantRepository
from app.schemas.common import Page
from app.schemas.pbx import (
    PbxCreate,
    PbxResponse,
    PbxTestResult,
    PbxUpdate,
)
from app.services import audit
from app.services.connection_test import probe_sip_endpoint
from shared.logging import get_logger
from shared.models import ConnectionTestResult, Permission, ResourceStatus

logger = get_logger(__name__)

router = APIRouter(prefix="/pbxs", tags=["pbxs"])

#: Fields captured in audit old/new values. An explicit list rather than the
#: whole row, so a column added later does not silently widen the trail.
_AUDITED = ("name", "pbx_type", "host", "port", "transport", "status", "description")


def _not_found(pbx_id: uuid.UUID) -> HTTPException:
    """404 for a missing row and for another tenant's row alike.

    Returning 403 for the second case would confirm that the id exists, and
    cross-tenant existence is itself information spec 7 does not permit
    leaking.
    """
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no PBX with id {pbx_id}")


@router.get(
    "",
    response_model=Page[PbxResponse],
    summary="List this tenant's PBXs",
    dependencies=[Depends(require_permission(Permission.PBXS_READ))],
)
async def list_pbxs(
    tenant: CurrentTenant,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[PbxResponse]:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    rows = await repository.list(Pbx, limit=limit, offset=offset, order_by=Pbx.name)
    total = await repository.count(Pbx)
    return Page(
        items=[PbxResponse.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{pbx_id}",
    response_model=PbxResponse,
    summary="Fetch one PBX",
    dependencies=[Depends(require_permission(Permission.PBXS_READ))],
)
async def get_pbx(pbx_id: uuid.UUID, tenant: CurrentTenant) -> PbxResponse:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(Pbx, pbx_id)
    if row is None:
        raise _not_found(pbx_id)
    return PbxResponse.model_validate(row)


@router.post(
    "",
    response_model=PbxResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a PBX",
    dependencies=[Depends(require_permission(Permission.PBXS_WRITE))],
)
async def create_pbx(
    payload: PbxCreate,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> PbxResponse:
    """Register a PBX the tenant already operates.

    The name is unique per tenant, which the database enforces; the check here
    exists to return a 409 rather than surfacing a constraint violation.
    """
    repository = TenantRepository(tenant.session, tenant.tenant_id)

    clash = await tenant.session.execute(repository.scoped(Pbx).where(Pbx.name == payload.name))
    if clash.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a PBX named {payload.name!r} already exists",
        )

    row = Pbx(**payload.model_dump())
    repository.add(row)
    await tenant.session.flush()

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="pbx.created",
        resource_type="pbx",
        resource_id=row.id,
        new_value=audit.snapshot(row, *_AUDITED),
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
        user_agent=request.headers.get("user-agent"),
    )
    await tenant.session.commit()

    logger.info("pbx_created", extra={"pbx_id": str(row.id), "pbx_name": row.name})
    return PbxResponse.model_validate(row)


@router.put(
    "/{pbx_id}",
    response_model=PbxResponse,
    summary="Update a PBX",
    dependencies=[Depends(require_permission(Permission.PBXS_WRITE))],
)
async def update_pbx(
    pbx_id: uuid.UUID,
    payload: PbxUpdate,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> PbxResponse:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(Pbx, pbx_id)
    if row is None:
        raise _not_found(pbx_id)

    before = audit.snapshot(row, *_AUDITED)
    changes = payload.model_dump(exclude_unset=True)

    if "name" in changes and changes["name"] != row.name:
        clash = await tenant.session.execute(
            repository.scoped(Pbx).where(Pbx.name == changes["name"], Pbx.id != pbx_id)
        )
        if clash.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"a PBX named {changes['name']!r} already exists",
            )

    # Changing the host or port invalidates any previous connection test: the
    # record would otherwise keep claiming PASSED for an address it was never
    # tested against.
    if any(field in changes for field in ("host", "port", "transport")):
        row.last_test_result = ConnectionTestResult.UNTESTED
        row.last_tested_at = None
        row.last_test_detail = "reset because the address changed"

    for field, value in changes.items():
        setattr(row, field, value)

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="pbx.updated",
        resource_type="pbx",
        resource_id=row.id,
        old_value=before,
        new_value=audit.snapshot(row, *_AUDITED),
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
        user_agent=request.headers.get("user-agent"),
    )
    await tenant.session.commit()

    logger.info("pbx_updated", extra={"pbx_id": str(row.id), "changed": sorted(changes)})
    return PbxResponse.model_validate(row)


@router.delete(
    "/{pbx_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    # response_class is explicit because a 204 carries no body: FastAPI
    # otherwise infers a response field from the return annotation and refuses
    # to register the route.
    response_class=Response,
    summary="Delete a PBX",
    dependencies=[Depends(require_permission(Permission.PBXS_WRITE))],
)
async def delete_pbx(
    pbx_id: uuid.UUID,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> Response:
    """Delete a PBX.

    Refused while trunks or DIDs still reference it. The foreign keys use
    SET NULL, so deleting would silently orphan a trunk and break routing at
    the next call rather than at the moment of the mistake.
    """
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(Pbx, pbx_id)
    if row is None:
        raise _not_found(pbx_id)

    trunks = await tenant.session.execute(
        repository.scoped(SipTrunk).where(SipTrunk.pbx_id == pbx_id)
    )
    trunk_names = [trunk.name for trunk in trunks.scalars().all()]

    dids = await tenant.session.execute(
        repository.scoped(PhoneNumber).where(PhoneNumber.pbx_id == pbx_id)
    )
    did_numbers = [did.number for did in dids.scalars().all()]

    if trunk_names or did_numbers:
        parts = []
        if trunk_names:
            parts.append(f"SIP trunk(s): {', '.join(sorted(trunk_names))}")
        if did_numbers:
            parts.append(f"phone number(s): {', '.join(sorted(did_numbers))}")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "this PBX is still referenced by " + "; ".join(parts) + ". "
                "Reassign or remove them first."
            ),
        )

    before = audit.snapshot(row, *_AUDITED)
    await repository.delete(Pbx, pbx_id)

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="pbx.deleted",
        resource_type="pbx",
        resource_id=pbx_id,
        old_value=before,
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
        user_agent=request.headers.get("user-agent"),
    )
    await tenant.session.commit()
    logger.info("pbx_deleted", extra={"pbx_id": str(pbx_id)})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{pbx_id}/test",
    response_model=PbxTestResult,
    summary="Test connectivity to a PBX",
    dependencies=[Depends(require_permission(Permission.PBXS_READ))],
)
async def test_pbx(
    pbx_id: uuid.UUID,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> PbxTestResult:
    """Probe the PBX and record the outcome (spec 14 "Test Connection").

    Requires only read permission: a test changes no configuration, and making
    it a write would stop an ANALYST from diagnosing a problem they can already
    see.
    """
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(Pbx, pbx_id)
    if row is None:
        raise _not_found(pbx_id)

    outcome = await probe_sip_endpoint(host=row.host, port=row.port, transport=row.transport)

    row.last_test_result = outcome.result
    row.last_tested_at = datetime.now(UTC)
    row.last_test_detail = outcome.detail

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="pbx.tested",
        resource_type="pbx",
        resource_id=row.id,
        new_value={"result": outcome.result.value, "detail": outcome.detail},
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()

    logger.info(
        "pbx_tested",
        extra={
            "pbx_id": str(row.id),
            "test_result": outcome.result.value,
            "latency_ms": outcome.latency_ms,
        },
    )

    return PbxTestResult(
        result=outcome.result,
        detail=outcome.detail,
        tested_at=row.last_tested_at,
        latency_ms=outcome.latency_ms,
    )


@router.post(
    "/{pbx_id}/enable",
    response_model=PbxResponse,
    summary="Enable a PBX",
    dependencies=[Depends(require_permission(Permission.PBXS_WRITE))],
)
async def enable_pbx(
    pbx_id: uuid.UUID, tenant: CurrentTenant, request: Request, client_ip: ClientIp
) -> PbxResponse:
    return await _set_status(
        pbx_id, ResourceStatus.ACTIVE, tenant, request, client_ip, "pbx.enabled"
    )


@router.post(
    "/{pbx_id}/disable",
    response_model=PbxResponse,
    summary="Disable a PBX",
    dependencies=[Depends(require_permission(Permission.PBXS_WRITE))],
)
async def disable_pbx(
    pbx_id: uuid.UUID, tenant: CurrentTenant, request: Request, client_ip: ClientIp
) -> PbxResponse:
    """Disable a PBX.

    Deliberately allowed even while trunks reference it, unlike delete:
    disabling is how an operator takes a misbehaving PBX out of service during
    an incident, and refusing would make the platform obstruct exactly the
    action the situation calls for. The record and its references survive.
    """
    return await _set_status(
        pbx_id, ResourceStatus.DISABLED, tenant, request, client_ip, "pbx.disabled"
    )


async def _set_status(
    pbx_id: uuid.UUID,
    new_status: ResourceStatus,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
    action: str,
) -> PbxResponse:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(Pbx, pbx_id)
    if row is None:
        raise _not_found(pbx_id)

    before = {"status": row.status.value}
    row.status = new_status

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action=action,
        resource_type="pbx",
        resource_id=row.id,
        old_value=before,
        new_value={"status": new_status.value},
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    logger.info(action.replace(".", "_"), extra={"pbx_id": str(row.id)})
    return PbxResponse.model_validate(row)
