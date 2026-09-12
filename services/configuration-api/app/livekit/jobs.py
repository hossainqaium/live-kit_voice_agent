"""Background LiveKit synchronisation and repair (spec 12, 80).

HTTP handlers write PostgreSQL, mark the row ``PENDING``, commit, and return.
LiveKit work runs afterwards in a fresh session so a slow admin API cannot
block the UI. Delete stays synchronous: returning success while LiveKit still
accepts calls for a removed row is worse than a slow delete.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime

from shared.crypto import CredentialCipher, CredentialEncryptionError
from shared.logging import get_logger
from shared.models import ResourceStatus, RoomStrategy, SyncStatus
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db.models import LiveKitDispatchRule, PhoneNumber, SipCredential, SipTrunk
from app.db.session import get_session_factory
from app.livekit.errors import LiveKitError
from app.livekit.sip import SipResourceManager

logger = get_logger(__name__)

_TASKS: set[asyncio.Task[None]] = set()


def enqueue_trunk_sync(trunk_id: uuid.UUID, *, repair: bool = False) -> None:
    """Schedule trunk mirror work after the caller has committed."""
    _enqueue(_run_trunk_sync(trunk_id, repair=repair))


def enqueue_dispatch_rule_sync(rule_id: uuid.UUID, *, repair: bool = False) -> None:
    """Schedule dispatch-rule mirror work after the caller has committed."""
    _enqueue(_run_dispatch_rule_sync(rule_id, repair=repair))


def _enqueue(coro: Coroutine[object, object, None]) -> None:
    if not get_settings().livekit_background_sync:
        coro.close()
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return
    task = loop.create_task(_guarded(coro))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def _guarded(coro: Coroutine[object, object, None]) -> None:
    try:
        await coro
    except Exception:
        logger.exception("livekit_background_job_crashed")


async def _run_trunk_sync(trunk_id: uuid.UUID, *, repair: bool) -> None:
    factory = get_session_factory()
    async with factory() as session:
        trunk = await session.get(SipTrunk, trunk_id)
        if trunk is None:
            return
        await apply_trunk_sync(session, trunk, repair=repair)
        await session.commit()
        if repair and trunk.sync_status is SyncStatus.SYNCED:
            await _enqueue_dependent_rules(session, trunk.id, repair=True)


async def _run_dispatch_rule_sync(rule_id: uuid.UUID, *, repair: bool) -> None:
    factory = get_session_factory()
    async with factory() as session:
        rule = await session.get(LiveKitDispatchRule, rule_id)
        if rule is None:
            return
        await apply_dispatch_rule_sync(session, rule, repair=repair)
        await session.commit()


async def _enqueue_dependent_rules(
    session: AsyncSession, trunk_id: uuid.UUID, *, repair: bool
) -> None:
    rows = (
        await session.execute(
            select(LiveKitDispatchRule.id).where(LiveKitDispatchRule.sip_trunk_id == trunk_id)
        )
    ).scalars()
    for rule_id in rows:
        enqueue_dispatch_rule_sync(rule_id, repair=repair)


async def accepted_numbers(session: AsyncSession, trunk_id: uuid.UUID) -> list[str]:
    rows = await session.execute(
        select(PhoneNumber.number).where(
            PhoneNumber.sip_trunk_id == trunk_id,
            PhoneNumber.status == ResourceStatus.ACTIVE,
        )
    )
    return sorted(rows.scalars().all())


async def decrypt_trunk_password(session: AsyncSession, trunk: SipTrunk) -> str | None:
    """Decrypt the stored SIP password, or None when the trunk has none.

    A stored ciphertext we cannot open is an error: syncing without it would
    produce a LiveKit trunk that rejects every call while reporting SYNCED.
    """
    credential = (
        await session.execute(select(SipCredential).where(SipCredential.sip_trunk_id == trunk.id))
    ).scalar_one_or_none()
    if credential is None:
        return None
    try:
        cipher = CredentialCipher.from_env()
    except CredentialEncryptionError as exc:
        raise LiveKitError(
            f"trunk {trunk.name} has a stored SIP password but no encryption key "
            f"is available to decrypt it: {exc}"
        ) from exc
    return cipher.decrypt(bytes(credential.password_ciphertext), credential.encryption_key_version)


async def apply_trunk_sync(
    session: AsyncSession,
    trunk: SipTrunk,
    *,
    repair: bool = False,
    manager: SipResourceManager | None = None,
) -> None:
    """Push one trunk from PostgreSQL into LiveKit (spec 12).

    * **Synchronize / Retry** — update in place when an ID exists; create
      when it does not. A missing LiveKit ID is left as FAILED so Repair
      can recreate it.
    * **Repair** — delete any leftover LiveKit resource, clear the stored
      ID, and create from PostgreSQL.
    """
    manager = manager or SipResourceManager()
    trunk.sync_status = SyncStatus.PENDING
    await session.flush()

    numbers = await accepted_numbers(session, trunk.id)
    try:
        password = await decrypt_trunk_password(session, trunk)
    except LiveKitError as exc:
        _mark_failed(trunk, exc)
        return

    try:
        if repair and trunk.livekit_resource_id:
            try:
                await manager.delete_inbound_trunk(trunk.livekit_resource_id)
            except LiveKitError as exc:
                logger.info(
                    "trunk_repair_delete",
                    extra={"trunk_id": str(trunk.id), "reason": str(exc)[:200]},
                )
            trunk.livekit_resource_id = None

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
        _mark_failed(trunk, exc)


async def apply_dispatch_rule_sync(
    session: AsyncSession,
    rule: LiveKitDispatchRule,
    *,
    repair: bool = False,
    manager: SipResourceManager | None = None,
) -> None:
    """Push one dispatch rule from PostgreSQL into LiveKit (spec 12).

    LiveKit has no reliable in-place update, so Synchronize already
    delete-and-recreates. Repair does the same after clearing a leftover ID.
    """
    if rule.room_strategy is not RoomStrategy.INDIVIDUAL:
        rule.sync_status = SyncStatus.FAILED
        rule.sync_error = (
            "only INDIVIDUAL rooms are supported — a shared room would mix callers (spec 22)"
        )
        return

    manager = manager or SipResourceManager()
    rule.sync_status = SyncStatus.PENDING
    await session.flush()

    trunk_ids: list[str] = []
    if rule.sip_trunk_id:
        trunk = await session.get(SipTrunk, rule.sip_trunk_id)
        if trunk is not None and trunk.livekit_resource_id:
            trunk_ids.append(trunk.livekit_resource_id)

    if not trunk_ids:
        rule.sync_status = SyncStatus.FAILED
        rule.sync_error = "no synced SIP trunk to attach the rule to"
        rule.sync_attempts = (rule.sync_attempts or 0) + 1
        return

    livekit_id = rule.livekit_resource_id
    if livekit_id:
        try:
            await manager.delete_dispatch_rule(livekit_id)
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
        _mark_failed(rule, exc)


def _mark_failed(row: SipTrunk | LiveKitDispatchRule, exc: LiveKitError) -> None:
    row.sync_status = SyncStatus.FAILED
    row.sync_error = str(exc)[:1000]
    row.sync_attempts = (row.sync_attempts or 0) + 1
    logger.error(
        "livekit_sync_failed",
        extra={"resource_id": str(row.id), "reason": str(exc)[:300]},
    )
