"""Routing rules, business hours and transfer destinations (spec 20, 37, 35)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select

from app.core.dependencies import ClientIp, CurrentTenant, require_permission
from app.db.models import (
    Agent,
    BusinessHours,
    BusinessHoursInterval,
    PhoneNumber,
    RoutingRule,
    Tenant,
    TransferDestination,
)
from app.db.repository import TenantRepository
from app.db.util import as_lookup
from app.schemas.common import Page
from app.schemas.routing import (
    BusinessHoursCreate,
    BusinessHoursResponse,
    BusinessHoursUpdate,
    IntervalResponse,
    RoutingRuleCreate,
    RoutingRuleResponse,
    RoutingRuleUpdate,
    TransferDestinationCreate,
    TransferDestinationResponse,
    TransferDestinationUpdate,
)
from app.services import audit
from shared.logging import get_logger
from shared.models import DayOfWeek, Permission

logger = get_logger(__name__)

router = APIRouter(tags=["routing"])

_RULE_AUDITED = ("name", "priority", "conditions", "agent_id", "status")
_HOURS_AUDITED = ("name", "timezone", "holidays")
_DEST_AUDITED = ("name", "kind", "target", "whisper_summary", "ring_timeout_seconds", "status")


# --------------------------------------------------------------------------- #
# Routing rules (spec 20)
# --------------------------------------------------------------------------- #


@router.get(
    "/routing-rules",
    response_model=Page[RoutingRuleResponse],
    summary="List routing rules in evaluation order",
    dependencies=[Depends(require_permission(Permission.AGENTS_READ))],
)
async def list_rules(
    tenant: CurrentTenant,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[RoutingRuleResponse]:
    """Ordered by priority, which is the order they are evaluated in.

    Sorting by name instead would make the list read as arbitrary when the
    thing that matters is which rule wins.
    """
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    rows = list(
        (
            await tenant.session.execute(
                repository.scoped(RoutingRule)
                .order_by(RoutingRule.priority, RoutingRule.name)
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    total = await repository.count(RoutingRule)
    return Page(items=await _decorate_rules(tenant, rows), total=total, limit=limit, offset=offset)


async def _decorate_rules(
    tenant: CurrentTenant, rows: list[RoutingRule]
) -> list[RoutingRuleResponse]:
    agent_ids = {r.agent_id for r in rows if r.agent_id}
    hours_ids = {r.business_hours_id for r in rows if r.business_hours_id}

    agents = (
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
    hours = (
        as_lookup(
            (
                await tenant.session.execute(
                    select(BusinessHours.id, BusinessHours.name).where(
                        BusinessHours.tenant_id == tenant.tenant_id,
                        BusinessHours.id.in_(hours_ids),
                    )
                )
            ).all()
        )
        if hours_ids
        else {}
    )

    return [
        RoutingRuleResponse.model_validate(row, from_attributes=True).model_copy(
            update={
                "agent_name": agents.get(row.agent_id) if row.agent_id else None,
                "business_hours_name": hours.get(row.business_hours_id)
                if row.business_hours_id
                else None,
            }
        )
        for row in rows
    ]


async def _check_rule_references(repository: TenantRepository, payload: dict) -> None:
    for field, model, label in (
        ("agent_id", Agent, "agent"),
        ("fallback_agent_id", Agent, "fallback agent"),
        ("business_hours_id", BusinessHours, "business hours schedule"),
        ("fallback_transfer_destination_id", TransferDestination, "fallback destination"),
        ("closed_transfer_destination_id", TransferDestination, "closed destination"),
    ):
        value = payload.get(field)
        if value is not None and not await repository.exists(model, value):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"that {label} does not exist in this tenant",
            )


@router.post(
    "/routing-rules",
    response_model=RoutingRuleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a routing rule",
    dependencies=[Depends(require_permission(Permission.AGENTS_WRITE))],
)
async def create_rule(
    payload: RoutingRuleCreate,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> RoutingRuleResponse:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    fields = payload.model_dump()
    fields["conditions"] = {k: str(v) for k, v in fields["conditions"].items() if v is not None}
    await _check_rule_references(repository, fields)

    clash = await tenant.session.execute(
        repository.scoped(RoutingRule).where(RoutingRule.name == payload.name)
    )
    if clash.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a routing rule named {payload.name!r} already exists",
        )

    row = RoutingRule(**fields)
    repository.add(row)
    await tenant.session.flush()

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="routing.changed",
        resource_type="routing_rule",
        resource_id=row.id,
        new_value=audit.snapshot(row, *_RULE_AUDITED),
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return (await _decorate_rules(tenant, [row]))[0]


@router.put(
    "/routing-rules/{rule_id}",
    response_model=RoutingRuleResponse,
    summary="Update a routing rule",
    dependencies=[Depends(require_permission(Permission.AGENTS_WRITE))],
)
async def update_rule(
    rule_id: uuid.UUID,
    payload: RoutingRuleUpdate,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> RoutingRuleResponse:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(RoutingRule, rule_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no routing rule with id {rule_id}")

    before = audit.snapshot(row, *_RULE_AUDITED)
    changes = payload.model_dump(exclude_unset=True)
    if "conditions" in changes and changes["conditions"] is not None:
        changes["conditions"] = {
            k: str(v) for k, v in changes["conditions"].items() if v is not None
        }
    await _check_rule_references(repository, changes)

    for field, value in changes.items():
        setattr(row, field, value)

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="routing.changed",
        resource_type="routing_rule",
        resource_id=row.id,
        old_value=before,
        new_value=audit.snapshot(row, *_RULE_AUDITED),
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return (await _decorate_rules(tenant, [row]))[0]


@router.delete(
    "/routing-rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a routing rule",
    dependencies=[Depends(require_permission(Permission.AGENTS_WRITE))],
)
async def delete_rule(
    rule_id: uuid.UUID, tenant: CurrentTenant, request: Request, client_ip: ClientIp
) -> Response:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(RoutingRule, rule_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no routing rule with id {rule_id}")

    used_by = await tenant.session.execute(
        repository.scoped(PhoneNumber).where(PhoneNumber.routing_rule_id == rule_id)
    )
    numbers = [n.number for n in used_by.scalars().all()]
    if numbers:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"still used by phone number(s): {', '.join(sorted(numbers))}",
        )

    before = audit.snapshot(row, *_RULE_AUDITED)
    await repository.delete(RoutingRule, rule_id)
    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="routing.changed",
        resource_type="routing_rule",
        resource_id=rule_id,
        old_value=before,
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Business hours (spec 37)
# --------------------------------------------------------------------------- #


_WEEKDAY_ORDER = list(DayOfWeek)


def _is_open_now(
    intervals: list[BusinessHoursInterval], timezone_name: str, holidays: list
) -> bool | None:
    """Whether the schedule says open right now, in its own timezone.

    Returns None when the timezone is unknown rather than guessing: showing
    "closed" because a timezone string was mistyped would be worse than
    showing nothing.
    """
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        return None

    now = datetime.now(UTC).astimezone(zone)
    today = now.date().isoformat()

    for holiday in holidays or []:
        if isinstance(holiday, dict) and holiday.get("date") == today:
            # An entry may also declare a day open, e.g. a trading Sunday.
            return not bool(holiday.get("closed", True))

    weekday = _WEEKDAY_ORDER[now.weekday()]
    current = now.time()
    return any(
        interval.day_of_week == weekday and interval.opens_at <= current < interval.closes_at
        for interval in intervals
    )


async def _hours_response(
    tenant: CurrentTenant, row: BusinessHours, fallback_timezone: str
) -> BusinessHoursResponse:
    intervals = list(
        (
            await tenant.session.execute(
                select(BusinessHoursInterval)
                .where(
                    BusinessHoursInterval.tenant_id == tenant.tenant_id,
                    BusinessHoursInterval.business_hours_id == row.id,
                )
                .order_by(BusinessHoursInterval.day_of_week, BusinessHoursInterval.opens_at)
            )
        )
        .scalars()
        .all()
    )
    return BusinessHoursResponse.model_validate(row, from_attributes=True).model_copy(
        update={
            "intervals": [
                IntervalResponse.model_validate(i, from_attributes=True) for i in intervals
            ],
            "open_now": _is_open_now(
                intervals, row.timezone or fallback_timezone, list(row.holidays or [])
            ),
        }
    )


async def _tenant_timezone(tenant: CurrentTenant) -> str:
    row = (
        await tenant.session.execute(select(Tenant.timezone).where(Tenant.id == tenant.tenant_id))
    ).scalar_one_or_none()
    return row or "UTC"


@router.get(
    "/business-hours",
    response_model=Page[BusinessHoursResponse],
    summary="List business hours schedules",
    dependencies=[Depends(require_permission(Permission.AGENTS_READ))],
)
async def list_hours(
    tenant: CurrentTenant,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[BusinessHoursResponse]:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    rows = await repository.list(
        BusinessHours, limit=limit, offset=offset, order_by=BusinessHours.name
    )
    total = await repository.count(BusinessHours)
    fallback = await _tenant_timezone(tenant)
    return Page(
        items=[await _hours_response(tenant, row, fallback) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/business-hours",
    response_model=BusinessHoursResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a business hours schedule",
    dependencies=[Depends(require_permission(Permission.AGENTS_WRITE))],
)
async def create_hours(
    payload: BusinessHoursCreate,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> BusinessHoursResponse:
    repository = TenantRepository(tenant.session, tenant.tenant_id)

    if payload.timezone:
        try:
            ZoneInfo(payload.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{payload.timezone!r} is not a known IANA timezone",
            ) from exc

    clash = await tenant.session.execute(
        repository.scoped(BusinessHours).where(BusinessHours.name == payload.name)
    )
    if clash.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a schedule named {payload.name!r} already exists",
        )

    row = BusinessHours(name=payload.name, timezone=payload.timezone, holidays=payload.holidays)
    repository.add(row)
    await tenant.session.flush()

    for interval in payload.intervals:
        repository.add(
            BusinessHoursInterval(
                business_hours_id=row.id,
                day_of_week=interval.day_of_week,
                opens_at=interval.opens_at,
                closes_at=interval.closes_at,
            )
        )

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="business_hours.created",
        resource_type="business_hours",
        resource_id=row.id,
        new_value=audit.snapshot(row, *_HOURS_AUDITED),
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return await _hours_response(tenant, row, await _tenant_timezone(tenant))


@router.put(
    "/business-hours/{hours_id}",
    response_model=BusinessHoursResponse,
    summary="Update a schedule and replace its intervals",
    dependencies=[Depends(require_permission(Permission.AGENTS_WRITE))],
)
async def update_hours(
    hours_id: uuid.UUID,
    payload: BusinessHoursUpdate,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> BusinessHoursResponse:
    """Update a schedule.

    Intervals are replaced wholesale rather than patched: a weekly schedule is
    edited as a unit in the UI, and diffing individual rows would let a partial
    failure leave a schedule that is open when it should be closed.
    """
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(BusinessHours, hours_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no schedule with id {hours_id}")

    before = audit.snapshot(row, *_HOURS_AUDITED)
    changes = payload.model_dump(exclude_unset=True)
    intervals = changes.pop("intervals", None)

    if changes.get("timezone"):
        try:
            ZoneInfo(changes["timezone"])
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{changes['timezone']!r} is not a known IANA timezone",
            ) from exc

    for field, value in changes.items():
        setattr(row, field, value)

    if intervals is not None:
        from sqlalchemy import delete as sql_delete

        await tenant.session.execute(
            sql_delete(BusinessHoursInterval).where(
                BusinessHoursInterval.tenant_id == tenant.tenant_id,
                BusinessHoursInterval.business_hours_id == hours_id,
            )
        )
        for interval in intervals:
            repository.add(
                BusinessHoursInterval(
                    business_hours_id=hours_id,
                    day_of_week=interval["day_of_week"],
                    opens_at=interval["opens_at"],
                    closes_at=interval["closes_at"],
                )
            )

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="business_hours.updated",
        resource_type="business_hours",
        resource_id=row.id,
        old_value=before,
        new_value=audit.snapshot(row, *_HOURS_AUDITED),
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return await _hours_response(tenant, row, await _tenant_timezone(tenant))


@router.delete(
    "/business-hours/{hours_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a schedule",
    dependencies=[Depends(require_permission(Permission.AGENTS_WRITE))],
)
async def delete_hours(
    hours_id: uuid.UUID, tenant: CurrentTenant, request: Request, client_ip: ClientIp
) -> Response:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(BusinessHours, hours_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no schedule with id {hours_id}")

    rules = await tenant.session.execute(
        repository.scoped(RoutingRule).where(RoutingRule.business_hours_id == hours_id)
    )
    names = [r.name for r in rules.scalars().all()]
    if names:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"still used by routing rule(s): {', '.join(sorted(names))}",
        )

    await repository.delete(BusinessHours, hours_id)
    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="business_hours.deleted",
        resource_type="business_hours",
        resource_id=hours_id,
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Transfer destinations (spec 35)
# --------------------------------------------------------------------------- #


@router.get(
    "/transfer-destinations",
    response_model=Page[TransferDestinationResponse],
    summary="List transfer destinations",
    dependencies=[Depends(require_permission(Permission.AGENTS_READ))],
)
async def list_destinations(
    tenant: CurrentTenant,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[TransferDestinationResponse]:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    rows = await repository.list(
        TransferDestination, limit=limit, offset=offset, order_by=TransferDestination.name
    )
    total = await repository.count(TransferDestination)
    return Page(
        items=[TransferDestinationResponse.model_validate(r, from_attributes=True) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/transfer-destinations",
    response_model=TransferDestinationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a transfer destination",
    dependencies=[Depends(require_permission(Permission.AGENTS_WRITE))],
)
async def create_destination(
    payload: TransferDestinationCreate,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> TransferDestinationResponse:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    clash = await tenant.session.execute(
        repository.scoped(TransferDestination).where(TransferDestination.name == payload.name)
    )
    if clash.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a destination named {payload.name!r} already exists",
        )

    row = TransferDestination(**payload.model_dump())
    repository.add(row)
    await tenant.session.flush()

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="transfer_destination.created",
        resource_type="transfer_destination",
        resource_id=row.id,
        new_value=audit.snapshot(row, *_DEST_AUDITED),
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return TransferDestinationResponse.model_validate(row, from_attributes=True)


@router.put(
    "/transfer-destinations/{dest_id}",
    response_model=TransferDestinationResponse,
    summary="Update a transfer destination",
    dependencies=[Depends(require_permission(Permission.AGENTS_WRITE))],
)
async def update_destination(
    dest_id: uuid.UUID,
    payload: TransferDestinationUpdate,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> TransferDestinationResponse:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(TransferDestination, dest_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no destination with id {dest_id}")

    before = audit.snapshot(row, *_DEST_AUDITED)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field, value)

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="transfer_destination.updated",
        resource_type="transfer_destination",
        resource_id=row.id,
        old_value=before,
        new_value=audit.snapshot(row, *_DEST_AUDITED),
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return TransferDestinationResponse.model_validate(row, from_attributes=True)


@router.delete(
    "/transfer-destinations/{dest_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a transfer destination",
    dependencies=[Depends(require_permission(Permission.AGENTS_WRITE))],
)
async def delete_destination(
    dest_id: uuid.UUID, tenant: CurrentTenant, request: Request, client_ip: ClientIp
) -> Response:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    if not await repository.exists(TransferDestination, dest_id):
        raise HTTPException(status_code=404, detail=f"no destination with id {dest_id}")
    await repository.delete(TransferDestination, dest_id)
    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="transfer_destination.deleted",
        resource_type="transfer_destination",
        resource_id=dest_id,
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
