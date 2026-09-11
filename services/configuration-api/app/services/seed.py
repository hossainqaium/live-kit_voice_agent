"""Development seeding.

Creates the platform catalog and one development tenant configured end to end,
so a call can be placed before the configuration UI exists (Phase 4).

This is a development convenience, not a product feature. Spec 77 requires a
tenant administrator to do all of this through the UI with no direct database
modification; that path is built in Phase 4 and this file is what Phase 1 uses
in the meantime. Every value here is data, not behaviour — nothing seeded
becomes code (spec 9).
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Agent,
    AgentVersion,
    LiveKitDispatchRule,
    Model,
    Pbx,
    Permission,
    PhoneNumber,
    Provider,
    Role,
    RolePermission,
    RoutingRule,
    SipCredential,
    SipTrunk,
    Tenant,
    Voice,
)
from shared.crypto import CredentialCipher
from shared.logging import get_logger
from shared.models import (
    TENANT_ROLE_PERMISSIONS,
    AgentVersionState,
    PbxType,
    PlatformRole,
    ProviderKind,
    RoleScope,
    RoomStrategy,
    SipTransport,
    TrunkDirection,
)
from shared.models import Permission as PermissionCode

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DevTenantSpec:
    """Inputs for the development tenant.

    Defaults describe a PBX on the local network; every one is overridable so
    the seed does not encode one machine's addresses.
    """

    tenant_slug: str = "dev"
    tenant_name: str = "Development Tenant"

    pbx_host: str = "192.168.0.113"
    pbx_port: int = 5060
    pbx_type: PbxType = PbxType.FREESWITCH

    did: str = "1001"

    #: Source addresses permitted on the trunk. Empty in development because
    #: Docker Desktop rewrites the source address (see the trunk below).
    allowed_source_ips: tuple[str, ...] = ()

    #: SIP digest credentials for the trunk. Authentication is what secures an
    #: inbound trunk when IP allow-listing is unavailable, and livekit-sip
    #: refuses calls on a trunk it cannot place (spec 15, 53).
    sip_auth_username: str = "lkdev"

    #: Generated when left empty, and printed once by the CLI. Not a default
    #: password: a shared well-known credential in a seed is how a development
    #: convenience becomes a production incident.
    sip_auth_password: str = ""

    #: OpenAI-compatible speech endpoint. Reachable from inside the worker
    #: container, which is why it is host.docker.internal rather than
    #: localhost.
    speech_base_url: str = "http://host.docker.internal:8010/v1"
    stt_model: str = "Systran/faster-whisper-tiny"
    tts_model: str = "speaches-ai/Kokoro-82M-v1.0-ONNX"
    tts_voice: str = "af_heart"

    #: The keyless development stand-in. Replaced by a real provider as soon
    #: as a credential is configured.
    llm_provider_slug: str = "echo_dev"
    llm_model: str = "echo"

    agent_dispatch_name: str = "voice-agent"


# --------------------------------------------------------------------------- #
# Roles and permissions (spec 8)
# --------------------------------------------------------------------------- #


async def seed_roles_and_permissions(session: AsyncSession) -> None:
    """Create every permission and role, and wire them together.

    Idempotent: safe to run on an existing database, because the platform
    gains permissions over time and this is how they arrive.
    """
    existing_permissions = {
        code for (code,) in (await session.execute(select(Permission.code))).all()
    }
    for code in PermissionCode:
        if code.value not in existing_permissions:
            session.add(Permission(code=code.value, description=f"Grants {code.value}"))
    await session.flush()

    permissions_by_code = {p.code: p for p in (await session.execute(select(Permission))).scalars()}

    existing_roles = {r.name: r for r in (await session.execute(select(Role))).scalars()}

    # Platform roles hold authority that is not expressible as tenant
    # permissions, so they are not mapped through the tenant permission table.
    for platform_role in PlatformRole:
        if platform_role.value not in existing_roles:
            platform_row = Role(
                name=platform_role.value,
                scope=RoleScope.PLATFORM,
                description=f"Platform role {platform_role.value}",
                is_system=True,
            )
            session.add(platform_row)
            existing_roles[platform_role.value] = platform_row
    await session.flush()

    for tenant_role, permissions in TENANT_ROLE_PERMISSIONS.items():
        role: Role | None = existing_roles.get(tenant_role.value)
        if role is None:
            role = Role(
                name=tenant_role.value,
                scope=RoleScope.TENANT,
                description=f"Tenant role {tenant_role.value}",
                is_system=True,
            )
            session.add(role)
            await session.flush()
            existing_roles[tenant_role.value] = role

        already = {
            permission_id
            for (permission_id,) in (
                await session.execute(
                    select(RolePermission.permission_id).where(RolePermission.role_id == role.id)
                )
            ).all()
        }
        for permission in permissions:
            record = permissions_by_code[permission.value]
            if record.id not in already:
                session.add(RolePermission(role_id=role.id, permission_id=record.id))

    await session.flush()
    logger.info(
        "seeded_roles_and_permissions",
        extra={"role_count": len(existing_roles), "permission_count": len(PermissionCode)},
    )


# --------------------------------------------------------------------------- #
# Provider catalog (spec 25, 27)
# --------------------------------------------------------------------------- #


async def seed_provider_catalog(session: AsyncSession, spec: DevTenantSpec) -> None:
    """Register the adapters this build ships with.

    Only adapters that exist in the worker's registry are seeded. Listing a
    provider the code cannot construct would let an operator publish an agent
    that fails at call time rather than at validation time (spec 63).
    """
    definitions = [
        (
            ProviderKind.STT,
            "openai_compatible",
            "OpenAI-compatible STT",
            spec.speech_base_url,
            [spec.stt_model, "whisper-1"],
        ),
        (
            ProviderKind.LLM,
            "openai_compatible",
            "OpenAI-compatible LLM",
            None,
            ["gpt-4o-mini", "gpt-4o"],
        ),
        (
            ProviderKind.LLM,
            spec.llm_provider_slug,
            "Development echo model",
            None,
            [spec.llm_model],
        ),
        (
            ProviderKind.TTS,
            "openai_compatible",
            "OpenAI-compatible TTS",
            spec.speech_base_url,
            [spec.tts_model, "tts-1"],
        ),
    ]

    for kind, slug, display_name, base_url, model_slugs in definitions:
        provider = (
            await session.execute(
                select(Provider).where(Provider.kind == kind, Provider.slug == slug)
            )
        ).scalar_one_or_none()

        if provider is None:
            provider = Provider(
                kind=kind,
                slug=slug,
                display_name=display_name,
                default_base_url=base_url,
                supports_streaming=True,
                # A base URL pointing somewhere other than a vendor's public
                # API means a self-hosted endpoint, which authenticates by
                # network reachability rather than by key. Seeding it as
                # credential-required would make the local-model path
                # unpublishable out of the box.
                requires_credential=not (
                    base_url and "://" in base_url and "api.openai.com" not in base_url
                ),
                notes=(
                    "Development stand-in. Refused outside development."
                    if slug == spec.llm_provider_slug
                    else None
                ),
            )
            session.add(provider)
            await session.flush()

        existing_models = {
            m.slug
            for m in (
                await session.execute(select(Model).where(Model.provider_id == provider.id))
            ).scalars()
        }
        for index, model_slug in enumerate(model_slugs):
            if model_slug not in existing_models:
                session.add(
                    Model(
                        provider_id=provider.id,
                        slug=model_slug,
                        display_name=model_slug,
                        is_default=(index == 0),
                    )
                )

    await session.flush()

    # Voice library entry for the local TTS voice (spec 27).
    tts_provider = (
        await session.execute(
            select(Provider).where(
                Provider.kind == ProviderKind.TTS, Provider.slug == "openai_compatible"
            )
        )
    ).scalar_one()

    voice = (
        await session.execute(
            select(Voice).where(
                Voice.provider_id == tts_provider.id, Voice.voice_id == spec.tts_voice
            )
        )
    ).scalar_one_or_none()
    if voice is None:
        session.add(
            Voice(
                provider_id=tts_provider.id,
                voice_id=spec.tts_voice,
                name=spec.tts_voice,
                language="en",
                description="Local Kokoro voice used for development",
                is_default=True,
            )
        )

    await session.flush()
    logger.info("seeded_provider_catalog", extra={"provider_count": len(definitions)})


# --------------------------------------------------------------------------- #
# Development tenant
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SeededTenant:
    """What the caller needs to drive a test call."""

    tenant_id: uuid.UUID
    pbx_id: uuid.UUID
    sip_trunk_id: uuid.UUID
    phone_number_id: uuid.UUID
    agent_id: uuid.UUID
    agent_version_id: uuid.UUID
    dispatch_rule_id: uuid.UUID
    did: str

    #: The trunk's SIP password. Shown once by the CLI, because the stored copy
    #: is encrypted and cannot be read back (spec 54).
    sip_auth_username: str = ""
    sip_auth_password: str = ""


async def seed_dev_tenant(session: AsyncSession, spec: DevTenantSpec) -> SeededTenant:
    """Create one tenant configured end to end.

    Does not touch LiveKit. Creating the LiveKit resources is a separate,
    explicit step so that seeding a database and mutating a media server are
    never the same action.
    """
    tenant = (
        await session.execute(select(Tenant).where(Tenant.slug == spec.tenant_slug))
    ).scalar_one_or_none()
    if tenant is None:
        tenant = Tenant(
            name=spec.tenant_name,
            slug=spec.tenant_slug,
            timezone="Asia/Dhaka",
            default_language="en",
            # Deliberately low: the limit path (spec 47) should be exercised in
            # development, not discovered in production.
            max_concurrent_calls=5,
            max_daily_calls=200,
            max_monthly_minutes=1000,
        )
        session.add(tenant)
        await session.flush()

    pbx = (
        await session.execute(
            select(Pbx).where(Pbx.tenant_id == tenant.id, Pbx.name == "Development PBX")
        )
    ).scalar_one_or_none()
    if pbx is None:
        pbx = Pbx(
            tenant_id=tenant.id,
            name="Development PBX",
            pbx_type=spec.pbx_type,
            host=spec.pbx_host,
            port=spec.pbx_port,
            transport=SipTransport.UDP,
            description="Local FreeSWITCH/FusionPBX used for development calls",
        )
        session.add(pbx)
        await session.flush()

    trunk = (
        await session.execute(
            select(SipTrunk).where(
                SipTrunk.tenant_id == tenant.id, SipTrunk.name == "Development Trunk"
            )
        )
    ).scalar_one_or_none()
    if trunk is None:
        trunk = SipTrunk(
            tenant_id=tenant.id,
            name="Development Trunk",
            pbx_id=pbx.id,
            direction=TrunkDirection.INBOUND,
            sip_host=spec.pbx_host,
            port=spec.pbx_port,
            transport=SipTransport.UDP,
            # Empty on purpose in development, despite spec 53 wanting an IP
            # allowlist.
            #
            # Docker Desktop on macOS rewrites the UDP source address of
            # inbound packets: a SIP INVITE sent from 192.168.0.113 arrives
            # inside the container appearing to come from a synthetic public
            # address. An allowlist containing the real PBX address therefore
            # never matches, and LiveKit rejects the call — which presents as
            # "486 Busy" with reason "flood", not as an obvious ACL denial.
            #
            # Production is unaffected: on a Linux host, or Kubernetes with
            # externalTrafficPolicy Local, the source address survives. The
            # allowlist must be re-verified there rather than assumed to work
            # because it was configured.
            allowed_ips=list(spec.allowed_source_ips),
            auth_username=spec.sip_auth_username,
            codecs=["PCMU", "PCMA", "OPUS"],
            media_encryption_required=False,
        )
        session.add(trunk)
        await session.flush()

    trunk_password = await _ensure_trunk_credential(session, trunk, spec)

    agent = (
        await session.execute(
            select(Agent).where(Agent.tenant_id == tenant.id, Agent.name == "Development Agent")
        )
    ).scalar_one_or_none()
    if agent is None:
        agent = Agent(
            tenant_id=tenant.id,
            name="Development Agent",
            description="Answers development calls and verifies the voice pipeline",
        )
        session.add(agent)
        await session.flush()

    version = (
        await session.execute(
            select(AgentVersion).where(
                AgentVersion.agent_id == agent.id, AgentVersion.version_number == 1
            )
        )
    ).scalar_one_or_none()
    if version is None:
        stt = await _provider(session, ProviderKind.STT, "openai_compatible")
        llm = await _provider(session, ProviderKind.LLM, spec.llm_provider_slug)
        tts = await _provider(session, ProviderKind.TTS, "openai_compatible")

        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version_number=1,
            state=AgentVersionState.PUBLISHED,
            language="en",
            greeting="Hello. You are through to the development voice agent. How can I help?",
            system_prompt=(
                "You are a helpful voice assistant answering a phone call. "
                "Keep replies short and conversational, as they will be spoken aloud."
            ),
            stt_provider_id=stt.id,
            stt_model_id=await _model_id(session, stt.id, spec.stt_model),
            llm_provider_id=llm.id,
            llm_model_id=await _model_id(session, llm.id, spec.llm_model),
            tts_provider_id=tts.id,
            tts_model_id=await _model_id(session, tts.id, spec.tts_model),
            voice_id=await _voice_id(session, tts.id, spec.tts_voice),
            temperature=0.7,
            interruption_enabled=True,
            silence_timeout_seconds=15,
            max_call_duration_seconds=600,
            recording_enabled=False,
            transcription_enabled=True,
        )
        session.add(version)
        await session.flush()

        agent.published_version_id = version.id
        await session.flush()

    dispatch_rule = (
        await session.execute(
            select(LiveKitDispatchRule).where(
                LiveKitDispatchRule.tenant_id == tenant.id,
                LiveKitDispatchRule.name == "Development Dispatch",
            )
        )
    ).scalar_one_or_none()
    if dispatch_rule is None:
        dispatch_rule = LiveKitDispatchRule(
            tenant_id=tenant.id,
            name="Development Dispatch",
            sip_trunk_id=trunk.id,
            room_strategy=RoomStrategy.INDIVIDUAL,
            room_prefix=f"{spec.tenant_slug}-call-",
            agent_dispatch_name=spec.agent_dispatch_name,
            matched_numbers=[spec.did],
        )
        session.add(dispatch_rule)
        await session.flush()

    number = (
        await session.execute(select(PhoneNumber).where(PhoneNumber.number == spec.did))
    ).scalar_one_or_none()
    if number is None:
        number = PhoneNumber(
            tenant_id=tenant.id,
            number=spec.did,
            pbx_id=pbx.id,
            sip_trunk_id=trunk.id,
            inbound_agent_id=agent.id,
            label="Development DID",
        )
        session.add(number)
        await session.flush()

    routing_rule = (
        await session.execute(
            select(RoutingRule).where(
                RoutingRule.tenant_id == tenant.id, RoutingRule.name == "Development Default"
            )
        )
    ).scalar_one_or_none()
    if routing_rule is None:
        session.add(
            RoutingRule(
                tenant_id=tenant.id,
                name="Development Default",
                description="Routes the development DID to the development agent",
                priority=100,
                conditions={"did": spec.did},
                agent_id=agent.id,
            )
        )
        await session.flush()

    logger.info(
        "seeded_dev_tenant",
        extra={"tenant_id": str(tenant.id), "agent_id": str(agent.id)},
    )

    return SeededTenant(
        tenant_id=tenant.id,
        pbx_id=pbx.id,
        sip_trunk_id=trunk.id,
        phone_number_id=number.id,
        agent_id=agent.id,
        agent_version_id=version.id,
        dispatch_rule_id=dispatch_rule.id,
        did=spec.did,
        sip_auth_username=trunk.auth_username or "",
        sip_auth_password=trunk_password,
    )


async def _ensure_trunk_credential(
    session: AsyncSession, trunk: SipTrunk, spec: DevTenantSpec
) -> str:
    """Create or reuse the trunk's SIP password, returning the plaintext.

    Returned so the CLI can print it exactly once. The stored copy is
    encrypted and cannot be read back, which is the point (spec 54) — but a
    credential nobody can use is also useless, so it is surfaced at the moment
    of creation and never again.
    """
    existing = (
        await session.execute(select(SipCredential).where(SipCredential.sip_trunk_id == trunk.id))
    ).scalar_one_or_none()

    if existing is not None:
        # Already provisioned. The plaintext is not recoverable from here, and
        # inventing a new one would silently break a working trunk.
        return ""

    password = spec.sip_auth_password or secrets.token_urlsafe(18)
    cipher = CredentialCipher.from_env()
    encrypted = cipher.encrypt(password)

    session.add(
        SipCredential(
            tenant_id=trunk.tenant_id,
            sip_trunk_id=trunk.id,
            username=trunk.auth_username or spec.sip_auth_username,
            password_ciphertext=encrypted.ciphertext,
            encryption_key_version=encrypted.key_version,
        )
    )
    await session.flush()
    return password


# --------------------------------------------------------------------------- #
# Lookup helpers
# --------------------------------------------------------------------------- #


async def _provider(session: AsyncSession, kind: ProviderKind, slug: str) -> Provider:
    provider = (
        await session.execute(select(Provider).where(Provider.kind == kind, Provider.slug == slug))
    ).scalar_one_or_none()
    if provider is None:
        raise LookupError(f"provider {kind}:{slug} is not in the catalog; seed it first")
    return provider


async def _model_id(session: AsyncSession, provider_id: uuid.UUID, slug: str) -> uuid.UUID | None:
    model = (
        await session.execute(
            select(Model).where(Model.provider_id == provider_id, Model.slug == slug)
        )
    ).scalar_one_or_none()
    return model.id if model else None


async def _voice_id(session: AsyncSession, provider_id: uuid.UUID, voice: str) -> uuid.UUID | None:
    record = (
        await session.execute(
            select(Voice).where(Voice.provider_id == provider_id, Voice.voice_id == voice)
        )
    ).scalar_one_or_none()
    return record.id if record else None
