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
    AgentTool,
    AgentVersion,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
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
    Ticket,
    Tool,
    TransferDestination,
    Voice,
)
from shared.crypto import CredentialCipher
from shared.logging import get_logger
from shared.models import (
    TENANT_ROLE_PERMISSIONS,
    AgentVersionState,
    DocumentStatus,
    HttpMethod,
    KnowledgeSourceType,
    PbxType,
    PlatformRole,
    ProviderKind,
    ResourceStatus,
    RoleScope,
    RoomStrategy,
    SipTransport,
    TicketPriority,
    TicketSource,
    TicketStatus,
    TransferDestinationKind,
    TrunkDirection,
)
from shared.models import Permission as PermissionCode
from shared.tools import validate_request_schema

logger = get_logger(__name__)

_SERVICE_AGENT_PROMPT = (
    "You are Service Agent. Your job is to file a support ticket when a caller "
    "reports a problem with a building, room, or service. Ask for a short title "
    "and what is wrong. Confirm before calling create_ticket. After it returns "
    "a ticket number, read that number back to the caller. Keep spoken replies "
    "brief. Do not invent a ticket number."
)

_BUILTIN_TOOLS: tuple[tuple[str, str, dict], ...] = (
    (
        "create_ticket",
        "File a support ticket for a reported problem. Call only after the caller confirms.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short title of the problem"},
                "description": {
                    "type": "string",
                    "description": "What is broken and where",
                },
                "priority": {
                    "type": "string",
                    "enum": ["LOW", "NORMAL", "HIGH", "URGENT"],
                    "description": "How urgent the problem is",
                },
            },
            "required": ["title", "description"],
        },
    ),
    (
        "get_customer",
        "Look up a customer by customer_id. Demo records: 1001, 1002.",
        {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string", "description": "Customer reference"}
            },
            "required": ["customer_id"],
        },
    ),
    (
        "check_order",
        "Look up an order by order_id. Demo records: ORD-100, ORD-200.",
        {
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "Order reference"}},
            "required": ["order_id"],
        },
    ),
    (
        "create_order",
        "Place an order for a customer. Demo SKUs: WIDGET, GADGET.",
        {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "sku": {"type": "string", "description": "Item SKU"},
            },
            "required": ["customer_id", "sku"],
        },
    ),
    (
        "cancel_order",
        "Cancel an existing order by order_id.",
        {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
    ),
    (
        "check_inventory",
        "Check stock for a SKU. Demo SKUs: WIDGET, GADGET.",
        {
            "type": "object",
            "properties": {"sku": {"type": "string"}},
            "required": ["sku"],
        },
    ),
    (
        "send_sms",
        "Queue a sandbox SMS. Uses caller_number when to is omitted.",
        {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["body"],
        },
    ),
    (
        "send_email",
        "Queue a sandbox email.",
        {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to", "subject"],
        },
    ),
    (
        "transfer_call",
        "Warm-transfer this call to a human agent. The caller hears the "
        "announcement while the human is whispered a summary, then the legs "
        "are bridged. Optional destination name; optional reason.",
        {
            "type": "object",
            "properties": {
                "destination": {
                    "type": "string",
                    "description": "Name of a configured transfer destination",
                },
                "reason": {
                    "type": "string",
                    "description": "Why the caller needs a human",
                },
            },
        },
    ),
    (
        "refund_order",
        "Issue a refund for an order. Restricted — grant only to agents that may refund.",
        {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
    ),
)


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
    #: (kind, slug, adapter, display name, default base URL, model slugs, requires_credential,
    #:  notes).
    #:
    #: ``slug`` names the row and ``adapter`` names the code path, which is
    #: what lets a hosted endpoint and a self-hosted one both exist for the
    #: same protocol. Without the split there could be only one row per
    #: adapter per kind, and the local tier in spec 55 would have nothing
    #: distinct to point at.
    #:
    #: ``requires_credential`` is stated explicitly here rather than inferred
    #: from the URL, because third-party vendors (Groq, Mistral, etc.) host
    #: their APIs at their own domains but still require an API key — the old
    #: URL-based heuristic wrongly marked them as keyless.
    #:
    #: The existing ``openai_compatible`` slugs keep their names: they are the
    #: self-hosted speech endpoints and are referenced by agent versions
    #: already published. Renaming them would invalidate a live configuration.
    #:
    #: Providers whose ``adapter`` does not yet have a worker implementation
    #: can be configured through AI Setup and have keys stored and verified;
    #: they cannot be published into a live agent until the adapter lands.
    definitions: list[tuple[
        ProviderKind, str, str, str, str | None, list[str], bool, str | None
    ]] = [
        # ------------------------------------------------------------------ #
        # STT
        # ------------------------------------------------------------------ #
        (
            ProviderKind.STT,
            "openai_compatible",
            "openai_compatible",
            "Self-hosted STT (speaches / Whisper)",
            spec.speech_base_url,
            [spec.stt_model, "whisper-1"],
            False,   # self-hosted, no key needed
            None,
        ),
        (
            ProviderKind.STT,
            "openai_hosted",
            "openai_compatible",
            "OpenAI STT",
            "https://api.openai.com/v1",
            ["gpt-4o-mini-transcribe", "gpt-4o-transcribe", "whisper-1"],
            True,
            None,
        ),
        (
            ProviderKind.STT,
            "deepgram",
            "deepgram",
            "Deepgram",
            "https://api.deepgram.com",
            ["nova-3", "nova-2", "nova-2-general", "nova-2-meeting", "nova-2-phonecall",
             "nova-2-medical", "enhanced", "base"],
            True,
            None,
        ),
        (
            ProviderKind.STT,
            "assemblyai",
            "assemblyai",
            "AssemblyAI",
            "https://api.assemblyai.com",
            ["best", "nano"],
            True,
            None,
        ),
        (
            ProviderKind.STT,
            "google_stt",
            "google_stt",
            "Google Cloud Speech-to-Text",
            "https://speech.googleapis.com",
            ["latest_long", "latest_short", "telephony", "medical_dictation"],
            True,
            None,
        ),
        (
            ProviderKind.STT,
            "speechmatics",
            "speechmatics",
            "Speechmatics",
            "https://asr.api.speechmatics.com",
            ["enhanced", "standard"],
            True,
            None,
        ),
        (
            ProviderKind.STT,
            "gladia",
            "gladia",
            "Gladia",
            "https://api.gladia.io",
            ["solaria-1", "fast"],
            True,
            None,
        ),
        # ------------------------------------------------------------------ #
        # LLM
        # ------------------------------------------------------------------ #
        (
            ProviderKind.LLM,
            "openai_compatible",
            "openai_compatible",
            "OpenAI-compatible (self-hosted)",
            None,
            ["gpt-4o-mini", "gpt-4o"],
            False,   # no default URL → user supplies base_url with credential
            None,
        ),
        (
            ProviderKind.LLM,
            spec.llm_provider_slug,
            spec.llm_provider_slug,
            "Development echo model",
            None,
            [spec.llm_model],
            False,
            "Development stand-in. Refused outside development.",
        ),
        (
            ProviderKind.LLM,
            "openai_hosted",
            "openai_compatible",
            "OpenAI",
            "https://api.openai.com/v1",
            ["gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini", "o3", "o4-mini"],
            True,
            None,
        ),
        (
            ProviderKind.LLM,
            "anthropic",
            "anthropic",
            "Anthropic",
            "https://api.anthropic.com/v1",
            ["claude-opus-4-5", "claude-sonnet-4-5", "claude-haiku-3-5",
             "claude-opus-4", "claude-sonnet-4"],
            True,
            None,
        ),
        (
            ProviderKind.LLM,
            "groq",
            "openai_compatible",
            "Groq",
            "https://api.groq.com/openai/v1",
            ["llama-3.3-70b-versatile", "llama-3.1-8b-instant",
             "mixtral-8x7b-32768", "gemma2-9b-it"],
            True,
            None,
        ),
        (
            ProviderKind.LLM,
            "mistral",
            "openai_compatible",
            "Mistral AI",
            "https://api.mistral.ai/v1",
            ["mistral-large-latest", "mistral-medium-latest",
             "mistral-small-latest", "codestral-latest"],
            True,
            None,
        ),
        (
            ProviderKind.LLM,
            "google_gemini",
            "openai_compatible",
            "Google Gemini",
            "https://generativelanguage.googleapis.com/v1beta/openai",
            ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-2.5-pro",
             "gemini-1.5-pro", "gemini-1.5-flash"],
            True,
            None,
        ),
        (
            ProviderKind.LLM,
            "together",
            "openai_compatible",
            "Together AI",
            "https://api.together.xyz/v1",
            ["meta-llama/Llama-3.3-70B-Instruct-Turbo",
             "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
             "mistralai/Mixtral-8x7B-Instruct-v0.1"],
            True,
            None,
        ),
        (
            ProviderKind.LLM,
            "deepseek",
            "openai_compatible",
            "DeepSeek",
            "https://api.deepseek.com/v1",
            ["deepseek-chat", "deepseek-reasoner"],
            True,
            None,
        ),
        (
            ProviderKind.LLM,
            "ollama",
            "openai_compatible",
            "Ollama (local)",
            "http://host.docker.internal:11434/v1",
            ["llama3.3", "llama3.2", "mistral", "phi4", "gemma3", "qwen3"],
            False,  # local, no key
            None,
        ),
        # ------------------------------------------------------------------ #
        # TTS
        # ------------------------------------------------------------------ #
        (
            ProviderKind.TTS,
            "openai_compatible",
            "openai_compatible",
            "Self-hosted TTS (Kokoro)",
            spec.speech_base_url,
            [spec.tts_model, "tts-1"],
            False,
            None,
        ),
        (
            ProviderKind.TTS,
            "openai_hosted",
            "openai_compatible",
            "OpenAI TTS",
            "https://api.openai.com/v1",
            ["tts-1", "tts-1-hd", "gpt-4o-mini-tts"],
            True,
            None,
        ),
        (
            ProviderKind.TTS,
            "elevenlabs",
            "elevenlabs",
            "ElevenLabs",
            "https://api.elevenlabs.io",
            ["eleven_turbo_v2_5", "eleven_flash_v2_5",
             "eleven_multilingual_v2", "eleven_turbo_v2"],
            True,
            None,
        ),
        (
            ProviderKind.TTS,
            "cartesia",
            "cartesia",
            "Cartesia",
            "https://api.cartesia.ai",
            ["sonic-2", "sonic-english", "sonic-multilingual"],
            True,
            None,
        ),
        (
            ProviderKind.TTS,
            "playht",
            "playht",
            "PlayHT",
            "https://api.play.ht",
            ["PlayDialog", "Play3.0-mini"],
            True,
            None,
        ),
        (
            ProviderKind.TTS,
            "lmnt",
            "lmnt",
            "LMNT",
            "https://api.lmnt.com",
            ["aurora", "blizzard"],
            True,
            None,
        ),
        (
            ProviderKind.TTS,
            "deepgram_tts",
            "deepgram",
            "Deepgram Aura",
            "https://api.deepgram.com",
            ["aura-2-en-us", "aura-asteria-en", "aura-luna-en",
             "aura-stella-en", "aura-orion-en", "aura-arcas-en"],
            True,
            None,
        ),
        # ------------------------------------------------------------------ #
        # Embedding
        # ------------------------------------------------------------------ #
        (
            ProviderKind.EMBEDDING,
            "openai_compatible",
            "openai_compatible",
            "Self-hosted Embedding (OpenAI-compatible)",
            spec.speech_base_url,
            ["text-embedding-3-small", "text-embedding-3-large"],
            False,
            None,
        ),
        (
            ProviderKind.EMBEDDING,
            "openai_hosted",
            "openai_compatible",
            "OpenAI Embedding",
            "https://api.openai.com/v1",
            ["text-embedding-3-small", "text-embedding-3-large", "text-embedding-ada-002"],
            True,
            None,
        ),
        (
            ProviderKind.EMBEDDING,
            "cohere",
            "cohere",
            "Cohere",
            "https://api.cohere.ai",
            ["embed-english-v3.0", "embed-multilingual-v3.0",
             "embed-english-light-v3.0", "embed-multilingual-light-v3.0"],
            True,
            None,
        ),
        (
            ProviderKind.EMBEDDING,
            "voyage",
            "openai_compatible",
            "Voyage AI",
            "https://api.voyageai.com/v1",
            ["voyage-3.5", "voyage-3.5-lite", "voyage-3", "voyage-3-lite",
             "voyage-code-3", "voyage-finance-2"],
            True,
            None,
        ),
        (
            ProviderKind.EMBEDDING,
            "google_embedding",
            "google_embedding",
            "Google Embedding",
            "https://generativelanguage.googleapis.com/v1beta",
            ["text-embedding-004", "gemini-embedding-exp-03-07"],
            True,
            None,
        ),
    ]

    for kind, slug, adapter, display_name, base_url, model_slugs, requires_credential, notes in definitions:
        provider = (
            await session.execute(
                select(Provider).where(Provider.kind == kind, Provider.slug == slug)
            )
        ).scalar_one_or_none()

        if provider is None:
            provider = Provider(
                kind=kind,
                slug=slug,
                adapter=adapter,
                display_name=display_name,
                default_base_url=base_url,
                supports_streaming=True,
                requires_credential=requires_credential,
                notes=notes,
            )
            session.add(provider)
            await session.flush()
        elif provider.adapter is None:
            # Seeded before the column existed. Backfilling is safe because
            # the fallback is the slug, which is what it resolved to anyway.
            provider.adapter = adapter
            provider.display_name = display_name

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

    service_agent = (
        await session.execute(
            select(Agent).where(
                Agent.tenant_id == tenant.id,
                Agent.name.in_(("Service Agent", "Server Agent")),
            )
        )
    ).scalar_one_or_none()
    if service_agent is None:
        service_agent = Agent(
            tenant_id=tenant.id,
            name="Service Agent",
            description="Files support tickets when a caller reports a problem",
        )
        session.add(service_agent)
        await session.flush()
    elif service_agent.name != "Service Agent":
        service_agent.name = "Service Agent"
        service_agent.description = "Files support tickets when a caller reports a problem"

    service_version = (
        await session.execute(
            select(AgentVersion).where(
                AgentVersion.agent_id == service_agent.id, AgentVersion.version_number == 1
            )
        )
    ).scalar_one_or_none()
    if service_version is None:
        stt = await _provider(session, ProviderKind.STT, "openai_compatible")
        llm = await _provider(session, ProviderKind.LLM, spec.llm_provider_slug)
        tts = await _provider(session, ProviderKind.TTS, "openai_compatible")
        service_version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=service_agent.id,
            version_number=1,
            state=AgentVersionState.PUBLISHED,
            language="en",
            greeting=(
                "Hello, this is the service desk. Tell me what is broken and I "
                "will file a ticket."
            ),
            system_prompt=_SERVICE_AGENT_PROMPT,
            stt_provider_id=stt.id,
            stt_model_id=await _model_id(session, stt.id, spec.stt_model),
            llm_provider_id=llm.id,
            llm_model_id=await _model_id(session, llm.id, spec.llm_model),
            tts_provider_id=tts.id,
            tts_model_id=await _model_id(session, tts.id, spec.tts_model),
            voice_id=await _voice_id(session, tts.id, spec.tts_voice),
            temperature=0.4,
            interruption_enabled=True,
            silence_timeout_seconds=15,
            max_call_duration_seconds=600,
            recording_enabled=False,
            transcription_enabled=True,
        )
        session.add(service_version)
        await session.flush()
        service_agent.published_version_id = service_version.id
        await session.flush()

    ticket = (
        await session.execute(
            select(Ticket).where(
                Ticket.tenant_id == tenant.id, Ticket.ticket_number == "TCK-0001"
            )
        )
    ).scalar_one_or_none()
    if ticket is None:
        session.add(
            Ticket(
                tenant_id=tenant.id,
                ticket_number="TCK-0001",
                title="Lobby access card reader offline",
                description=(
                    "The card reader at the main lobby door does not beep and "
                    "the lock stays closed. Seeded example ticket."
                ),
                status=TicketStatus.OPEN,
                priority=TicketPriority.HIGH,
                source=TicketSource.MANUAL,
                caller_number=None,
            )
        )
        await session.flush()

    if service_version is not None:
        prompt = service_version.system_prompt or ""
        greeting = service_version.greeting or ""
        if (
            "Until the ticket tool" in prompt
            or "You are Server Agent" in prompt
            or "server desk" in greeting
        ):
            service_version.system_prompt = _SERVICE_AGENT_PROMPT
            service_version.greeting = (
                "Hello, this is the service desk. Tell me what is broken and I "
                "will file a ticket."
            )

    tools_by_name = await _seed_builtin_tools(session, tenant.id)
    service_version_id = service_agent.published_version_id or service_version.id
    await _grant_tool(session, tenant.id, service_version_id, tools_by_name["create_ticket"].id)
    dev_version_id = agent.published_version_id or version.id
    for name in (
        "get_customer",
        "check_order",
        "create_order",
        "check_inventory",
        "transfer_call",
    ):
        await _grant_tool(session, tenant.id, dev_version_id, tools_by_name[name].id)

    dest = (
        await session.execute(
            select(TransferDestination).where(
                TransferDestination.tenant_id == tenant.id,
                TransferDestination.name == "Reception",
            )
        )
    ).scalar_one_or_none()
    if dest is None:
        dest = TransferDestination(
            tenant_id=tenant.id,
            name="Reception",
            kind=TransferDestinationKind.PBX_EXTENSION,
            target="1000",
            pbx_id=pbx.id,
            sip_trunk_id=trunk.id,
            whisper_summary=True,
            ring_timeout_seconds=30,
            status=ResourceStatus.ACTIVE,
        )
        session.add(dest)
        await session.flush()

    live = await session.get(AgentVersion, dev_version_id)
    if live is not None and not live.transfer_enabled:
        live.transfer_enabled = True
        live.transfer_announcement_text = (
            live.transfer_announcement_text
            or "Your call is being transferred to a human agent. Please wait."
        )
        live.transfer_summary_max_seconds = live.transfer_summary_max_seconds or 30
        live.transfer_skip_dtmf = live.transfer_skip_dtmf or "1"

    hotel_kb = await _seed_hotel_knowledge(session, tenant.id)
    if hotel_kb is not None:
        if version.knowledge_base_id is None:
            version.knowledge_base_id = hotel_kb.id
        published = agent.published_version_id
        if published is not None and published != version.id:
            live = await session.get(AgentVersion, published)
            if live is not None and live.knowledge_base_id is None:
                live.knowledge_base_id = hotel_kb.id

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


