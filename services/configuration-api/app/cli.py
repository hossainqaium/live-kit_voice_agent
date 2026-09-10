"""Administrative CLI.

Development and operational commands. Deliberately not a substitute for the
API: spec 77 requires a tenant administrator to complete the whole onboarding
flow through the UI. This exists for platform bootstrap and for Phase 1, before
that UI is built.

    python -m app.cli seed-platform
    python -m app.cli seed-dev-tenant --did 1001 --pbx-host 192.168.0.113
    python -m app.cli sync-livekit
    python -m app.cli show-config
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.settings import get_settings
from app.db.models import LiveKitDispatchRule, PhoneNumber, SipCredential, SipTrunk
from app.db.session import dispose_engine, get_session_factory
from app.livekit import LiveKitError, LiveKitUnsupportedError, SipResourceManager
from app.services.seed import (
    DevTenantSpec,
    seed_dev_tenant,
    seed_provider_catalog,
    seed_roles_and_permissions,
)
from shared.crypto import CredentialCipher, CredentialEncryptionError
from shared.logging import configure_logging, get_logger
from shared.models import SyncStatus

settings = get_settings()
configure_logging(service="configuration-api-cli", level=settings.log_level)
logger = get_logger(__name__)


async def cmd_seed_platform() -> int:
    """Seed roles, permissions and the provider catalog."""
    factory = get_session_factory()
    async with factory() as session:
        await seed_roles_and_permissions(session)
        await seed_provider_catalog(session, DevTenantSpec())
        await session.commit()
    print("platform catalog seeded")
    return 0


async def cmd_seed_dev_tenant(args: argparse.Namespace) -> int:
    """Seed one development tenant, configured end to end."""
    if settings.is_production:
        print("refusing to seed a development tenant in production", file=sys.stderr)
        return 2

    spec = DevTenantSpec(
        did=args.did,
        pbx_host=args.pbx_host,
        speech_base_url=args.speech_base_url,
        llm_provider_slug=args.llm_provider,
        llm_model=args.llm_model,
        agent_dispatch_name=args.agent_name,
    )

    factory = get_session_factory()
    async with factory() as session:
        await seed_roles_and_permissions(session)
        await seed_provider_catalog(session, spec)
        seeded = await seed_dev_tenant(session, spec)
        await session.commit()

    print("development tenant seeded")
    print(f"  tenant           {seeded.tenant_id}")
    print(f"  agent            {seeded.agent_id}")
    print(f"  agent version    {seeded.agent_version_id}")
    print(f"  sip trunk        {seeded.sip_trunk_id}")
    print(f"  dispatch rule    {seeded.dispatch_rule_id}")
    print(f"  agent number     {seeded.did}   (the number a caller dials)")
    print()

    if seeded.sip_auth_password:
        # Printed once. The stored copy is encrypted and unreadable (spec 54),
        # so this is the only opportunity to capture it.
        print("  SIP trunk credentials — shown once, not recoverable:")
        print(f"    username       {seeded.sip_auth_username}")
        print(f"    password       {seeded.sip_auth_password}")
    else:
        print(
            f"  SIP trunk username {seeded.sip_auth_username} "
            "(password already provisioned; not recoverable)"
        )
    print()
    print("next:")
    print("  1. python -m app.cli sync-livekit")
    print("  2. from the PBX, dial the agent number:")
    print(
        f'     fs_cli -x "originate {{origination_caller_id_number=15550001111,'
        f"sip_auth_username={seeded.sip_auth_username},"
        f"sip_auth_password=<password>}}"
        f'sofia/external/sip:{seeded.did}@<this-host>:5060 &park"'
    )
    return 0


async def cmd_sync_livekit() -> int:
    """Create or verify the LiveKit resources our records describe.

    Separate from seeding so that writing to the database and mutating a media
    server are never the same action. Idempotent: a row already SYNCED whose
    resource still exists in LiveKit is left alone (spec 12).
    """
    manager = SipResourceManager()
    factory = get_session_factory()
    failures = 0

    try:
        cipher: CredentialCipher | None = CredentialCipher.from_env()
    except CredentialEncryptionError as exc:
        # Trunks without a credential still sync; ones with a credential will
        # be reported rather than silently created unauthenticated.
        cipher = None
        print(f"note: no encryption key available ({exc})", file=sys.stderr)

    async with factory() as session:
        trunks = (await session.execute(select(SipTrunk))).scalars().all()

        for trunk in trunks:
            auth_password = await _trunk_password(session, cipher, trunk)
            numbers = [
                number
                for (number,) in (
                    await session.execute(
                        select(PhoneNumber.number).where(PhoneNumber.sip_trunk_id == trunk.id)
                    )
                ).all()
            ]

            if trunk.livekit_resource_id:
                try:
                    snapshot = await manager.get_inbound_trunk(trunk.livekit_resource_id)

                    if manager.trunk_matches(snapshot, trunk, numbers):
                        trunk.sync_status = SyncStatus.SYNCED
                        trunk.last_synced_at = datetime.now(UTC)
                        trunk.sync_error = None
                        print(f"  trunk {trunk.name}: already synced")
                        continue

                    # Exists but differs. Reshaping in place is preferred,
                    # because recreating changes the LiveKit trunk ID and
                    # invalidates every dispatch rule pointing at it.
                    try:
                        await manager.update_inbound_trunk(
                            trunk.livekit_resource_id,
                            trunk,
                            numbers=numbers,
                            auth_password=auth_password,
                        )
                        trunk.sync_status = SyncStatus.SYNCED
                        trunk.last_synced_at = datetime.now(UTC)
                        trunk.sync_error = None
                        print(f"  trunk {trunk.name}: updated to match our record")
                        continue
                    except LiveKitUnsupportedError:
                        # This LiveKit build has no UpdateSIPInboundTrunk
                        # route. Delete and recreate instead, which also means
                        # the dependent dispatch rules must be rebuilt: a
                        # create with the same number would otherwise be
                        # refused as a conflict, and the rules would still
                        # reference a trunk that no longer exists.
                        print(
                            f"  trunk {trunk.name}: this LiveKit build cannot update "
                            "a trunk in place; recreating"
                        )
                        await _release_trunk_and_rules(session, manager, trunk)
                except LiveKitError as exc:
                    # Present here, absent there: the drift case (spec 46).
                    # Recreate from PostgreSQL, which is the source of truth.
                    print(f"  trunk {trunk.name}: drifted ({exc}); recreating")
                    trunk.sync_status = SyncStatus.DRIFTED

            try:
                snapshot = await manager.create_inbound_trunk(
                    trunk, numbers=numbers, auth_password=auth_password
                )
                trunk.livekit_resource_id = snapshot.livekit_trunk_id
                trunk.sync_status = SyncStatus.SYNCED
                trunk.last_synced_at = datetime.now(UTC)
                trunk.sync_error = None
                trunk.sync_attempts = 0
                print(f"  trunk {trunk.name}: created {snapshot.livekit_trunk_id}")
            except LiveKitError as exc:
                trunk.sync_status = SyncStatus.FAILED
                trunk.sync_error = str(exc)
                trunk.sync_attempts += 1
                failures += 1
                print(f"  trunk {trunk.name}: FAILED {exc}", file=sys.stderr)

        await session.flush()

        rules = (await session.execute(select(LiveKitDispatchRule))).scalars().all()

        for rule in rules:
            trunk_resource_ids: list[str] = []
            if rule.sip_trunk_id:
                # Distinct name: `trunk` is the loop variable above, and
                # shadowing it here made the type checker infer the wrong type.
                attached_trunk = (
                    await session.execute(select(SipTrunk).where(SipTrunk.id == rule.sip_trunk_id))
                ).scalar_one_or_none()
                if attached_trunk and attached_trunk.livekit_resource_id:
                    trunk_resource_ids.append(attached_trunk.livekit_resource_id)

            if not trunk_resource_ids:
                # A rule with no synced trunk would match nothing, so this is
                # reported rather than created.
                rule.sync_status = SyncStatus.FAILED
                rule.sync_error = "no synced SIP trunk to attach the rule to"
                failures += 1
                print(f"  rule {rule.name}: FAILED no synced trunk", file=sys.stderr)
                continue

            if rule.livekit_resource_id:
                try:
                    await manager.get_dispatch_rule(rule.livekit_resource_id)
                    rule.sync_status = SyncStatus.SYNCED
                    rule.last_synced_at = datetime.now(UTC)
                    rule.sync_error = None
                    print(f"  rule {rule.name}: already synced")
                    continue
                except LiveKitError as exc:
                    print(f"  rule {rule.name}: drifted ({exc}); recreating")
                    rule.sync_status = SyncStatus.DRIFTED

            try:
                rule_snapshot = await manager.create_dispatch_rule(
                    rule, livekit_trunk_ids=trunk_resource_ids
                )
                rule.livekit_resource_id = rule_snapshot.livekit_rule_id
                rule.sync_status = SyncStatus.SYNCED
                rule.last_synced_at = datetime.now(UTC)
                rule.sync_error = None
                rule.sync_attempts = 0
                print(f"  rule {rule.name}: created {rule_snapshot.livekit_rule_id}")
            except LiveKitError as exc:
                rule.sync_status = SyncStatus.FAILED
                rule.sync_error = str(exc)
                rule.sync_attempts += 1
                failures += 1
                print(f"  rule {rule.name}: FAILED {exc}", file=sys.stderr)

        await session.commit()

    if failures:
        print(f"\n{failures} resource(s) failed to sync", file=sys.stderr)
        return 1

    print("\nall LiveKit resources synced")
    return 0


async def _trunk_password(session, cipher, trunk) -> str | None:
    """Decrypt a trunk's SIP password, if it has one.

    Returns None for a trunk with no credential, which is valid when the trunk
    is restricted by source address instead.
    """
    if not trunk.auth_username:
        return None

    credential = (
        await session.execute(select(SipCredential).where(SipCredential.sip_trunk_id == trunk.id))
    ).scalar_one_or_none()

    if credential is None:
        print(
            f"  trunk {trunk.name}: has auth_username but no stored password",
            file=sys.stderr,
        )
        return None

    if cipher is None:
        raise LiveKitError(
            f"trunk {trunk.name} has a stored SIP password but no encryption key "
            "is available to decrypt it"
        )

    password: str = cipher.decrypt(
        bytes(credential.password_ciphertext), credential.encryption_key_version
    )
    return password


async def _release_trunk_and_rules(session, manager, trunk) -> None:
    """Delete a LiveKit trunk and the rules that reference it.

    Both must go: LiveKit refuses a second trunk claiming the same number, and
    a dispatch rule left pointing at a deleted trunk silently matches nothing.
    Our rows keep their identity — only the mirrored LiveKit IDs are cleared,
    so the next pass recreates both from PostgreSQL.
    """
    dependent = (
        (
            await session.execute(
                select(LiveKitDispatchRule).where(LiveKitDispatchRule.sip_trunk_id == trunk.id)
            )
        )
        .scalars()
        .all()
    )

    for rule in dependent:
        if rule.livekit_resource_id:
            try:
                await manager.delete_dispatch_rule(rule.livekit_resource_id)
            except LiveKitError as exc:
                # Already gone is the outcome we wanted.
                print(f"    rule {rule.name}: delete reported {exc}")
        rule.livekit_resource_id = None
        rule.sync_status = SyncStatus.PENDING

    try:
        await manager.delete_inbound_trunk(trunk.livekit_resource_id)
    except LiveKitError as exc:
        print(f"    trunk {trunk.name}: delete reported {exc}")

    trunk.livekit_resource_id = None
    trunk.sync_status = SyncStatus.PENDING
    await session.flush()


async def cmd_show_config() -> int:
    """Print what LiveKit currently holds, next to what we recorded."""
    manager = SipResourceManager()

    print("LiveKit inbound trunks:")
    for snapshot in await manager.list_inbound_trunks():
        print(
            f"  {snapshot.livekit_trunk_id}  {snapshot.name}  "
            f"numbers={list(snapshot.numbers)}  allowed={list(snapshot.allowed_addresses)}"
        )

    print("\nLiveKit dispatch rules:")
    for rule_snapshot in await manager.list_dispatch_rules():
        print(
            f"  {rule_snapshot.livekit_rule_id}  {rule_snapshot.name}  "
            f"trunks={list(rule_snapshot.trunk_ids)}  prefix={rule_snapshot.room_prefix}  "
            f"agents={list(rule_snapshot.agent_names)}"
        )

    factory = get_session_factory()
    async with factory() as session:
        print("\nour records:")
        for trunk in (await session.execute(select(SipTrunk))).scalars():
            print(
                f"  trunk {trunk.name}: livekit={trunk.livekit_resource_id} "
                f"status={trunk.sync_status}"
            )
        for rule in (await session.execute(select(LiveKitDispatchRule))).scalars():
            print(
                f"  rule  {rule.name}: livekit={rule.livekit_resource_id} "
                f"status={rule.sync_status} agent={rule.agent_dispatch_name}"
            )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="app.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed-platform", help="seed roles, permissions and the provider catalog")

    dev = sub.add_parser("seed-dev-tenant", help="seed one development tenant end to end")
    dev.add_argument("--did", default="1001", help="DID the PBX will dial")
    dev.add_argument("--pbx-host", default="192.168.0.113", help="PBX address")
    dev.add_argument(
        "--speech-base-url",
        default="http://host.docker.internal:8010/v1",
        help="OpenAI-compatible speech endpoint, reachable from the worker container",
    )
    dev.add_argument(
        "--llm-provider",
        default="echo_dev",
        help="LLM adapter slug; echo_dev needs no credential",
    )
    dev.add_argument("--llm-model", default="echo", help="LLM model slug")
    dev.add_argument(
        "--agent-name",
        default="voice-agent",
        help="agent dispatch name; must match the worker's WORKER_AGENT_NAME",
    )

    sub.add_parser("sync-livekit", help="create or verify LiveKit SIP resources")
    sub.add_parser("show-config", help="compare LiveKit against our records")

    args = parser.parse_args()

    async def run() -> int:
        try:
            if args.command == "seed-platform":
                return await cmd_seed_platform()
            if args.command == "seed-dev-tenant":
                return await cmd_seed_dev_tenant(args)
            if args.command == "sync-livekit":
                return await cmd_sync_livekit()
            if args.command == "show-config":
                return await cmd_show_config()
            parser.error(f"unknown command {args.command}")
            return 2
        finally:
            await dispose_engine()

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
