"""SIP trunk endpoints (spec 15, 12, 67).

A trunk exists in two places: as a row here, which is the source of truth
(spec 10), and as a LiveKit SIP resource, which is what actually accepts calls.
Every write therefore has two halves, and they can disagree — so the row
records its own synchronisation state and the API never pretends a failed sync
succeeded.

The ordering is deliberate: **PostgreSQL first, LiveKit second.** A row marked
`PENDING` with no LiveKit resource is recoverable by retrying. A LiveKit
resource with no row is an orphan nobody knows about.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select

from app.core.dependencies import ClientIp, CurrentTenant, require_permission
from app.db.models import PhoneNumber, SipCredential, SipTrunk
from app.db.repository import TenantRepository
from app.livekit import LiveKitError, SipResourceManager
from app.schemas.common import Page
from app.schemas.telephony import (
    SipTrunkCreate,
    SipTrunkResponse,
    SipTrunkUpdate,
)
from app.services import audit
from app.services.connection_test import probe_sip_endpoint
from shared.crypto import CredentialCipher, CredentialEncryptionError
from shared.logging import get_logger
from shared.models import ConnectionTestResult, Permission, ResourceStatus, SyncStatus

logger = get_logger(__name__)

router = APIRouter(prefix="/sip-trunks", tags=["sip-trunks"])

_AUDITED = (
    "name",
    "pbx_id",
    "direction",
    "sip_host",
    "port",
    "transport",
    "allowed_ips",
    "auth_username",
    "media_encryption_required",
    "codecs",
    "dtmf_mode",
    "status",
)


def _not_found(trunk_id: uuid.UUID) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail=f"no SIP trunk with id {trunk_id}"
    )


async def _to_response(
    trunk: SipTrunk, *, has_credential: bool, accepted_numbers: list[str] | None = None
) -> SipTrunkResponse:
    payload = SipTrunkResponse.model_validate(trunk, from_attributes=True)
    return payload.model_copy(
        update={
            "has_credential": has_credential,
            "accepted_numbers": accepted_numbers or [],
        }
    )


async def _credential_map(tenant: CurrentTenant, trunk_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Which trunks have a password configured.

    One query for the page rather than one per row, and it selects only the
    foreign key — the ciphertext is never loaded to answer "is one set?".
    """
    if not trunk_ids:
        return set()
    rows = await tenant.session.execute(
        select(SipCredential.sip_trunk_id).where(
            SipCredential.tenant_id == tenant.tenant_id,
            SipCredential.sip_trunk_id.in_(trunk_ids),
        )
    )
    return set(rows.scalars().all())