_HOTEL_POLICY_TEXT = (
    "Checkout is at 11:00. Late checkout until 14:00 costs 25 dollars when the "
    "room is available. The lobby Wi-Fi network is Hotel-Guest and the password "
    "is harbour-1842. Pets under 15 kilograms are allowed with a 50 dollar "
    "cleaning fee. Breakfast is served from 06:30 to 10:00 in the Garden Room. "
    "The pool closes at 22:00. Lost-key replacements are 15 dollars at reception."
)


async def _seed_hotel_knowledge(session: AsyncSession, tenant_id: uuid.UUID) -> KnowledgeBase | None:
    """Idempotent sample base so RAG can be demonstrated without an upload."""
    existing = (
        await session.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.tenant_id == tenant_id,
                KnowledgeBase.name == "Hotel policies",
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = KnowledgeBase(
            tenant_id=tenant_id,
            name="Hotel policies",
            description="Seeded house rules used to demonstrate retrieval.",
            embedding_model_slug="text-embedding-3-small",
            embedding_dimensions=1536,
            top_k=4,
            status=ResourceStatus.ACTIVE,
        )
        session.add(existing)
        await session.flush()

    doc = (
        await session.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.knowledge_base_id == existing.id,
                KnowledgeDocument.title == "House rules",
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        doc = KnowledgeDocument(
            tenant_id=tenant_id,
            knowledge_base_id=existing.id,
            title="House rules",
            source_type=KnowledgeSourceType.TXT,
            status=DocumentStatus.INDEXED,
            chunk_count=1,
            byte_size=len(_HOTEL_POLICY_TEXT.encode()),
        )
        session.add(doc)
        await session.flush()
        session.add(
            KnowledgeChunk(
                tenant_id=tenant_id,
                knowledge_document_id=doc.id,
                knowledge_base_id=existing.id,
                chunk_index=0,
                content=_HOTEL_POLICY_TEXT,
                doc_metadata={"title": "House rules", "source_type": "TXT"},
            )
        )
        await session.flush()
    return existing


async def _seed_builtin_tools(session: AsyncSession, tenant_id: uuid.UUID) -> dict[str, Tool]:
    """Idempotently create the platform builtin tools for a tenant."""
    by_name: dict[str, Tool] = {}
    for name, description, schema in _BUILTIN_TOOLS:
        row = (
            await session.execute(
                select(Tool).where(Tool.tenant_id == tenant_id, Tool.name == name)
            )
        ).scalar_one_or_none()
        if row is None:
            valid, error = validate_request_schema(schema, [])
            row = Tool(
                tenant_id=tenant_id,
                name=name,
                description=description,
                http_method=HttpMethod.POST,
                url_template=f"builtin://{name}",
                request_schema=schema,
                status=ResourceStatus.ACTIVE,
                schema_valid=valid,
                schema_validation_error=error,
            )
            session.add(row)
            await session.flush()
        elif name == "transfer_call" and "Not available until Phase 6.9" in (row.description or ""):
            valid, error = validate_request_schema(schema, [])
            row.description = description
            row.request_schema = schema
            row.schema_valid = valid
            row.schema_validation_error = error
        by_name[name] = row
    return by_name


async def _grant_tool(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    agent_version_id: uuid.UUID,
    tool_id: uuid.UUID,
) -> None:
    existing = (
        await session.execute(
            select(AgentTool).where(
                AgentTool.tenant_id == tenant_id,
                AgentTool.agent_version_id == agent_version_id,
                AgentTool.tool_id == tool_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            AgentTool(
                tenant_id=tenant_id,
                agent_version_id=agent_version_id,
                tool_id=tool_id,
                enabled=True,
            )
        )
        await session.flush()


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
