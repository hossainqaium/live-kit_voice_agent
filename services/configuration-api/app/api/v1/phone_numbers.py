"""Phone number (DID) endpoints (spec 17, 67)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select

from app.core.dependencies import ClientIp, CurrentTenant, require_permission
from app.db.models import Agent, Pbx, PhoneNumber, SipTrunk
from app.db.repository import TenantRepository
from app.db.util import as_lookup
from app.schemas.common import Page
from app.schemas.telephony import (
    PhoneNumberCreate,
    PhoneNumberResponse,
    PhoneNumberUpdate,
)
from app.services import audit
from shared.logging import get_logger
from shared.models import Permission, ResourceStatus

logger = get_logger(__name__)

router = APIRouter(prefix="/phone-numbers", tags=["phone-numbers"])

_AUDITED = (
    "number",
    "label",
    "pbx_id",
    "sip_trunk_id",
    "inbound_agent_id",
    "routing_rule_id",
    "business_hours_id",
    "status",
)


def _not_found(number_id: uuid.UUID) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail=f"no phone number with id {number_id}"
    )


async def _decorate(tenant: CurrentTenant, rows: list[PhoneNumber]) -> list[PhoneNumberResponse]:
    """Attach the names behind the foreign keys.

    Three lookups for the whole page rather than one per row per key: the list
    view is otherwise unreadable, showing UUIDs where a human needs a PBX name.
    """
    pbx_ids = {row.pbx_id for row in rows if row.pbx_id}
    trunk_ids = {row.sip_trunk_id for row in rows if row.sip_trunk_id}
    agent_ids = {row.inbound_agent_id for row in rows if row.inbound_agent_id}

    async def names(model, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not ids:
            return {}
        result = await tenant.session.execute(
            select(model.id, model.name).where(
                model.tenant_id == tenant.tenant_id, model.id.in_(ids)
            )
        )
        return as_lookup(result.all())

    pbx_names = await names(Pbx, pbx_ids)
    trunk_names = await names(SipTrunk, trunk_ids)
    agent_names = await names(Agent, agent_ids)

    return [
        PhoneNumberResponse.model_validate(row, from_attributes=True).model_copy(
            update={
                "pbx_name": pbx_names.get(row.pbx_id) if row.pbx_id else None,
                "sip_trunk_name": trunk_names.get(row.sip_trunk_id) if row.sip_trunk_id else None,
                "agent_name": agent_names.get(row.inbound_agent_id)
                if row.inbound_agent_id
                else None,
            }
        )
        for row in rows
    ]


@router.get(
    "",
    response_model=Page[PhoneNumberResponse],
    summary="List phone numbers",
    dependencies=[Depends(require_permission(Permission.SIP_TRUNKS_READ))],
)
async def list_numbers(
    tenant: CurrentTenant,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[PhoneNumberResponse]:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    rows = await repository.list(
        PhoneNumber, limit=limit, offset=offset, order_by=PhoneNumber.number
    )
    total = await repository.count(PhoneNumber)
    return Page(items=await _decorate(tenant, rows), total=total, limit=limit, offset=offset)


async def _validate_references(
    tenant: CurrentTenant, repository: TenantRepository, payload: dict
) -> None:
    """Check every foreign key belongs to this tenant.

    Without this, a valid-looking UUID from another tenant would be accepted
    and produce a DID pointing at a resource its owner cannot see. The
    repository's scoping is what makes the check a tenant-safe one.
    """
    for field, model, label in (
        ("pbx_id", Pbx, "PBX"),
        ("sip_trunk_id", SipTrunk, "SIP trunk"),
        ("inbound_agent_id", Agent, "agent"),
    ):
        value = payload.get(field)
        if value is not None and not await repository.exists(model, value):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"that {label} does not exist in this tenant",
            )


@router.post(
    "",
    response_model=PhoneNumberResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a phone number",
    dependencies=[Depends(require_permission(Permission.SIP_TRUNKS_WRITE))],
)
async def create_number(
    payload: PhoneNumberCreate,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> PhoneNumberResponse:
    """Add a DID.

    A number is unique platform-wide, not per tenant: two tenants claiming the
    same DID would make inbound routing ambiguous. The 409 says so explicitly
    rather than leaking whether another tenant holds it.
    """
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    fields = payload.model_dump()
    await _validate_references(tenant, repository, fields)

    existing = await tenant.session.execute(
        select(PhoneNumber.id).where(PhoneNumber.number == payload.number)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{payload.number} is already registered. A number can belong to only "
                "one tenant, because inbound routing would otherwise be ambiguous."
            ),
        )

    row = PhoneNumber(**fields)
    repository.add(row)
    await tenant.session.flush()

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="phone_number.created",
        resource_type="phone_number",
        resource_id=row.id,
        new_value=audit.snapshot(row, *_AUDITED),
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()

    return (await _decorate(tenant, [row]))[0]


@router.put(
    "/{number_id}",
    response_model=PhoneNumberResponse,
    summary="Update a phone number",
    dependencies=[Depends(require_permission(Permission.SIP_TRUNKS_WRITE))],
)
async def update_number(
    number_id: uuid.UUID,
    payload: PhoneNumberUpdate,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> PhoneNumberResponse:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(PhoneNumber, number_id)
    if row is None:
        raise _not_found(number_id)

    before = audit.snapshot(row, *_AUDITED)
    changes = payload.model_dump(exclude_unset=True)
    await _validate_references(tenant, repository, changes)

    for field, value in changes.items():
        setattr(row, field, value)

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="phone_number.updated",
        resource_type="phone_number",
        resource_id=row.id,
        old_value=before,
        new_value=audit.snapshot(row, *_AUDITED),
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return (await _decorate(tenant, [row]))[0]


@router.post(
    "/{number_id}/enable",
    response_model=PhoneNumberResponse,
    summary="Enable a phone number",
    dependencies=[Depends(require_permission(Permission.SIP_TRUNKS_WRITE))],
)
async def enable_number(
    number_id: uuid.UUID, tenant: CurrentTenant, request: Request, client_ip: ClientIp
) -> PhoneNumberResponse:
    return await _set_status(
        number_id, ResourceStatus.ACTIVE, tenant, request, client_ip, "phone_number.enabled"
    )


@router.post(
    "/{number_id}/disable",
    response_model=PhoneNumberResponse,
    summary="Disable a phone number",
    dependencies=[Depends(require_permission(Permission.SIP_TRUNKS_WRITE))],
)
async def disable_number(
    number_id: uuid.UUID, tenant: CurrentTenant, request: Request, client_ip: ClientIp
) -> PhoneNumberResponse:
    return await _set_status(
        number_id, ResourceStatus.DISABLED, tenant, request, client_ip, "phone_number.disabled"
    )


async def _set_status(
    number_id: uuid.UUID,
    new_status: ResourceStatus,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
    action: str,
) -> PhoneNumberResponse:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(PhoneNumber, number_id)
    if row is None:
        raise _not_found(number_id)
    row.status = new_status
    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action=action,
        resource_type="phone_number",
        resource_id=row.id,
        new_value={"status": new_status.value},
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return (await _decorate(tenant, [row]))[0]


@router.delete(
    "/{number_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Remove a phone number",
    dependencies=[Depends(require_permission(Permission.SIP_TRUNKS_WRITE))],
)
async def delete_number(
    number_id: uuid.UUID,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> Response:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(PhoneNumber, number_id)
    if row is None:
        raise _not_found(number_id)

    before = audit.snapshot(row, *_AUDITED)
    await repository.delete(PhoneNumber, number_id)

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="phone_number.deleted",
        resource_type="phone_number",
        resource_id=number_id,
        old_value=before,
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