@router.get(
    "",
    response_model=Page[SipTrunkResponse],
    summary="List SIP trunks",
    dependencies=[Depends(require_permission(Permission.SIP_TRUNKS_READ))],
)
async def list_trunks(
    tenant: CurrentTenant,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[SipTrunkResponse]:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    rows = await repository.list(SipTrunk, limit=limit, offset=offset, order_by=SipTrunk.name)
    total = await repository.count(SipTrunk)
    with_credentials = await _credential_map(tenant, [row.id for row in rows])
    return Page(
        items=[
            await _to_response(
                row,
                has_credential=row.id in with_credentials,
                accepted_numbers=await _accepted_numbers(tenant, row.id),
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{trunk_id}",
    response_model=SipTrunkResponse,
    summary="Fetch one SIP trunk",
    dependencies=[Depends(require_permission(Permission.SIP_TRUNKS_READ))],
)
async def get_trunk(trunk_id: uuid.UUID, tenant: CurrentTenant) -> SipTrunkResponse:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(SipTrunk, trunk_id)
    if row is None:
        raise _not_found(trunk_id)
    credentials = await _credential_map(tenant, [row.id])
    return await _to_response(
        row,
        has_credential=bool(credentials),
        accepted_numbers=await _accepted_numbers(tenant, row.id),
    )


async def _store_password(
    tenant: CurrentTenant, trunk: SipTrunk, username: str | None, password: str
) -> None:
    """Encrypt and store a trunk password (spec 53).

    Kept in its own table so that listing trunks never loads ciphertext, and
    so reading a secret is a distinct, auditable query rather than a side
    effect of rendering a table.
    """
    try:
        cipher = CredentialCipher.from_env()
    except CredentialEncryptionError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "credential encryption is not configured, so a password cannot be "
                f"stored safely: {exc}"
            ),
        ) from exc

    encrypted = cipher.encrypt(password)
    existing = (
        await tenant.session.execute(
            select(SipCredential).where(SipCredential.sip_trunk_id == trunk.id)
        )
    ).scalar_one_or_none()

    if existing is None:
        tenant.session.add(
            SipCredential(
                tenant_id=tenant.tenant_id,
                sip_trunk_id=trunk.id,
                username=username or trunk.auth_username or "",
                password_ciphertext=encrypted.ciphertext,
                encryption_key_version=encrypted.key_version,
            )
        )
    else:
        existing.username = username or trunk.auth_username or existing.username
        existing.password_ciphertext = encrypted.ciphertext
        existing.encryption_key_version = encrypted.key_version
        existing.rotated_at = datetime.now(UTC)


async def _accepted_numbers(tenant: CurrentTenant, trunk_id: uuid.UUID) -> list[str]:
    """The DIDs assigned to a trunk.

    This is what LiveKit matches an inbound call against. Read from the
    phone_numbers table rather than duplicated onto the trunk, so the two
    cannot disagree: assigning a number to a trunk is the single action that
    changes what the trunk accepts.
    """
    rows = await tenant.session.execute(
        select(PhoneNumber.number).where(
            PhoneNumber.tenant_id == tenant.tenant_id,
            PhoneNumber.sip_trunk_id == trunk_id,
            PhoneNumber.status == ResourceStatus.ACTIVE,
        )
    )
    return sorted(rows.scalars().all())


async def _sync_to_livekit(tenant: CurrentTenant, trunk: SipTrunk, password: str | None) -> None:
    """Create or update the LiveKit SIP resource for a trunk (spec 12).

    A failure is recorded on the row rather than raised: the configuration is
    saved, the mirror is not, and the operator gets a `FAILED` state with the
    reason and a Retry action. Raising would roll back a valid configuration
    change because a separate system was briefly unavailable.
    """
    manager = SipResourceManager()

    trunk.sync_status = SyncStatus.PENDING
    await tenant.session.flush()

    numbers = await _accepted_numbers(tenant, trunk.id)

    try:
        if trunk.livekit_resource_id:
            snapshot = await manager.update_inbound_trunk(
                trunk.livekit_resource_id, trunk, numbers=numbers, auth_password=password
            )
        else:
            snapshot = await manager.create_inbound_trunk(
                trunk, numbers=numbers, auth_password=password
            )
        trunk.livekit_resource_id = snapshot.livekit_trunk_id
        trunk.sync_status = SyncStatus.SYNCED
        trunk.last_synced_at = datetime.now(UTC)
        trunk.sync_error = None
        trunk.sync_attempts = 0
        logger.info(
            "trunk_synced",
            extra={"trunk_id": str(trunk.id), "livekit_trunk_id": snapshot.livekit_trunk_id},
        )
    except LiveKitError as exc:
        trunk.sync_status = SyncStatus.FAILED
        trunk.sync_error = str(exc)[:1000]
        trunk.sync_attempts = (trunk.sync_attempts or 0) + 1
        logger.error(
            "trunk_sync_failed",
            extra={"trunk_id": str(trunk.id), "reason": str(exc)[:300]},
        )


@router.post(
    "",
    response_model=SipTrunkResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a SIP trunk and mirror it into LiveKit",
    dependencies=[Depends(require_permission(Permission.SIP_TRUNKS_WRITE))],
)
async def create_trunk(
    payload: SipTrunkCreate,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> SipTrunkResponse:
    repository = TenantRepository(tenant.session, tenant.tenant_id)

    clash = await tenant.session.execute(
        repository.scoped(SipTrunk).where(SipTrunk.name == payload.name)
    )
    if clash.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a SIP trunk named {payload.name!r} already exists",
        )

    if payload.pbx_id is not None:
        from app.db.models import Pbx

        if not await repository.exists(Pbx, payload.pbx_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="that PBX does not exist in this tenant",
            )

    fields = payload.model_dump(exclude={"auth_password"})
    row = SipTrunk(**fields)
    repository.add(row)
    await tenant.session.flush()

    if payload.auth_password:
        await _store_password(tenant, row, payload.auth_username, payload.auth_password)

    await _sync_to_livekit(tenant, row, payload.auth_password)

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="trunk.created",
        resource_type="sip_trunk",
        resource_id=row.id,
        new_value=audit.snapshot(row, *_AUDITED),
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()

    return await _to_response(
        row,
        has_credential=bool(payload.auth_password),
        accepted_numbers=await _accepted_numbers(tenant, row.id),
    )


@router.put(
    "/{trunk_id}",
    response_model=SipTrunkResponse,
    summary="Update a SIP trunk and re-sync it",
    dependencies=[Depends(require_permission(Permission.SIP_TRUNKS_WRITE))],
)
async def update_trunk(
    trunk_id: uuid.UUID,
    payload: SipTrunkUpdate,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> SipTrunkResponse:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(SipTrunk, trunk_id)
    if row is None:
        raise _not_found(trunk_id)

    before = audit.snapshot(row, *_AUDITED)
    changes = payload.model_dump(exclude_unset=True, exclude={"auth_password"})

    if "name" in changes and changes["name"] != row.name:
        clash = await tenant.session.execute(
            repository.scoped(SipTrunk).where(
                SipTrunk.name == changes["name"], SipTrunk.id != trunk_id
            )
        )
        if clash.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"a SIP trunk named {changes['name']!r} already exists",
            )

    if any(field in changes for field in ("sip_host", "port", "transport")):
        row.last_test_result = ConnectionTestResult.UNTESTED
        row.last_tested_at = None
        row.last_test_detail = "reset because the address changed"

    for field, value in changes.items():
        setattr(row, field, value)

    if payload.auth_password:
        await _store_password(tenant, row, payload.auth_username, payload.auth_password)

    # Anything LiveKit mirrors has changed, so re-sync. A name-only change
    # still matters: the LiveKit resource carries the name too, and leaving it
    # stale makes the two systems disagree in the admin view.
    mirrored = {"name", "allowed_ips", "auth_username", "media_encryption_required"}
    if mirrored & set(changes) or payload.auth_password:
        await _sync_to_livekit(tenant, row, payload.auth_password)

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="trunk.updated",
        resource_type="sip_trunk",
        resource_id=row.id,
        old_value=before,
        new_value=audit.snapshot(row, *_AUDITED),
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()

    credentials = await _credential_map(tenant, [row.id])
    return await _to_response(
        row,
        has_credential=bool(credentials),
        accepted_numbers=await _accepted_numbers(tenant, row.id),
    )


@router.post(
    "/{trunk_id}/sync",
    response_model=SipTrunkResponse,
    summary="Retry synchronisation with LiveKit",
    dependencies=[Depends(require_permission(Permission.SIP_TRUNKS_WRITE))],
)
async def sync_trunk(
    trunk_id: uuid.UUID,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> SipTrunkResponse:
    """Re-create or refresh the LiveKit resource (spec 12 Synchronize/Retry).

    Note what cannot be done here: the stored password is encrypted and is
    *not* decrypted to re-sync. LiveKit keeps the credential it was given, so a
    retry after a failed create needs the password supplied again through an
    update. Silently syncing without it would produce a trunk that rejects
    every call while reporting SYNCED.
    """
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(SipTrunk, trunk_id)
    if row is None:
        raise _not_found(trunk_id)

    await _sync_to_livekit(tenant, row, None)

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="trunk.synchronized",
        resource_type="sip_trunk",
        resource_id=row.id,
        new_value={"sync_status": row.sync_status.value, "sync_error": row.sync_error},
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()

    credentials = await _credential_map(tenant, [row.id])
    return await _to_response(
        row,
        has_credential=bool(credentials),
        accepted_numbers=await _accepted_numbers(tenant, row.id),
    )


@router.post(
    "/{trunk_id}/test",
    summary="Test connectivity to the trunk's SIP host",
    dependencies=[Depends(require_permission(Permission.SIP_TRUNKS_READ))],
)
async def test_trunk(
    trunk_id: uuid.UUID,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> dict:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(SipTrunk, trunk_id)
    if row is None:
        raise _not_found(trunk_id)

    outcome = await probe_sip_endpoint(host=row.sip_host, port=row.port, transport=row.transport)
    row.last_test_result = outcome.result
    row.last_tested_at = datetime.now(UTC)
    row.last_test_detail = outcome.detail

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="trunk.tested",
        resource_type="sip_trunk",
        resource_id=row.id,
        new_value={"result": outcome.result.value, "detail": outcome.detail},
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()

    return {
        "result": outcome.result.value,
        "detail": outcome.detail,
        "tested_at": row.last_tested_at.isoformat(),
        "latency_ms": outcome.latency_ms,
    }


@router.post(
    "/{trunk_id}/enable",
    response_model=SipTrunkResponse,
    summary="Enable a trunk",
    dependencies=[Depends(require_permission(Permission.SIP_TRUNKS_WRITE))],
)
async def enable_trunk(
    trunk_id: uuid.UUID, tenant: CurrentTenant, request: Request, client_ip: ClientIp
) -> SipTrunkResponse:
    return await _set_status(
        trunk_id, ResourceStatus.ACTIVE, tenant, request, client_ip, "trunk.enabled"
    )


@router.post(
    "/{trunk_id}/disable",
    response_model=SipTrunkResponse,
    summary="Disable a trunk",
    dependencies=[Depends(require_permission(Permission.SIP_TRUNKS_WRITE))],
)
async def disable_trunk(
    trunk_id: uuid.UUID, tenant: CurrentTenant, request: Request, client_ip: ClientIp
) -> SipTrunkResponse:
    return await _set_status(
        trunk_id, ResourceStatus.DISABLED, tenant, request, client_ip, "trunk.disabled"
    )


async def _set_status(
    trunk_id: uuid.UUID,
    new_status: ResourceStatus,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
    action: str,
) -> SipTrunkResponse:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(SipTrunk, trunk_id)
    if row is None:
        raise _not_found(trunk_id)

    row.status = new_status
    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action=action,
        resource_type="sip_trunk",
        resource_id=row.id,
        new_value={"status": new_status.value},
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()

    credentials = await _credential_map(tenant, [row.id])
    return await _to_response(
        row,
        has_credential=bool(credentials),
        accepted_numbers=await _accepted_numbers(tenant, row.id),
    )


@router.delete(
    "/{trunk_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a SIP trunk",
    dependencies=[Depends(require_permission(Permission.SIP_TRUNKS_WRITE))],
)
async def delete_trunk(
    trunk_id: uuid.UUID,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> Response:
    """Delete a trunk and its LiveKit resource.

    Refused while phone numbers reference it, for the same reason as the PBX:
    the foreign key is SET NULL, so deleting would leave a DID that routes
    nowhere and fails at the next call rather than at the mistake.
    """
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(SipTrunk, trunk_id)
    if row is None:
        raise _not_found(trunk_id)

    numbers = await tenant.session.execute(
        repository.scoped(PhoneNumber).where(PhoneNumber.sip_trunk_id == trunk_id)
    )
    in_use = [number.number for number in numbers.scalars().all()]
    if in_use:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "this trunk is still used by phone number(s): "
                f"{', '.join(sorted(in_use))}. Reassign or remove them first."
            ),
        )

    before = audit.snapshot(row, *_AUDITED)
    livekit_id = row.livekit_resource_id

    # Remove the mirror first. An orphaned LiveKit trunk would keep accepting
    # calls for a configuration that no longer exists, which is worse than a
    # row whose mirror is already gone.
    if livekit_id:
        manager = SipResourceManager()
        try:
            await manager.delete_inbound_trunk(livekit_id)
        except LiveKitError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    f"the LiveKit resource could not be removed ({exc}). The trunk was "
                    "not deleted, because leaving an orphaned LiveKit trunk accepting "
                    "calls would be worse. Retry once LiveKit is reachable."
                ),
            ) from exc

    await repository.delete(SipTrunk, trunk_id)

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="trunk.deleted",
        resource_type="sip_trunk",
        resource_id=trunk_id,
        old_value={**before, "livekit_resource_id": livekit_id},
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
