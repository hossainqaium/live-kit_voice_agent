"""Administrative CLI.

Development and operational commands. Deliberately not a substitute for the
API: spec 77 requires a tenant administrator to complete the whole onboarding
flow through the UI. This exists for platform bootstrap and for Phase 1, before
that UI is built.

    python -m app.cli seed-platform
    python -m app.cli seed-dev-tenant --did 1001 --pbx-host 192.168.0.113
    python -m app.cli sync-livekit
    python -m app.cli detect-drift
    python -m app.cli show-config
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.core.security import hash_password
from app.core.settings import get_settings
from app.db.models import (
    Agent,
    AgentVersion,
    LiveKitDispatchRule,
    Model,
    Permission,
    PhoneNumber,
    Provider,
    ProviderCredential,
    Role,
    RolePermission,
    SipCredential,
    SipTrunk,
    Tenant,
    User,
    UserRole,
)
from app.db.session import dispose_engine, get_session_factory
from app.livekit import (
    LiveKitAdminClient,
    LiveKitError,
    LiveKitUnsupportedError,
    SipResourceManager,
)
from app.services.seed import (
    DevTenantSpec,
    seed_dev_tenant,
    seed_provider_catalog,
    seed_roles_and_permissions,
)
from shared.crypto import CredentialCipher, CredentialEncryptionError
from shared.logging import configure_logging, get_logger
from shared.models import (
    PlatformRole,
    ProviderKind,
    ResourceStatus,
    SyncStatus,
    TenantRole,
)

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


async def cmd_set_credential(args: argparse.Namespace) -> int:
    """Store a provider credential, encrypted (spec 53, 54).

    The key is read from stdin rather than an argument, so it never appears in
    a process listing, a shell history file, or this command's own logs. The
    stored value is ciphertext; only a short hint is kept for display.
    """
    import sys as _sys

    api_key = _sys.stdin.read().strip()
    if not api_key:
        print('no key on stdin; pipe it in, e.g. printf %s "$KEY" | ... ', file=sys.stderr)
        return 2

    try:
        cipher = CredentialCipher.from_env()
    except CredentialEncryptionError as exc:
        print(f"cannot encrypt: {exc}", file=sys.stderr)
        return 2

    factory = get_session_factory()
    async with factory() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == args.tenant))
        ).scalar_one_or_none()
        if tenant is None:
            print(f"no tenant with slug {args.tenant!r}", file=sys.stderr)
            return 2

        provider = (
            await session.execute(
                select(Provider).where(
                    Provider.kind == ProviderKind(args.kind),
                    Provider.slug == args.provider,
                )
            )
        ).scalar_one_or_none()
        if provider is None:
            print(
                f"no {args.kind} provider with slug {args.provider!r} in the catalog; "
                "run seed-platform first",
                file=sys.stderr,
            )
            return 2

        encrypted = cipher.encrypt(api_key)
        existing = (
            await session.execute(
                select(ProviderCredential).where(
                    ProviderCredential.tenant_id == tenant.id,
                    ProviderCredential.provider_id == provider.id,
                    ProviderCredential.label == args.label,
                )
            )
        ).scalar_one_or_none()

        if existing is None:
            session.add(
                ProviderCredential(
                    tenant_id=tenant.id,
                    provider_id=provider.id,
                    label=args.label,
                    api_key_ciphertext=encrypted.ciphertext,
                    encryption_key_version=encrypted.key_version,
                    key_hint=CredentialCipher.hint(api_key),
                    base_url=args.base_url,
                )
            )
            action = "stored"
        else:
            existing.api_key_ciphertext = encrypted.ciphertext
            existing.encryption_key_version = encrypted.key_version
            existing.key_hint = CredentialCipher.hint(api_key)
            existing.base_url = args.base_url
            existing.rotated_at = datetime.now(UTC)
            action = "rotated"

        await session.commit()

    print(
        f"credential {action}: tenant={args.tenant} {args.kind}/{args.provider} "
        f"label={args.label} key=***{CredentialCipher.hint(api_key)} "
        f"base_url={args.base_url or 'provider default'}"
    )
    return 0


async def cmd_set_agent_provider(args: argparse.Namespace) -> int:
    """Point an agent version's STT, LLM or TTS at a different provider.

    Configuration, not code: switching a tenant between a self-hosted model
    and a hosted one is a row update (spec 9, 25).
    """
    factory = get_session_factory()
    async with factory() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == args.tenant))
        ).scalar_one_or_none()
        if tenant is None:
            print(f"no tenant with slug {args.tenant!r}", file=sys.stderr)
            return 2

        provider = (
            await session.execute(
                select(Provider).where(
                    Provider.kind == ProviderKind(args.kind),
                    Provider.slug == args.provider,
                )
            )
        ).scalar_one_or_none()
        if provider is None:
            print(f"no {args.kind} provider {args.provider!r}", file=sys.stderr)
            return 2

        model = (
            await session.execute(
                select(Model).where(Model.provider_id == provider.id, Model.slug == args.model)
            )
        ).scalar_one_or_none()
        if model is None:
            # Registering it is the right move rather than failing: the catalog
            # is data, and a provider ships new models constantly.
            model = Model(
                provider_id=provider.id,
                slug=args.model,
                display_name=args.model,
            )
            session.add(model)
            await session.flush()
            print(f"  registered model {args.model} in the catalog")

        version = (
            (
                await session.execute(
                    select(AgentVersion)
                    .join(Agent, Agent.id == AgentVersion.agent_id)
                    .where(Agent.tenant_id == tenant.id, Agent.name == args.agent)
                    .order_by(AgentVersion.version_number.desc())
                )
            )
            .scalars()
            .first()
        )
        if version is None:
            print(f"no agent named {args.agent!r} for tenant {args.tenant}", file=sys.stderr)
            return 2

        field = args.kind.lower()
        setattr(version, f"{field}_provider_id", provider.id)
        setattr(version, f"{field}_model_id", model.id)
        await session.commit()

        print(
            f"agent {args.agent!r} v{version.version_number}: "
            f"{args.kind} -> {args.provider}/{args.model}"
        )
        print("in-flight calls keep their current configuration; new calls use this")
    return 0


async def cmd_seed_rbac(args: argparse.Namespace) -> int:
    """Create the spec-8 roles and permissions (Phase 3 item 3.1).

    Reports the resulting totals rather than a delta, because the operation is
    idempotent and "what exists now" is the useful answer either way.
    """
    factory = get_session_factory()
    async with factory() as session:
        await seed_roles_and_permissions(session)
        await session.commit()

        permissions = (
            await session.execute(select(func.count()).select_from(Permission))
        ).scalar_one()
        roles = (await session.execute(select(func.count()).select_from(Role))).scalar_one()
        grants = (
            await session.execute(select(func.count()).select_from(RolePermission))
        ).scalar_one()

        print(f"rbac: {permissions} permissions, {roles} roles, {grants} grants")
        for name, scope in (
            await session.execute(select(Role.name, Role.scope).order_by(Role.scope, Role.name))
        ).all():
            held = (
                await session.execute(
                    select(func.count())
                    .select_from(RolePermission)
                    .join(Role, Role.id == RolePermission.role_id)
                    .where(Role.name == name)
                )
            ).scalar_one()
            print(f"  {scope:<8} {name:<18} permissions={held}")
    return 0


async def cmd_create_user(args: argparse.Namespace) -> int:
    """Create a user and assign a role.

    The password is read from stdin so it never reaches a process listing or a
    shell history file.
    """
    import sys as _sys

    password = _sys.stdin.read().strip()
    if not password:
        print(
            "no password on stdin; pipe it in, e.g. "
            'printf %s "$PW" | ... create-user --email a@b.c --role TENANT_ADMIN',
            file=sys.stderr,
        )
        return 2

    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        print(f"password rejected: {exc}", file=sys.stderr)
        return 2

    # Validate with exactly the rules the API's login schema uses. Without
    # this the CLI happily creates an account whose address the API will later
    # refuse, producing a user who exists and can never sign in.
    try:
        from pydantic import TypeAdapter
        from pydantic.networks import EmailStr

        TypeAdapter(EmailStr).validate_python(args.email)
    except Exception as exc:
        reason = str(exc).splitlines()[-1].strip() if str(exc) else "invalid address"
        print(f"email rejected: {reason}", file=sys.stderr)
        print(
            "  note: reserved TLDs such as .test, .invalid and .localhost are refused, "
            "so an account using one could never sign in",
            file=sys.stderr,
        )
        return 2

    is_platform = args.role in {r.value for r in PlatformRole}

    factory = get_session_factory()
    async with factory() as session:
        role = (
            await session.execute(select(Role).where(Role.name == args.role))
        ).scalar_one_or_none()
        if role is None:
            print(
                f"no role named {args.role!r}; run seed-rbac first",
                file=sys.stderr,
            )
            return 2

        # Carry the id rather than the row: a platform user has no tenant, and
        # threading an Optional row through the inserts below makes every use
        # site need a None check.
        tenant_id: uuid.UUID | None = None
        if not is_platform:
            tenant = (
                await session.execute(select(Tenant).where(Tenant.slug == args.tenant))
            ).scalar_one_or_none()
            if tenant is None:
                print(f"no tenant with slug {args.tenant!r}", file=sys.stderr)
                return 2
            tenant_id = tenant.id

        existing = (
            await session.execute(select(User).where(User.email == args.email))
        ).scalar_one_or_none()
        if existing is not None:
            print(f"a user with email {args.email!r} already exists", file=sys.stderr)
            return 2

        user = User(
            tenant_id=tenant_id,
            is_platform_user=is_platform,
            email=args.email,
            full_name=args.name or args.email.split("@")[0],
            password_hash=password_hash,
        )
        session.add(user)
        await session.flush()

        session.add(
            UserRole(
                user_id=user.id,
                role_id=role.id,
                tenant_id=tenant_id,
            )
        )
        await session.commit()

        scope = "platform" if is_platform else f"tenant={args.tenant}"
        print(f"user created: {args.email} role={args.role} {scope}")
    return 0


async def cmd_create_test_room(args: argparse.Namespace) -> int:
    """Create a LiveKit room carrying a DID, for the browser test path.

    Plan 2b.10. A browser client mints its own token from its own configuration
    and cannot declare which DID it is calling, so the DID travels in the
    room's metadata and the worker reads it there when no SIP participant
    arrives. Connect the test client to this room name and the call resolves
    the same tenant, agent and providers a real call would.

    Refused outside development, and refused for a DID that is not active:
    creating a room for a number nothing answers produces a call that connects
    to silence, which is the failure this whole path exists to avoid.
    """
    if settings.environment != "development":
        print(
            f"refused: the browser test path is development-only "
            f"(environment={settings.environment})",
            file=sys.stderr,
        )
        return 2

    factory = get_session_factory()
    async with factory() as session:
        row = (
            await session.execute(
                select(
                    PhoneNumber.number,
                    PhoneNumber.inbound_agent_id,
                    PhoneNumber.tenant_id,
                    Tenant.slug,
                )
                .join(Tenant, Tenant.id == PhoneNumber.tenant_id)
                .where(
                    PhoneNumber.number == args.did,
                    PhoneNumber.status == ResourceStatus.ACTIVE,
                )
            )
        ).first()

        # Taken from the tenant's own dispatch rule rather than a default here.
        # The name has to match what the worker registered, and two places
        # holding it independently is how they come to disagree.
        dispatch_name = None
        if row is not None:
            dispatch_name = (
                await session.execute(
                    select(LiveKitDispatchRule.agent_dispatch_name).where(
                        LiveKitDispatchRule.tenant_id == row.tenant_id
                    )
                )
            ).scalar_one_or_none()

    if row is None:
        print(f"refused: no active DID {args.did!r}", file=sys.stderr)
        return 2
    if row.inbound_agent_id is None:
        print(f"refused: DID {args.did!r} has no inbound agent", file=sys.stderr)
        return 2

    metadata = json.dumps({"did": args.did, "source": "browser-test"})
    client = LiveKitAdminClient()
    try:
        name = await client.create_room_with_metadata(args.room, metadata)
    except LiveKitError as exc:
        print(f"could not create the room: {exc}", file=sys.stderr)
        return 2

    # The worker registers with an explicit agent name, so LiveKit will not
    # dispatch it into a room on its own. Without this the browser joins, no
    # agent arrives, and the failure looks identical to every other one on
    # this path.
    agent_name = args.agent_name or dispatch_name
    if not agent_name:
        print(
            "refused: no dispatch rule for this tenant names an agent, so there is "
            "nothing to dispatch; pass --agent-name to override",
            file=sys.stderr,
        )
        return 2
    try:
        await client.dispatch_agent(name, agent_name)
    except LiveKitError as exc:
        print(f"room created but the agent could not be dispatched: {exc}", file=sys.stderr)
        return 2

    print(f"room created: {name}")
    print(f"  did      {args.did}  (tenant {row.slug})")
    print(f"  metadata {metadata}")
    print(f"  agent    {agent_name} dispatched")
    print()
    print("next:")
    print("  1. set ALLOW_BROWSER_TEST_PARTICIPANT=true and restart the worker")
    print(f"  2. connect a browser client to room {name!r} and publish a microphone")
    print("  3. the worker resolves this DID exactly as an inbound call would")
    return 0


async def cmd_detect_drift() -> int:
    """Compare PostgreSQL against LiveKit without repairing (spec 46).

    Marks missing or mismatched rows ``DRIFTED``. Orphans — LiveKit objects
    no row names — are printed and left alone. Repair is ``sync-livekit``.
    """
    from app.livekit.drift import detect_drift

    manager = SipResourceManager()
    factory = get_session_factory()
    async with factory() as session:
        report = await detect_drift(session, manager)
        await session.commit()

    if not report.livekit_reachable:
        print(f"LiveKit unreachable: {report.error}", file=sys.stderr)
        return 2

    print(
        f"compared {report.trunks_compared} trunk(s) and "
        f"{report.rules_compared} dispatch rule(s)"
    )
    if not report.findings:
        print("no configuration drift")
        return 0

    print("Configuration Drift Detected")
    for item in report.findings:
        location = item.livekit_resource_id or str(item.row_id or "")
        print(f"  {item.kind.value:18} {item.resource:14} {item.name}  {location}")
        print(f"    {item.reason}")
    return 1


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
    sub.add_parser(
        "detect-drift",
        help="mark drifted rows without repairing; print orphans (spec 46)",
    )
    sub.add_parser("show-config", help="compare LiveKit against our records")

    sub.add_parser("seed-rbac", help="create the spec-8 roles and permissions")

    mkuser = sub.add_parser(
        "create-user", help="create a user and assign a role; password read from stdin"
    )
    mkuser.add_argument("--email", required=True)
    mkuser.add_argument("--name", default=None, help="full name")
    mkuser.add_argument(
        "--role",
        required=True,
        choices=[r.value for r in PlatformRole] + [r.value for r in TenantRole],
    )
    mkuser.add_argument("--tenant", default="dev", help="tenant slug; ignored for platform roles")

    cred = sub.add_parser(
        "set-credential",
        help="store a provider API key, encrypted; the key is read from stdin",
    )
    cred.add_argument("--tenant", default="dev", help="tenant slug")
    cred.add_argument("--kind", required=True, choices=[k.value for k in ProviderKind])
    cred.add_argument("--provider", required=True, help="provider slug, e.g. openai_compatible")
    cred.add_argument("--label", default="primary", help="primary, failover, ...")
    cred.add_argument("--base-url", default=None, help="endpoint override for this credential")

    agentp = sub.add_parser(
        "set-agent-provider", help="point an agent's STT, LLM or TTS at a provider"
    )
    agentp.add_argument("--tenant", default="dev", help="tenant slug")
    agentp.add_argument("--agent", default="Development Agent", help="agent name")
    agentp.add_argument("--kind", required=True, choices=[k.value for k in ProviderKind])
    agentp.add_argument("--provider", required=True, help="provider slug")
    agentp.add_argument("--model", required=True, help="model slug")

    testroom = sub.add_parser(
        "create-test-room",
        help="create a LiveKit room carrying a DID, for the browser test path (dev only)",
    )
    testroom.add_argument("--did", required=True, help="the DID the test call should resolve")
    testroom.add_argument("--room", default="browser-test", help="room name to create")
    testroom.add_argument(
        "--agent-name",
        default=None,
        help="override the dispatch identity; defaults to the tenant's dispatch rule",
    )

    args = parser.parse_args()

    async def run() -> int:
        try:
            if args.command == "seed-platform":
                return await cmd_seed_platform()
            if args.command == "seed-dev-tenant":
                return await cmd_seed_dev_tenant(args)
            if args.command == "sync-livekit":
                return await cmd_sync_livekit()
            if args.command == "detect-drift":
                return await cmd_detect_drift()
            if args.command == "show-config":
                return await cmd_show_config()
            if args.command == "seed-rbac":
                return await cmd_seed_rbac(args)
            if args.command == "create-user":
                return await cmd_create_user(args)
            if args.command == "set-credential":
                return await cmd_set_credential(args)
            if args.command == "set-agent-provider":
                return await cmd_set_agent_provider(args)
            if args.command == "create-test-room":
                return await cmd_create_test_room(args)
            parser.error(f"unknown command {args.command}")
            return 2
        finally:
            await dispose_engine()

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
