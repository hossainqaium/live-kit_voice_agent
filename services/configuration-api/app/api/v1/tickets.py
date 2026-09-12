"""Support ticket endpoints (Phase 6.0).

Tickets are tenant-owned operations records. The console files them as
``MANUAL``. Server Agent files them as ``AGENT`` during a call via the
``create_ticket()`` builtin (Plan 6.1).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from shared.models import Permission, TicketSource, TicketStatus
from sqlalchemy import func, select

from app.core.dependencies import ClientIp, CurrentTenant, require_permission
from app.db.models import Agent, Ticket
from app.db.repository import TenantRepository
from app.schemas.common import Page
from app.schemas.ticket import TicketCreate, TicketResponse, TicketUpdate
from app.services import audit

router = APIRouter(prefix="/tickets", tags=["tickets"])

_AUDITED = ("ticket_number", "title", "status", "priority", "source", "caller_number")


def _not_found(ticket_id: uuid.UUID) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail=f"no ticket with id {ticket_id}"
    )


async def _agent_names(tenant: CurrentTenant, agent_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    ids = [item for item in agent_ids if item is not None]
    if not ids:
        return {}
    rows = await tenant.session.execute(
        select(Agent.id, Agent.name).where(
            Agent.tenant_id == tenant.tenant_id, Agent.id.in_(ids)
        )
    )
    return {row_id: name for row_id, name in rows.all()}


def _to_response(row: Ticket, names: dict[uuid.UUID, str]) -> TicketResponse:
    payload = TicketResponse.model_validate(row, from_attributes=True)
    return payload.model_copy(update={"agent_name": names.get(row.agent_id) if row.agent_id else None})


async def next_ticket_number(tenant: CurrentTenant) -> str:
    total = int(
        (
            await tenant.session.execute(
                select(func.count())
                .select_from(Ticket)
                .where(Ticket.tenant_id == tenant.tenant_id)
            )
        ).scalar_one()
    )
    return f"TCK-{total + 1:04d}"


@router.get(
    "",
    response_model=Page[TicketResponse],
    summary="List support tickets",
    dependencies=[Depends(require_permission(Permission.CALLS_READ))],
)
async def list_tickets(
    tenant: CurrentTenant,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    status_filter: Annotated[TicketStatus | None, Query(alias="status")] = None,
) -> Page[TicketResponse]:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    query = repository.scoped(Ticket)
    if status_filter is not None:
        query = query.where(Ticket.status == status_filter)
    total = int(
        (
            await tenant.session.execute(
                select(func.count()).select_from(query.order_by(None).subquery())
            )
        ).scalar_one()
    )
    rows = (
        await tenant.session.execute(
            query.order_by(Ticket.created_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()
    names = await _agent_names(tenant, [row.agent_id for row in rows if row.agent_id])
    return Page(
        items=[_to_response(row, names) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{ticket_id}",
    response_model=TicketResponse,
    summary="Fetch one ticket",
    dependencies=[Depends(require_permission(Permission.CALLS_READ))],
)
async def get_ticket(ticket_id: uuid.UUID, tenant: CurrentTenant) -> TicketResponse:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(Ticket, ticket_id)
    if row is None:
        raise _not_found(ticket_id)
    names = await _agent_names(tenant, [row.agent_id] if row.agent_id else [])
    return _to_response(row, names)


@router.post(
    "",
    response_model=TicketResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a support ticket",
    dependencies=[Depends(require_permission(Permission.AGENTS_WRITE))],
)
async def create_ticket(
    payload: TicketCreate,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> TicketResponse:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = Ticket(
        ticket_number=await next_ticket_number(tenant),
        title=payload.title.strip(),
        description=payload.description.strip(),
        priority=payload.priority,
        caller_number=payload.caller_number.strip() if payload.caller_number else None,
        source=TicketSource.MANUAL,
    )
    repository.add(row)
    await tenant.session.flush()
    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="ticket.created",
        resource_type="ticket",
        resource_id=row.id,
        new_value=audit.snapshot(row, *_AUDITED),
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return _to_response(row, {})


@router.put(
    "/{ticket_id}",
    response_model=TicketResponse,
    summary="Update a support ticket",
    dependencies=[Depends(require_permission(Permission.AGENTS_WRITE))],
)
async def update_ticket(
    ticket_id: uuid.UUID,
    payload: TicketUpdate,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> TicketResponse:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(Ticket, ticket_id)
    if row is None:
        raise _not_found(ticket_id)

    before = audit.snapshot(row, *_AUDITED)
    changes = payload.model_dump(exclude_unset=True)
    if "title" in changes and changes["title"] is not None:
        changes["title"] = changes["title"].strip()
    if "description" in changes and changes["description"] is not None:
        changes["description"] = changes["description"].strip()
    if "caller_number" in changes and changes["caller_number"] is not None:
        changes["caller_number"] = changes["caller_number"].strip() or None
    for field, value in changes.items():
        setattr(row, field, value)

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="ticket.updated",
        resource_type="ticket",
        resource_id=row.id,
        old_value=before,
        new_value=audit.snapshot(row, *_AUDITED),
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    names = await _agent_names(tenant, [row.agent_id] if row.agent_id else [])
    return _to_response(row, names)


@router.delete(
    "/{ticket_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a support ticket",
    dependencies=[Depends(require_permission(Permission.AGENTS_WRITE))],
)
async def delete_ticket(
    ticket_id: uuid.UUID,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> Response:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(Ticket, ticket_id)
    if row is None:
        raise _not_found(ticket_id)
    before = audit.snapshot(row, *_AUDITED)
    await repository.delete(Ticket, ticket_id)
    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="ticket.deleted",
        resource_type="ticket",
        resource_id=ticket_id,
        old_value=before,
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
