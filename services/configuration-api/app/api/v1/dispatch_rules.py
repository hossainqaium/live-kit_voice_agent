"""LiveKit dispatch-rule endpoints (spec 21, 12).

Rules are long-lived configuration. Nothing here runs on the call path — a
rule is created once, mirrored into LiveKit, and reused for every subsequent
call. Creating one per call is the defect spec 21 exists to prevent.

Ordering is the same as trunks: PostgreSQL first, LiveKit second. A
``PENDING`` row with no LiveKit ID is recoverable; a LiveKit rule with no row
is an orphan the drift job reports.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from shared.logging import get_logger
from shared.models import Permission, ResourceStatus, RoomStrategy, SyncStatus
from sqlalchemy import select

from app.core.dependencies import ClientIp, CurrentTenant, require_permission
from app.db.models import LiveKitDispatchRule, PhoneNumber, SipTrunk
from app.db.repository import TenantRepository
from app.livekit import LiveKitError, SipResourceManager
from app.schemas.common import Page
from app.schemas.telephony import (
    DispatchRuleCreate,
    DispatchRuleResponse,
    DispatchRuleUpdate,
)
from app.services import audit

logger = get_logger(__name__)

router = APIRouter(prefix="/dispatch-rules", tags=["dispatch-rules"])

_AUDITED = (
    "name",
    "sip_trunk_id",
    "room_strategy",
    "room_prefix",
    "agent_dispatch_name",
    "matched_numbers",
    "status",
)


def _not_found(rule_id: uuid.UUID) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail=f"no dispatch rule with id {rule_id}"
    )


async def _decorate(
    tenant: CurrentTenant, row: LiveKitDispatchRule
) -> DispatchRuleResponse:
    trunk_name = None
    trunk_dids: list[str] = []
    if row.sip_trunk_id:
        trunk = await TenantRepository(tenant.session, tenant.tenant_id).get(
            SipTrunk, row.sip_trunk_id
        )
        if trunk is not None:
            trunk_name = trunk.name
            trunk_dids = await _trunk_dids(tenant, trunk.id)
    payload = DispatchRuleResponse.model_validate(row, from_attributes=True)
    return payload.model_copy(update={"sip_trunk_name": trunk_name, "trunk_dids": trunk_dids})


async def _trunk_dids(tenant: CurrentTenant, trunk_id: uuid.UUID) -> list[str]:
    rows = await tenant.session.execute(
        select(PhoneNumber.number).where(
            PhoneNumber.tenant_id == tenant.tenant_id,
            PhoneNumber.sip_trunk_id == trunk_id,
            PhoneNumber.status == ResourceStatus.ACTIVE,
        )
    )
    return sorted(rows.scalars().all())


async def _sync_to_livekit(tenant: CurrentTenant, rule: LiveKitDispatchRule) -> None:
    """Create or replace the LiveKit dispatch rule (spec 12).

    LiveKit has no reliable in-place update for dispatch rules, so a change
    that the mirror cares about is delete-and-recreate. The PostgreSQL row
    keeps its identity; only ``livekit_resource_id`` changes.
    """
    if rule.room_strategy is not RoomStrategy.INDIVIDUAL:
        rule.sync_status = SyncStatus.FAILED
        rule.sync_error = (
            "only INDIVIDUAL rooms are supported — a shared room would mix callers (spec 22)"
        )
        return

    manager = SipResourceManager()
    rule.sync_status = SyncStatus.PENDING
    await tenant.session.flush()

    trunk_ids: list[str] = []
    if rule.sip_trunk_id:
        trunk = await TenantRepository(tenant.session, tenant.tenant_id).get(
            SipTrunk, rule.sip_trunk_id
        )
        if trunk is not None and trunk.livekit_resource_id:
            trunk_ids.append(trunk.livekit_resource_id)

    if not trunk_ids:
        rule.sync_status = SyncStatus.FAILED
        rule.sync_error = "no synced SIP trunk to attach the rule to"
        rule.sync_attempts = (rule.sync_attempts or 0) + 1
        return

    if rule.livekit_resource_id:
        try:
            await manager.delete_dispatch_rule(rule.livekit_resource_id)
        except LiveKitError as exc:
            logger.info(
                "dispatch_rule_delete_before_recreate",
                extra={"rule_id": str(rule.id), "reason": str(exc)[:200]},
            )
        rule.livekit_resource_id = None

    try:
        snapshot = await manager.create_dispatch_rule(rule, livekit_trunk_ids=trunk_ids)
        rule.livekit_resource_id = snapshot.livekit_rule_id
        rule.sync_status = SyncStatus.SYNCED
        rule.last_synced_at = datetime.now(UTC)
        rule.sync_error = None
        rule.sync_attempts = 0
        logger.info(
            "dispatch_rule_synced",
            extra={"rule_id": str(rule.id), "livekit_rule_id": snapshot.livekit_rule_id},
        )
    except LiveKitError as exc:
        rule.sync_status = SyncStatus.FAILED
        rule.sync_error = str(exc)[:1000]
        rule.sync_attempts = (rule.sync_attempts or 0) + 1
        logger.error(
            "dispatch_rule_sync_failed",
            extra={"rule_id": str(rule.id), "reason": str(exc)[:300]},
        )


@router.get(
    "",
    response_model=Page[DispatchRuleResponse],
    summary="List long-lived dispatch rules",
    dependencies=[Depends(require_permission(Permission.SIP_TRUNKS_READ))],
)
async def list_rules(
    tenant: CurrentTenant,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[DispatchRuleResponse]:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    rows = await repository.list(
        LiveKitDispatchRule, limit=limit, offset=offset, order_by=LiveKitDispatchRule.name
    )
    total = await repository.count(LiveKitDispatchRule)
    return Page(
        items=[await _decorate(tenant, row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{rule_id}",
    response_model=DispatchRuleResponse,
    summary="Fetch one dispatch rule",
    dependencies=[Depends(require_permission(Permission.SIP_TRUNKS_READ))],
)
async def get_rule(rule_id: uuid.UUID, tenant: CurrentTenant) -> DispatchRuleResponse:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(LiveKitDispatchRule, rule_id)
    if row is None:
        raise _not_found(rule_id)
    return await _decorate(tenant, row)


@router.post(
    "",
    response_model=DispatchRuleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a long-lived dispatch rule and mirror it into LiveKit",
    dependencies=[Depends(require_permission(Permission.SIP_TRUNKS_WRITE))],
)
async def create_rule(
    payload: DispatchRuleCreate,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> DispatchRuleResponse:
    if payload.room_strategy is not RoomStrategy.INDIVIDUAL:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="only INDIVIDUAL rooms are supported — a shared room would mix callers (spec 22)",
        )

    repository = TenantRepository(tenant.session, tenant.tenant_id)
    clash = await tenant.session.execute(
        repository.scoped(LiveKitDispatchRule).where(LiveKitDispatchRule.name == payload.name)
    )
    if clash.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a dispatch rule named {payload.name!r} already exists",
        )

    if payload.sip_trunk_id is not None and not await repository.exists(
        SipTrunk, payload.sip_trunk_id
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="that SIP trunk does not exist in this tenant",
        )

    row = LiveKitDispatchRule(**payload.model_dump())
    repository.add(row)
    await tenant.session.flush()
    await _sync_to_livekit(tenant, row)

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="dispatch_rule.created",
        resource_type="dispatch_rule",
        resource_id=row.id,
        new_value=audit.snapshot(row, *_AUDITED),
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return await _decorate(tenant, row)


@router.put(
    "/{rule_id}",
    response_model=DispatchRuleResponse,
    summary="Update a dispatch rule and re-sync it",
    dependencies=[Depends(require_permission(Permission.SIP_TRUNKS_WRITE))],
)
async def update_rule(
    rule_id: uuid.UUID,
    payload: DispatchRuleUpdate,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> DispatchRuleResponse:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(LiveKitDispatchRule, rule_id)
    if row is None:
        raise _not_found(rule_id)

    if payload.room_strategy is not None and payload.room_strategy is not RoomStrategy.INDIVIDUAL:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="only INDIVIDUAL rooms are supported — a shared room would mix callers (spec 22)",
        )

    before = audit.snapshot(row, *_AUDITED)
    changes = payload.model_dump(exclude_unset=True)

    if "name" in changes and changes["name"] != row.name:
        clash = await tenant.session.execute(
            repository.scoped(LiveKitDispatchRule).where(
                LiveKitDispatchRule.name == changes["name"],
                LiveKitDispatchRule.id != rule_id,
            )
        )
        if clash.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"a dispatch rule named {changes['name']!r} already exists",
            )

    if "sip_trunk_id" in changes and changes["sip_trunk_id"] is not None and not await repository.exists(
        SipTrunk, changes["sip_trunk_id"]
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="that SIP trunk does not exist in this tenant",
        )

    for field, value in changes.items():
        setattr(row, field, value)

    mirrored = {
        "name",
        "sip_trunk_id",
        "room_strategy",
        "room_prefix",
        "agent_dispatch_name",
        "matched_numbers",
    }
    if mirrored & set(changes):
        await _sync_to_livekit(tenant, row)

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="dispatch_rule.updated",
        resource_type="dispatch_rule",
        resource_id=row.id,
        old_value=before,
        new_value=audit.snapshot(row, *_AUDITED),
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return await _decorate(tenant, row)


@router.post(
    "/{rule_id}/sync",
    response_model=DispatchRuleResponse,
    summary="Retry synchronisation with LiveKit",
    dependencies=[Depends(require_permission(Permission.SIP_TRUNKS_WRITE))],
)
async def sync_rule(
    rule_id: uuid.UUID,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> DispatchRuleResponse:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(LiveKitDispatchRule, rule_id)
    if row is None:
        raise _not_found(rule_id)

    await _sync_to_livekit(tenant, row)
    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="dispatch_rule.synchronized",
        resource_type="dispatch_rule",
        resource_id=row.id,
        new_value={"sync_status": row.sync_status.value, "sync_error": row.sync_error},
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return await _decorate(tenant, row)


@router.delete(
    "/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a dispatch rule",
    dependencies=[Depends(require_permission(Permission.SIP_TRUNKS_WRITE))],
)
async def delete_rule(
    rule_id: uuid.UUID,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> Response:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(LiveKitDispatchRule, rule_id)
    if row is None:
        raise _not_found(rule_id)

    before = audit.snapshot(row, *_AUDITED)
    livekit_id = row.livekit_resource_id
    if livekit_id:
        try:
            await SipResourceManager().delete_dispatch_rule(livekit_id)
        except LiveKitError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    f"the LiveKit resource could not be removed ({exc}). The rule was "
                    "not deleted, because an orphaned dispatch rule would keep "
                    "matching calls. Retry once LiveKit is reachable."
                ),
            ) from exc

    await repository.delete(LiveKitDispatchRule, rule_id)
    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="dispatch_rule.deleted",
        resource_type="dispatch_rule",
        resource_id=rule_id,
        old_value={**before, "livekit_resource_id": livekit_id},
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
