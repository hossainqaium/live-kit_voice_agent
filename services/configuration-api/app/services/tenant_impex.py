"""Tenant configuration export and import (spec 65).

The bundle is name-based so it can move between environments. Secrets are
stripped on the way out and refused on the way in — an export is not a
credential dump (spec 54, 65).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Agent,
    AgentTool,
    AgentVersion,
    BusinessHours,
    BusinessHoursInterval,
    KnowledgeBase,
    KnowledgeDocument,
    Model,
    Provider,
    ProviderCredential,
    RoutingRule,
    Tenant,
    Tool,
    TransferDestination,
    Voice,
)
from shared.models import AgentVersionState, ResourceStatus
from shared.tools import extract_variables, validate_request_schema

FORMAT = "livekit-voice-agent.tenant.v1"

_SECRET_HINTS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "ciphertext",
    "authorization",
    "private_key",
    "sip_auth",
)

_VERSION_TIERS = (
    "stt",
    "llm",
    "tts",
    "stt_fallback",
    "llm_fallback",
    "tts_fallback",
    "stt_local",
    "tts_local",
)


def contains_plaintext_secret(value: Any) -> bool:
    """True when a bundle still carries a secret-bearing string or bytes."""
    if isinstance(value, dict):
        for key, inner in value.items():
            lowered = str(key).lower()
            if any(hint in lowered for hint in _SECRET_HINTS) and isinstance(inner, str | bytes):
                if inner and inner not in {"***redacted***", "true", "false"}:
                    return True
            if contains_plaintext_secret(inner):
                return True
        return False
    if isinstance(value, list):
        return any(contains_plaintext_secret(item) for item in value)
    return False


def strip_secrets(value: Any) -> Any:
    """Drop secret-bearing string/bytes keys so a hand-edited file cannot sneak a key in.

    Boolean flags such as ``configured`` stay — they are not secrets.
    """
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, inner in value.items():
            lowered = str(key).lower()
            if any(hint in lowered for hint in _SECRET_HINTS) and isinstance(inner, str | bytes):
                continue
            cleaned[key] = strip_secrets(inner)
        return cleaned
    if isinstance(value, list):
        return [strip_secrets(item) for item in value]
    return value


async def export_tenant(session: AsyncSession, *, tenant_id: uuid.UUID) -> dict[str, Any]:
    """Build a portable bundle with no plaintext secrets."""
    tenant_row = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one()
    providers = {
        row.id: row
        for row in (await session.execute(select(Provider))).scalars()
    }
    models = {
        row.id: row for row in (await session.execute(select(Model))).scalars()
    }
    voices = {
        row.id: row for row in (await session.execute(select(Voice))).scalars()
    }

    tools = (
        await session.execute(select(Tool).where(Tool.tenant_id == tenant_id).order_by(Tool.name))
    ).scalars().all()
    agents = (
        await session.execute(select(Agent).where(Agent.tenant_id == tenant_id).order_by(Agent.name))
    ).scalars().all()
    hours = (
        await session.execute(
            select(BusinessHours).where(BusinessHours.tenant_id == tenant_id).order_by(BusinessHours.name)
        )
    ).scalars().all()
    rules = (
        await session.execute(
            select(RoutingRule).where(RoutingRule.tenant_id == tenant_id).order_by(RoutingRule.priority)
        )
    ).scalars().all()
    destinations = (
        await session.execute(
            select(TransferDestination)
            .where(TransferDestination.tenant_id == tenant_id)
            .order_by(TransferDestination.name)
        )
    ).scalars().all()
    bases = (
        await session.execute(
            select(KnowledgeBase).where(KnowledgeBase.tenant_id == tenant_id).order_by(KnowledgeBase.name)
        )
    ).scalars().all()
    credentials = (
        await session.execute(
            select(ProviderCredential).where(ProviderCredential.tenant_id == tenant_id)
        )
    ).scalars().all()

    agent_by_id = {row.id: row.name for row in agents}
    dest_by_id = {row.id: row.name for row in destinations}
    hours_by_id = {row.id: row.name for row in hours}
    base_by_id = {row.id: row.name for row in bases}

    exported_hours = []
    for schedule in hours:
        intervals = (
            await session.execute(
                select(BusinessHoursInterval).where(
                    BusinessHoursInterval.business_hours_id == schedule.id
                )
            )
        ).scalars().all()
        exported_hours.append(
            {
                "name": schedule.name,
                "timezone": schedule.timezone,
                "holidays": schedule.holidays or [],
                "intervals": [
                    {
                        "day_of_week": interval.day_of_week.value,
                        "opens_at": interval.opens_at.isoformat(),
                        "closes_at": interval.closes_at.isoformat(),
                    }
                    for interval in intervals
                ],
            }
        )

    exported_agents = []
    for agent in agents:
        versions = (
            await session.execute(
                select(AgentVersion)
                .where(AgentVersion.agent_id == agent.id)
                .order_by(AgentVersion.version_number)
            )
        ).scalars().all()
        exported_versions = []
        for version in versions:
            grants = (
                await session.execute(
                    select(Tool.name)
                    .join(AgentTool, AgentTool.tool_id == Tool.id)
                    .where(AgentTool.agent_version_id == version.id)
                )
            ).scalars().all()
            payload = {
                "version_number": version.version_number,
                "state": version.state.value,
                "language": version.language,
                "greeting": version.greeting,
                "system_prompt": version.system_prompt,
                "temperature": version.temperature,
                "interruption_enabled": version.interruption_enabled,
                "interruption_min_words": version.interruption_min_words,
                "silence_timeout_seconds": version.silence_timeout_seconds,
                "max_call_duration_seconds": version.max_call_duration_seconds,
                "recording_enabled": version.recording_enabled,
                "transcription_enabled": version.transcription_enabled,
                "transfer_enabled": version.transfer_enabled,
                "transfer_announcement_text": version.transfer_announcement_text,
                "transfer_summary_template": version.transfer_summary_template,
                "transfer_summary_max_seconds": version.transfer_summary_max_seconds,
                "transfer_skip_dtmf": version.transfer_skip_dtmf,
                "business_rules": version.business_rules or {},
                "knowledge_base": base_by_id.get(version.knowledge_base_id) if version.knowledge_base_id else None,
                "tools": list(grants),
                "change_note": version.change_note,
            }
            for tier in _VERSION_TIERS:
                payload[tier] = _tier_ref(version, tier, providers, models, voices)
            exported_versions.append(payload)
        published_number = None
        if agent.published_version_id is not None:
            published_number = next(
                (
                    ver.version_number
                    for ver in versions
                    if ver.id == agent.published_version_id
                ),
                None,
            )
        exported_agents.append(
            {
                "name": agent.name,
                "description": agent.description,
                "status": agent.status.value,
                "published_version": published_number,
                "versions": exported_versions,
            }
        )

    bundle = {
        "format": FORMAT,
        "exported_at": datetime.now(UTC).isoformat(),
        "tenant": {
            "slug": tenant_row.slug,
            "name": tenant_row.name,
            "timezone": tenant_row.timezone,
            "default_language": tenant_row.default_language,
        },
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "http_method": tool.http_method.value,
                "url_template": tool.url_template,
                "headers": tool.headers or {},
                "request_schema": tool.request_schema or {},
                "response_schema": tool.response_schema or {},
                "auth_type": tool.auth_type.value,
                "auth_header_name": tool.auth_header_name,
                "auth_configured": tool.auth_secret_ciphertext is not None,
                "timeout_seconds": tool.timeout_seconds,
                "max_retries": tool.max_retries,
                "status": tool.status.value,
            }
            for tool in tools
        ],
        "agents": exported_agents,
        "business_hours": exported_hours,
        "routing_rules": [
            {
                "name": rule.name,
                "description": rule.description,
                "priority": rule.priority,
                "conditions": _public_conditions(rule.conditions or {}),
                "agent": agent_by_id.get(rule.agent_id) if rule.agent_id else None,
                "business_hours": hours_by_id.get(rule.business_hours_id)
                if rule.business_hours_id
                else None,
                "fallback_action": rule.fallback_action.value if rule.fallback_action else None,
                "fallback_agent": agent_by_id.get(rule.fallback_agent_id)
                if rule.fallback_agent_id
                else None,
                "fallback_destination": dest_by_id.get(rule.fallback_transfer_destination_id)
                if rule.fallback_transfer_destination_id
                else None,
                "closed_action": rule.closed_action.value if rule.closed_action else None,
                "closed_destination": dest_by_id.get(rule.closed_transfer_destination_id)
                if rule.closed_transfer_destination_id
                else None,
                "status": rule.status.value,
            }
            for rule in rules
        ],
        "transfer_destinations": [
            {
                "name": dest.name,
                "kind": dest.kind.value,
                "target": dest.target,
                "whisper_summary": dest.whisper_summary,
                "ring_timeout_seconds": dest.ring_timeout_seconds,
                "status": dest.status.value,
            }
            for dest in destinations
        ],
        "knowledge_bases": [
            await _export_base(session, base)
            for base in bases
        ],
        "credentials": [
            {
                "label": cred.label,
                "provider_slug": providers[cred.provider_id].slug if cred.provider_id in providers else None,
                "provider_kind": providers[cred.provider_id].kind.value
                if cred.provider_id in providers
                else None,
                "configured": True,
                "key_hint": cred.key_hint,
            }
            for cred in credentials
        ],
    }
    return strip_secrets(bundle)


async def import_tenant(
    session: AsyncSession, *, tenant_id: uuid.UUID, bundle: dict[str, Any]
) -> dict[str, Any]:
    """Upsert configuration from a bundle. Never writes a supplied secret."""
    if contains_plaintext_secret(bundle):
        raise ValueError("bundle contains a plaintext secret and was refused")
    payload = strip_secrets(bundle)
    if payload.get("format") != FORMAT:
        raise ValueError(f"unsupported bundle format {payload.get('format')!r}")

    summary = {
        "created": [],
        "updated": [],
        "skipped": [],
        "errors": [],
    }

    providers = (
        await session.execute(select(Provider))
    ).scalars().all()
    provider_by_key = {(row.kind.value, row.slug): row for row in providers}
    models = (
        await session.execute(select(Model))
    ).scalars().all()
    model_by_key = {(row.provider_id, row.slug): row for row in models}
    voices = (
        await session.execute(select(Voice))
    ).scalars().all()
    voice_by_key = {(row.provider_id, row.voice_id): row for row in voices}

    tool_ids = await _import_tools(session, tenant_id, payload.get("tools") or [], summary)
    await _import_hours(session, tenant_id, payload.get("business_hours") or [], summary)
    await _import_destinations(
        session, tenant_id, payload.get("transfer_destinations") or [], summary
    )
    await _import_knowledge(session, tenant_id, payload.get("knowledge_bases") or [], summary)
    await _import_agents(
        session,
        tenant_id,
        payload.get("agents") or [],
        tool_ids,
        provider_by_key,
        model_by_key,
        voice_by_key,
        summary,
    )
    await _import_rules(session, tenant_id, payload.get("routing_rules") or [], summary)
    tenant_meta = payload.get("tenant") or {}
    if tenant_meta:
        summary["skipped"].append(
            "tenant identity (slug/name/timezone) is not applied — this environment keeps its own"
        )
    for cred in payload.get("credentials") or []:
        summary["skipped"].append(
            f"credential {cred.get('label') or cred.get('provider_slug')}: secret is never imported"
        )
    await session.flush()
    return summary


def _tier_ref(
    version: AgentVersion,
    prefix: str,
    providers: dict,
    models: dict,
    voices: dict,
) -> dict[str, str] | None:
    provider_id = getattr(version, f"{prefix}_provider_id", None)
    if provider_id is None:
        return None
    provider = providers.get(provider_id)
    model_id = getattr(version, f"{prefix}_model_id", None)
    model = models.get(model_id) if model_id else None
    voice_attr = "voice_id" if prefix == "tts" else f"{prefix}_voice_id"
    voice_id = getattr(version, voice_attr, None)
    voice = voices.get(voice_id) if voice_id else None
    return {
        "provider": provider.slug if provider else None,
        "kind": provider.kind.value if provider else None,
        "model": model.slug if model else None,
        "voice": voice.voice_id if voice else None,
    }


def _public_conditions(conditions: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(conditions)
    for key in ("pbx_id", "sip_trunk_id"):
        cleaned.pop(key, None)
    return cleaned


async def _export_base(session: AsyncSession, base: KnowledgeBase) -> dict[str, Any]:
    docs = (
        await session.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id == base.id)
        )
    ).scalars().all()
    return {
        "name": base.name,
        "description": base.description,
        "embedding_model": base.embedding_model_slug,
        "embedding_dimensions": base.embedding_dimensions,
        "top_k": base.top_k,
        "status": base.status.value,
        "documents": [
            {
                "title": doc.title,
                "source_type": doc.source_type.value,
                "source_url": doc.source_url,
                "status": doc.status.value,
            }
            for doc in docs
        ],
    }


async def _import_tools(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    rows: list[dict[str, Any]],
    summary: dict[str, list[str]],
) -> dict[str, uuid.UUID]:
    from shared.models import HttpMethod, ToolAuthType

    ids: dict[str, uuid.UUID] = {}
    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name:
            summary["errors"].append("tool missing name")
            continue
        existing = (
            await session.execute(
                select(Tool).where(Tool.tenant_id == tenant_id, Tool.name == name)
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = Tool(
                tenant_id=tenant_id,
                name=name,
                description=str(row.get("description") or name),
                url_template=str(row.get("url_template") or "https://example.invalid"),
            )
            session.add(existing)
            summary["created"].append(f"tool:{name}")
        else:
            summary["updated"].append(f"tool:{name}")
        existing.description = str(row.get("description") or existing.description)
        existing.http_method = HttpMethod(row.get("http_method") or existing.http_method.value)
        existing.url_template = str(row.get("url_template") or existing.url_template)
        existing.headers = row.get("headers") or {}
        existing.request_schema = row.get("request_schema") or {}
        existing.response_schema = row.get("response_schema") or {}
        existing.auth_type = ToolAuthType(row.get("auth_type") or existing.auth_type.value)
        existing.auth_header_name = row.get("auth_header_name")
        existing.timeout_seconds = int(row.get("timeout_seconds") or existing.timeout_seconds)
        existing.max_retries = int(row.get("max_retries") or existing.max_retries)
        if row.get("status"):
            existing.status = ResourceStatus(row["status"])
        header_values = [str(value) for value in (existing.headers or {}).values()]
        valid, error = validate_request_schema(
            existing.request_schema, extract_variables(existing.url_template or "", *header_values)
        )
        existing.schema_valid = valid
        existing.schema_validation_error = error
        await session.flush()
        ids[name] = existing.id
    return ids


async def _import_hours(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    rows: list[dict[str, Any]],
    summary: dict[str, list[str]],
) -> None:
    from datetime import time as time_type

    from shared.models import DayOfWeek

    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        existing = (
            await session.execute(
                select(BusinessHours).where(
                    BusinessHours.tenant_id == tenant_id, BusinessHours.name == name
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = BusinessHours(tenant_id=tenant_id, name=name)
            session.add(existing)
            summary["created"].append(f"business_hours:{name}")
        else:
            summary["updated"].append(f"business_hours:{name}")
        existing.timezone = row.get("timezone")
        existing.holidays = row.get("holidays") or []
        await session.flush()
        current = (
            await session.execute(
                select(BusinessHoursInterval).where(
                    BusinessHoursInterval.business_hours_id == existing.id
                )
            )
        ).scalars().all()
        for interval in current:
            await session.delete(interval)
        for item in row.get("intervals") or []:
            session.add(
                BusinessHoursInterval(
                    tenant_id=tenant_id,
                    business_hours_id=existing.id,
                    day_of_week=DayOfWeek(item["day_of_week"]),
                    opens_at=time_type.fromisoformat(item["opens_at"]),
                    closes_at=time_type.fromisoformat(item["closes_at"]),
                )
            )


async def _import_destinations(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    rows: list[dict[str, Any]],
    summary: dict[str, list[str]],
) -> None:
    from shared.models import TransferDestinationKind

    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        existing = (
            await session.execute(
                select(TransferDestination).where(
                    TransferDestination.tenant_id == tenant_id,
                    TransferDestination.name == name,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = TransferDestination(
                tenant_id=tenant_id,
                name=name,
                kind=TransferDestinationKind(row.get("kind") or "PBX_EXTENSION"),
                target=str(row.get("target") or ""),
            )
            session.add(existing)
            summary["created"].append(f"transfer_destination:{name}")
        else:
            summary["updated"].append(f"transfer_destination:{name}")
        existing.kind = TransferDestinationKind(row.get("kind") or existing.kind.value)
        existing.target = str(row.get("target") or existing.target)
        existing.whisper_summary = bool(row.get("whisper_summary", existing.whisper_summary))
        existing.ring_timeout_seconds = int(
            row.get("ring_timeout_seconds") or existing.ring_timeout_seconds
        )


async def _import_knowledge(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    rows: list[dict[str, Any]],
    summary: dict[str, list[str]],
) -> None:
    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        existing = (
            await session.execute(
                select(KnowledgeBase).where(
                    KnowledgeBase.tenant_id == tenant_id, KnowledgeBase.name == name
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = KnowledgeBase(tenant_id=tenant_id, name=name)
            session.add(existing)
            summary["created"].append(f"knowledge_base:{name}")
        else:
            summary["updated"].append(f"knowledge_base:{name}")
        existing.description = row.get("description")
        existing.embedding_model_slug = row.get("embedding_model")
        if row.get("embedding_dimensions"):
            existing.embedding_dimensions = int(row["embedding_dimensions"])
        if row.get("top_k"):
            existing.top_k = int(row["top_k"])
        docs = row.get("documents") or []
        if docs:
            summary["skipped"].append(
                f"knowledge_base:{name} documents listed only — re-ingest files separately"
            )


async def _import_agents(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    rows: list[dict[str, Any]],
    tool_ids: dict[str, uuid.UUID],
    provider_by_key: dict,
    model_by_key: dict,
    voice_by_key: dict,
    summary: dict[str, list[str]],
) -> None:
    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        agent = (
            await session.execute(
                select(Agent).where(Agent.tenant_id == tenant_id, Agent.name == name)
            )
        ).scalar_one_or_none()
        if agent is None:
            agent = Agent(tenant_id=tenant_id, name=name, description=row.get("description"))
            session.add(agent)
            await session.flush()
            summary["created"].append(f"agent:{name}")
        else:
            agent.description = row.get("description")
            summary["updated"].append(f"agent:{name}")
        latest = (
            await session.execute(
                select(AgentVersion)
                .where(AgentVersion.agent_id == agent.id)
                .order_by(AgentVersion.version_number.desc())
            )
        ).scalars().first()
        next_number = (latest.version_number + 1) if latest is not None else 1
        for version in row.get("versions") or []:
            draft = AgentVersion(
                tenant_id=tenant_id,
                agent_id=agent.id,
                version_number=next_number,
                state=AgentVersionState.DRAFT,
                language=version.get("language") or "en",
                greeting=version.get("greeting"),
                system_prompt=version.get("system_prompt") or "",
                temperature=version.get("temperature"),
                interruption_enabled=bool(version.get("interruption_enabled", True)),
                interruption_min_words=int(version.get("interruption_min_words") or 2),
                silence_timeout_seconds=version.get("silence_timeout_seconds"),
                max_call_duration_seconds=version.get("max_call_duration_seconds"),
                recording_enabled=bool(version.get("recording_enabled", False)),
                transcription_enabled=bool(version.get("transcription_enabled", True)),
                transfer_enabled=bool(version.get("transfer_enabled", False)),
                transfer_announcement_text=version.get("transfer_announcement_text"),
                transfer_summary_template=version.get("transfer_summary_template"),
                transfer_summary_max_seconds=int(
                    version.get("transfer_summary_max_seconds") or 30
                ),
                transfer_skip_dtmf=version.get("transfer_skip_dtmf") or "1",
                business_rules=version.get("business_rules") or {},
                change_note=version.get("change_note") or "imported",
            )
            _apply_tiers(draft, version, provider_by_key, model_by_key, voice_by_key, summary)
            if version.get("knowledge_base"):
                kb = (
                    await session.execute(
                        select(KnowledgeBase).where(
                            KnowledgeBase.tenant_id == tenant_id,
                            KnowledgeBase.name == version["knowledge_base"],
                        )
                    )
                ).scalar_one_or_none()
                if kb is not None:
                    draft.knowledge_base_id = kb.id
            session.add(draft)
            await session.flush()
            for tool_name in version.get("tools") or []:
                tool_id = tool_ids.get(tool_name)
                if tool_id is None:
                    summary["skipped"].append(f"agent:{name} tool {tool_name} not found")
                    continue
                session.add(
                    AgentTool(
                        tenant_id=tenant_id,
                        agent_version_id=draft.id,
                        tool_id=tool_id,
                        enabled=True,
                    )
                )
            summary["created"].append(f"agent_version:{name}:v{next_number}")
            next_number += 1


def _apply_tiers(
    draft: AgentVersion,
    version: dict[str, Any],
    provider_by_key: dict,
    model_by_key: dict,
    voice_by_key: dict,
    summary: dict[str, list[str]],
) -> None:
    for tier in _VERSION_TIERS:
        ref = version.get(tier)
        if not ref or not ref.get("provider"):
            continue
        kind = ref.get("kind") or {
            "stt": "STT",
            "stt_fallback": "STT",
            "stt_local": "STT",
            "llm": "LLM",
            "llm_fallback": "LLM",
            "tts": "TTS",
            "tts_fallback": "TTS",
            "tts_local": "TTS",
        }.get(tier)
        provider = provider_by_key.get((kind, ref["provider"]))
        if provider is None:
            summary["skipped"].append(f"{tier} provider {ref['provider']} not in catalog")
            continue
        setattr(draft, f"{tier}_provider_id", provider.id)
        if ref.get("model"):
            model = model_by_key.get((provider.id, ref["model"]))
            if model is not None:
                setattr(draft, f"{tier}_model_id", model.id)
        if ref.get("voice") and hasattr(draft, "voice_id" if tier == "tts" else f"{tier}_voice_id"):
            voice = voice_by_key.get((provider.id, ref["voice"]))
            if voice is not None:
                attr = "voice_id" if tier == "tts" else f"{tier}_voice_id"
                setattr(draft, attr, voice.id)


async def _import_rules(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    rows: list[dict[str, Any]],
    summary: dict[str, list[str]],
) -> None:
    from shared.models import FallbackAction

    agents = {
        row.name: row.id
        for row in (
            await session.execute(select(Agent).where(Agent.tenant_id == tenant_id))
        ).scalars()
    }
    hours = {
        row.name: row.id
        for row in (
            await session.execute(select(BusinessHours).where(BusinessHours.tenant_id == tenant_id))
        ).scalars()
    }
    destinations = {
        row.name: row.id
        for row in (
            await session.execute(
                select(TransferDestination).where(TransferDestination.tenant_id == tenant_id)
            )
        ).scalars()
    }
    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        existing = (
            await session.execute(
                select(RoutingRule).where(
                    RoutingRule.tenant_id == tenant_id, RoutingRule.name == name
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = RoutingRule(tenant_id=tenant_id, name=name)
            session.add(existing)
            summary["created"].append(f"routing_rule:{name}")
        else:
            summary["updated"].append(f"routing_rule:{name}")
        existing.description = row.get("description")
        existing.priority = int(row.get("priority") or 100)
        existing.conditions = row.get("conditions") or {}
        existing.agent_id = agents.get(row["agent"]) if row.get("agent") else None
        existing.business_hours_id = (
            hours.get(row["business_hours"]) if row.get("business_hours") else None
        )
        existing.fallback_action = (
            FallbackAction(row["fallback_action"]) if row.get("fallback_action") else None
        )
        existing.fallback_agent_id = (
            agents.get(row["fallback_agent"]) if row.get("fallback_agent") else None
        )
        existing.fallback_transfer_destination_id = (
            destinations.get(row["fallback_destination"])
            if row.get("fallback_destination")
            else None
        )
        existing.closed_action = (
            FallbackAction(row["closed_action"]) if row.get("closed_action") else None
        )
        existing.closed_transfer_destination_id = (
            destinations.get(row["closed_destination"]) if row.get("closed_destination") else None
        )
        if row.get("status"):
            existing.status = ResourceStatus(row["status"])
