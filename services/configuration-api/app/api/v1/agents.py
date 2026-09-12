"""Agent and agent-version endpoints (spec 18, 19, 62, 63, 64, 67).

The versioning rules are the whole point of this module, and they are what the
rest of the platform depends on:

* An agent holds identity. A **version** holds everything the worker executes.
* Publishing sets a pointer. It never mutates a running call's configuration,
  because a call records its ``agent_version_id`` and the worker caches that
  version for the call's lifetime (spec 19, 45).
* A published version is **immutable**. Editing one would change what a call
  already in progress is doing, so an edit to a published version creates a new
  draft instead.
* Publishing runs validation first (spec 63). An invalid configuration must not
  become production-active.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select

from app.core.dependencies import ClientIp, CurrentTenant, require_permission
from app.db.models import (
    Agent,
    AgentTool,
    AgentVersion,
    KnowledgeBase,
    Model,
    PhoneNumber,
    Provider,
    ProviderCredential,
    Tool,
    Voice,
)
from app.db.repository import TenantRepository
from app.db.util import as_lookup
from app.schemas.agent import (
    AgentCreate,
    AgentResponse,
    AgentUpdate,
    AgentVersionConfig,
    AgentVersionResponse,
    PublishRequest,
    RollbackRequest,
    ValidationIssue,
    ValidationReport,
)
from app.schemas.common import Page
from app.services import audit
from shared.logging import get_logger
from shared.models import AgentVersionState, Permission, ProviderKind, ResourceStatus

logger = get_logger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])

_AGENT_AUDITED = ("name", "description", "status", "published_version_id")
_VERSION_AUDITED = (
    "version_number",
    "state",
    "language",
    "greeting",
    "system_prompt",
    "temperature",
    "interruption_enabled",
    "silence_timeout_seconds",
    "max_call_duration_seconds",
    "recording_enabled",
    "transcription_enabled",
    "transfer_enabled",
)


def _agent_not_found(agent_id: uuid.UUID) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail=f"no agent with id {agent_id}"
    )


# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #


async def _agent_summary(tenant: CurrentTenant, rows: list[Agent]) -> list[AgentResponse]:
    """Attach version counts and the published/draft numbers.

    The list view needs to answer "is this agent live, and is there unpublished
    work" at a glance; without these the rows all look identical.
    """
    if not rows:
        return []

    agent_ids = [row.id for row in rows]

    counts = as_lookup(
        (
            await tenant.session.execute(
                select(AgentVersion.agent_id, func.count())
                .where(
                    AgentVersion.tenant_id == tenant.tenant_id,
                    AgentVersion.agent_id.in_(agent_ids),
                )
                .group_by(AgentVersion.agent_id)
            )
        ).all()
    )

    published_numbers = as_lookup(
        (
            await tenant.session.execute(
                select(AgentVersion.id, AgentVersion.version_number).where(
                    AgentVersion.tenant_id == tenant.tenant_id,
                    AgentVersion.id.in_(
                        [r.published_version_id for r in rows if r.published_version_id]
                    ),
                )
            )
        ).all()
    )

    latest_drafts = as_lookup(
        (
            await tenant.session.execute(
                select(AgentVersion.agent_id, func.max(AgentVersion.version_number))
                .where(
                    AgentVersion.tenant_id == tenant.tenant_id,
                    AgentVersion.agent_id.in_(agent_ids),
                    AgentVersion.state == AgentVersionState.DRAFT,
                )
                .group_by(AgentVersion.agent_id)
            )
        ).all()
    )

    return [
        AgentResponse.model_validate(row, from_attributes=True).model_copy(
            update={
                "version_count": counts.get(row.id, 0),
                "published_version_number": published_numbers.get(row.published_version_id)
                if row.published_version_id
                else None,
                "latest_draft_version_number": latest_drafts.get(row.id),
            }
        )
        for row in rows
    ]


@router.get(
    "",
    response_model=Page[AgentResponse],
    summary="List agents",
    dependencies=[Depends(require_permission(Permission.AGENTS_READ))],
)
async def list_agents(
    tenant: CurrentTenant,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[AgentResponse]:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    rows = await repository.list(Agent, limit=limit, offset=offset, order_by=Agent.name)
    total = await repository.count(Agent)
    return Page(items=await _agent_summary(tenant, rows), total=total, limit=limit, offset=offset)


@router.get(
    "/{agent_id}",
    response_model=AgentResponse,
    summary="Fetch one agent",
    dependencies=[Depends(require_permission(Permission.AGENTS_READ))],
)
async def get_agent(agent_id: uuid.UUID, tenant: CurrentTenant) -> AgentResponse:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(Agent, agent_id)
    if row is None:
        raise _agent_not_found(agent_id)
    return (await _agent_summary(tenant, [row]))[0]


@router.post(
    "",
    response_model=AgentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an agent and its first draft version",
    dependencies=[Depends(require_permission(Permission.AGENTS_WRITE))],
)
async def create_agent(
    payload: AgentCreate,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> AgentResponse:
    """Create an agent.

    A first draft version is created alongside it, because an agent with no
    version cannot be configured and would present an empty builder with no
    obvious next step.
    """
    repository = TenantRepository(tenant.session, tenant.tenant_id)

    clash = await tenant.session.execute(repository.scoped(Agent).where(Agent.name == payload.name))
    if clash.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"an agent named {payload.name!r} already exists",
        )

    agent = Agent(name=payload.name, description=payload.description)
    repository.add(agent)
    await tenant.session.flush()

    draft = AgentVersion(
        agent_id=agent.id,
        version_number=1,
        state=AgentVersionState.DRAFT,
        system_prompt="",
        change_note="Initial draft",
    )
    repository.add(draft)
    await tenant.session.flush()

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="agent.created",
        resource_type="agent",
        resource_id=agent.id,
        new_value=audit.snapshot(agent, *_AGENT_AUDITED),
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()

    logger.info("agent_created", extra={"agent_name": agent.name})
    return (await _agent_summary(tenant, [agent]))[0]


@router.put(
    "/{agent_id}",
    response_model=AgentResponse,
    summary="Update an agent's identity",
    dependencies=[Depends(require_permission(Permission.AGENTS_WRITE))],
)
async def update_agent(
    agent_id: uuid.UUID,
    payload: AgentUpdate,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> AgentResponse:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(Agent, agent_id)
    if row is None:
        raise _agent_not_found(agent_id)

    before = audit.snapshot(row, *_AGENT_AUDITED)
    changes = payload.model_dump(exclude_unset=True)

    if "name" in changes and changes["name"] != row.name:
        clash = await tenant.session.execute(
            repository.scoped(Agent).where(Agent.name == changes["name"], Agent.id != agent_id)
        )
        if clash.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"an agent named {changes['name']!r} already exists",
            )

    for field, value in changes.items():
        setattr(row, field, value)

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="agent.updated",
        resource_type="agent",
        resource_id=row.id,
        old_value=before,
        new_value=audit.snapshot(row, *_AGENT_AUDITED),
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return (await _agent_summary(tenant, [row]))[0]


@router.delete(
    "/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete an agent",
    dependencies=[Depends(require_permission(Permission.AGENTS_WRITE))],
)
async def delete_agent(
    agent_id: uuid.UUID,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> Response:
    """Delete an agent and all its versions.

    Refused while a phone number routes to it: deleting would leave a DID whose
    inbound agent is null, and the failure would appear at the next inbound
    call rather than here.

    Call history survives — ``calls.agent_id`` is SET NULL, so deleting an
    agent does not erase the record of what it did.
    """
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(Agent, agent_id)
    if row is None:
        raise _agent_not_found(agent_id)

    routed = await tenant.session.execute(
        repository.scoped(PhoneNumber).where(PhoneNumber.inbound_agent_id == agent_id)
    )
    numbers = [number.number for number in routed.scalars().all()]
    if numbers:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "this agent still answers phone number(s): "
                f"{', '.join(sorted(numbers))}. Reassign them first."
            ),
        )

    before = audit.snapshot(row, *_AGENT_AUDITED)
    await repository.delete(Agent, agent_id)

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="agent.deleted",
        resource_type="agent",
        resource_id=agent_id,
        old_value=before,
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Versions
# --------------------------------------------------------------------------- #


#: (label, provider column, model column, voice column). One table drives both
#: label resolution and reference validation, so a tier cannot be added to one
#: and forgotten in the other.
_PROVIDER_TIERS: tuple[tuple[str, str, str, str | None], ...] = (
    ("stt_label", "stt_provider_id", "stt_model_id", None),
    ("llm_label", "llm_provider_id", "llm_model_id", None),
    ("tts_label", "tts_provider_id", "tts_model_id", "voice_id"),
    ("stt_fallback_label", "stt_fallback_provider_id", "stt_fallback_model_id", None),
    ("llm_fallback_label", "llm_fallback_provider_id", "llm_fallback_model_id", None),
    (
        "tts_fallback_label",
        "tts_fallback_provider_id",
        "tts_fallback_model_id",
        "tts_fallback_voice_id",
    ),
    ("stt_local_label", "stt_local_provider_id", "stt_local_model_id", None),
    ("tts_local_label", "tts_local_provider_id", "tts_local_model_id", "tts_local_voice_id"),
)


async def _version_labels(tenant: CurrentTenant, version: AgentVersion) -> dict[str, str | None]:
    """Human-readable provider, model and voice names for the builder.

    Three queries regardless of how many tiers a version fills in. The earlier
    shape issued one per foreign key, which was tolerable for three labels and
    would not have been for eight — and ``list_versions`` calls this once per
    row, so the cost multiplies twice over.
    """
    provider_ids = {
        getattr(version, column)
        for _label, column, _model, _voice in _PROVIDER_TIERS
        if getattr(version, column) is not None
    }
    model_ids = {
        getattr(version, column)
        for _label, _provider, column, _voice in _PROVIDER_TIERS
        if getattr(version, column) is not None
    }
    voice_ids = {
        getattr(version, column)
        for _label, _provider, _model, column in _PROVIDER_TIERS
        if column is not None and getattr(version, column) is not None
    }

    providers: dict[uuid.UUID, Provider] = {}
    models: dict[uuid.UUID, Model] = {}
    voices: dict[uuid.UUID, Voice] = {}

    if provider_ids:
        providers = {
            row.id: row
            for row in (
                await tenant.session.execute(select(Provider).where(Provider.id.in_(provider_ids)))
            ).scalars()
        }
    if model_ids:
        models = {
            row.id: row
            for row in (
                await tenant.session.execute(select(Model).where(Model.id.in_(model_ids)))
            ).scalars()
        }
    if voice_ids:
        voices = {
            row.id: row
            for row in (
                await tenant.session.execute(select(Voice).where(Voice.id.in_(voice_ids)))
            ).scalars()
        }

    labels: dict[str, str | None] = {}
    for label, provider_column, model_column, voice_column in _PROVIDER_TIERS:
        provider = providers.get(getattr(version, provider_column))
        if provider is None:
            labels[label] = None
            continue
        model = models.get(getattr(version, model_column))
        labels[label] = (
            f"{provider.display_name} / {model.slug}" if model else provider.display_name
        )
        if voice_column is not None and label == "tts_label":
            voice = voices.get(getattr(version, voice_column))
            labels["voice_label"] = voice.name if voice else None

    labels.setdefault("voice_label", None)
    return labels


async def _version_tools(tenant: CurrentTenant, version_id: uuid.UUID) -> dict:
    """The allow-list for this version (spec 32)."""
    rows = (
        await tenant.session.execute(
            select(AgentTool.tool_id, Tool.name)
            .join(Tool, Tool.id == AgentTool.tool_id)
            .where(
                AgentTool.tenant_id == tenant.tenant_id,
                AgentTool.agent_version_id == version_id,
                AgentTool.enabled.is_(True),
            )
            .order_by(Tool.name)
        )
    ).all()
    return {
        "tool_ids": [tool_id for tool_id, _name in rows],
        "tool_names": [name for _tool_id, name in rows],
    }


async def _copy_tool_grants(
    tenant: CurrentTenant, *, source_version_id: uuid.UUID, target: AgentVersion
) -> None:
    """Copy the allow-list onto a new draft so a live version stays unchanged."""
    grants = (
        (
            await tenant.session.execute(
                select(AgentTool).where(
                    AgentTool.tenant_id == tenant.tenant_id,
                    AgentTool.agent_version_id == source_version_id,
                )
            )
        )
        .scalars()
        .all()
    )
    for grant in grants:
        tenant.session.add(
            AgentTool(
                tenant_id=tenant.tenant_id,
                tool_id=grant.tool_id,
                agent_version_id=target.id,
                enabled=grant.enabled,
            )
        )
    if grants:
        await tenant.session.flush()


async def _replace_tool_grants(
    tenant: CurrentTenant, version: AgentVersion, tool_ids: list[uuid.UUID]
) -> None:
    existing = (
        (
            await tenant.session.execute(
                select(AgentTool).where(
                    AgentTool.tenant_id == tenant.tenant_id,
                    AgentTool.agent_version_id == version.id,
                )
            )
        )
        .scalars()
        .all()
    )
    for row in existing:
        await tenant.session.delete(row)
    for tool_id in dict.fromkeys(tool_ids):
        tenant.session.add(
            AgentTool(
                tenant_id=tenant.tenant_id,
                agent_version_id=version.id,
                tool_id=tool_id,
                enabled=True,
            )
        )
    await tenant.session.flush()


async def _version_response(tenant: CurrentTenant, version: AgentVersion) -> AgentVersionResponse:
    """Serialise a version, refreshing what the database wrote itself.

    ``updated_at`` carries ``onupdate=func.now()``, so after an UPDATE its
    value lives in PostgreSQL and not in the instance. Reading it during
    serialisation then attempts lazy IO from pydantic's synchronous context and
    raises ``MissingGreenlet`` — an error that names greenlets and not the
    actual problem.

    This was reachable before the provider tiers existed but almost never hit:
    ``onupdate`` only fires when a column actually changes, and the builder
    used to send so few fields that a save was frequently a no-op. Adding the
    tier fields made every save a real UPDATE, which turned an occasional
    failure into a certain one.
    """
    await tenant.session.refresh(version)
    payload = AgentVersionResponse.model_validate(version, from_attributes=True)
    grants = await _version_tools(tenant, version.id)
    labels = await _version_labels(tenant, version)
    return payload.model_copy(update={**labels, **grants})


@router.get(
    "/{agent_id}/versions",
    response_model=list[AgentVersionResponse],
    summary="List an agent's versions, newest first",
    dependencies=[Depends(require_permission(Permission.AGENTS_READ))],
)
async def list_versions(agent_id: uuid.UUID, tenant: CurrentTenant) -> list[AgentVersionResponse]:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    if not await repository.exists(Agent, agent_id):
        raise _agent_not_found(agent_id)

    rows = (
        (
            await tenant.session.execute(
                repository.scoped(AgentVersion)
                .where(AgentVersion.agent_id == agent_id)
                .order_by(AgentVersion.version_number.desc())
            )
        )
        .scalars()
        .all()
    )

    return [await _version_response(tenant, row) for row in rows]


async def _editable_draft(
    tenant: CurrentTenant, repository: TenantRepository, agent: Agent
) -> AgentVersion:
    """The draft to write into, creating one if the latest version is published.

    This is what keeps a published version immutable (spec 19): editing a live
    agent produces a new draft seeded from the published configuration, so a
    call already running on the published version is unaffected.
    """
    latest = (
        await tenant.session.execute(
            repository.scoped(AgentVersion)
            .where(AgentVersion.agent_id == agent.id)
            .order_by(AgentVersion.version_number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if latest is not None and latest.state == AgentVersionState.DRAFT:
        return latest

    next_number = (latest.version_number + 1) if latest else 1

    # Copy the published configuration forward, so an edit starts from what is
    # live rather than from an empty form.
    copied: dict = {}
    if latest is not None:
        for column in (c.name for c in AgentVersion.__table__.columns):
            if column in {
                "id",
                "tenant_id",
                "agent_id",
                "version_number",
                "state",
                "published_at",
                "published_by_user_id",
                "created_at",
                "updated_at",
                "change_note",
                "validation_errors",
            }:
                continue
            copied[column] = getattr(latest, column)

    draft = AgentVersion(
        agent_id=agent.id,
        version_number=next_number,
        state=AgentVersionState.DRAFT,
        change_note=f"Draft from v{latest.version_number}" if latest else "Initial draft",
        **copied,
    )
    repository.add(draft)
    await tenant.session.flush()
    if latest is not None:
        await _copy_tool_grants(tenant, source_version_id=latest.id, target=draft)
    logger.info(
        "agent_draft_created",
        extra={"agent_id": str(agent.id), "version_number": next_number},
    )
    return draft


@router.put(
    "/{agent_id}/draft",
    response_model=AgentVersionResponse,
    summary="Save the agent's draft configuration",
    dependencies=[Depends(require_permission(Permission.AGENTS_WRITE))],
)
async def save_draft(
    agent_id: uuid.UUID,
    payload: AgentVersionConfig,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> AgentVersionResponse:
    """Write the builder's fields into the current draft (spec 62 "Save Draft").

    If the latest version is published, a new draft is created first — a
    published version is never edited in place.
    """
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    agent = await repository.get(Agent, agent_id)
    if agent is None:
        raise _agent_not_found(agent_id)

    draft = await _editable_draft(tenant, repository, agent)
    before = audit.snapshot(draft, *_VERSION_AUDITED)

    changes = payload.model_dump(exclude_unset=True)
    tool_ids = changes.pop("tool_ids", None)
    await _validate_references(tenant, repository, changes, tool_ids=tool_ids)

    for field, value in changes.items():
        setattr(draft, field, value)
    if tool_ids is not None:
        await _replace_tool_grants(tenant, draft, tool_ids)

    # The stored validation result is now stale; recompute so the builder can
    # show the current state without a second request.
    report = await _validate_version(tenant, repository, draft)
    draft.validation_errors = [issue.model_dump() for issue in report.issues]

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="agent.draft_saved",
        resource_type="agent_version",
        resource_id=draft.id,
        old_value=before,
        new_value=audit.snapshot(draft, *_VERSION_AUDITED),
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return await _version_response(tenant, draft)


async def _validate_references(
    tenant: CurrentTenant,
    repository: TenantRepository,
    changes: dict,
    *,
    tool_ids: list[uuid.UUID] | None = None,
) -> None:
    """Check provider, model, voice, knowledge-base and tool ids exist.

    Providers, models and voices are platform-level, so they are looked up
    without tenant scoping; the knowledge base and tools are tenant-owned and
    go through the repository.
    """
    # Derived from the tier table rather than listed again: a new tier that is
    # rendered in the builder but not validated here would accept a dangling
    # foreign key and fail at call setup instead.
    references: list[tuple[str, type[Provider] | type[Model] | type[Voice]]] = []
    for _label, provider_column, model_column, voice_column in _PROVIDER_TIERS:
        references.append((provider_column, Provider))
        references.append((model_column, Model))
        if voice_column is not None:
            references.append((voice_column, Voice))

    for field, model in references:
        value = changes.get(field)
        if value is None:
            continue
        exists = (
            await tenant.session.execute(select(model.id).where(model.id == value))
        ).scalar_one_or_none()
        if exists is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{field}: no such record in the platform catalog",
            )

    knowledge_base_id = changes.get("knowledge_base_id")
    if knowledge_base_id is not None and not await repository.exists(
        KnowledgeBase, knowledge_base_id
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="that knowledge base does not exist in this tenant",
        )

    for emb_field, emb_model in (
        ("embedding_provider_id", Provider),
        ("embedding_model_id", Model),
    ):
        value = changes.get(emb_field)
        if value is None:
            continue
        exists = (
            await tenant.session.execute(select(emb_model.id).where(emb_model.id == value))
        ).scalar_one_or_none()
        if exists is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{emb_field}: no such record in the platform catalog",
            )

    if tool_ids:
        found = set(
            (
                await tenant.session.execute(
                    select(Tool.id).where(
                        Tool.tenant_id == tenant.tenant_id, Tool.id.in_(tool_ids)
                    )
                )
            ).scalars()
        )
        if any(tool_id not in found for tool_id in tool_ids):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="one or more tools are not in this tenant's library",
            )


# --------------------------------------------------------------------------- #
# Validation (spec 63, 64)
# --------------------------------------------------------------------------- #


async def _validate_version(
    tenant: CurrentTenant, repository: TenantRepository, version: AgentVersion
) -> ValidationReport:
    """Decide whether a version may become production-active (spec 63).

    Each issue names the field so the builder can highlight the control rather
    than only showing a message — the difference between an error a
    non-developer can act on and one they cannot.

    The dependency list is spec 64's tree, flattened: it reports not just
    whether something is chosen but whether the chosen thing is usable, which
    is what catches a disabled provider or a missing credential.
    """
    issues: list[ValidationIssue] = []
    dependencies: list[ValidationIssue] = []

    if not (version.system_prompt or "").strip():
        issues.append(
            ValidationIssue(
                field="system_prompt",
                message="a system prompt is required; without one the agent has no instructions",
            )
        )

    async def check_provider(
        kind: ProviderKind, provider_id: uuid.UUID | None, model_id: uuid.UUID | None, field: str
    ) -> None:
        if provider_id is None:
            issues.append(ValidationIssue(field=field, message=f"choose a {kind.value} provider"))
            dependencies.append(ValidationIssue(field=field, message=f"{kind.value}: not selected"))
            return

        provider = (
            await tenant.session.execute(select(Provider).where(Provider.id == provider_id))
        ).scalar_one_or_none()

        if provider is None:
            issues.append(
                ValidationIssue(field=field, message=f"the selected {kind.value} provider is gone")
            )
            return

        if provider.status is not ResourceStatus.ACTIVE:
            # Spec 64: a disabled provider must show clearly as the problem.
            issues.append(
                ValidationIssue(
                    field=field,
                    message=(
                        f"{provider.display_name} is {provider.status.value.lower()} "
                        "and cannot be used"
                    ),
                )
            )
            dependencies.append(
                ValidationIssue(
                    field=field,
                    message=f"{kind.value}: {provider.display_name} is disabled",
                )
            )
            return

        credential = (
            (
                await tenant.session.execute(
                    repository.scoped(ProviderCredential).where(
                        ProviderCredential.provider_id == provider_id,
                        ProviderCredential.status == ResourceStatus.ACTIVE,
                    )
                )
            )
            .scalars()
            .first()
        )

        # A self-hosted provider authenticates by network reachability rather
        # than by an API key, so demanding a credential here would make a valid
        # self-hosted configuration unpublishable — and spec 25 requires
        # supporting exactly that.
        if credential is None and provider.requires_credential:
            issues.append(
                ValidationIssue(
                    field=field,
                    message=(
                        f"no API credential is configured for {provider.display_name}, "
                        "so a call using it would fail"
                    ),
                )
            )
            dependencies.append(
                ValidationIssue(
                    field=field, message=f"{kind.value}: {provider.display_name} has no credential"
                )
            )
            return

        if model_id is None:
            issues.append(
                ValidationIssue(
                    field=field.replace("provider", "model"), message=f"choose a {kind.value} model"
                )
            )
            return

        dependencies.append(
            ValidationIssue(
                field=field,
                message=f"{kind.value}: {provider.display_name} ready",
                severity="ok",
            )
        )

    await check_provider(
        ProviderKind.STT, version.stt_provider_id, version.stt_model_id, "stt_provider_id"
    )
    await check_provider(
        ProviderKind.LLM, version.llm_provider_id, version.llm_model_id, "llm_provider_id"
    )
    await check_provider(
        ProviderKind.TTS, version.tts_provider_id, version.tts_model_id, "tts_provider_id"
    )

    if version.tts_provider_id is not None and version.voice_id is None:
        issues.append(ValidationIssue(field="voice_id", message="choose a voice"))
    elif version.voice_id is not None:
        voice = (
            await tenant.session.execute(select(Voice).where(Voice.id == version.voice_id))
        ).scalar_one_or_none()
        if voice is None:
            issues.append(ValidationIssue(field="voice_id", message="the selected voice is gone"))
        elif voice.status is not ResourceStatus.ACTIVE:
            issues.append(
                ValidationIssue(
                    field="voice_id",
                    message=f"the voice {voice.name!r} is disabled and cannot be used",
                )
            )
        else:
            dependencies.append(
                ValidationIssue(field="voice_id", message=f"Voice: {voice.name}", severity="ok")
            )

    if version.transfer_enabled and not (version.transfer_announcement_text or "").strip():
        # CR-1 TR-2: the caller must hear something when a transfer starts.
        issues.append(
            ValidationIssue(
                field="transfer_announcement_text",
                message=(
                    "transfer is enabled but there is no announcement, so the caller "
                    "would hear silence while the agent is reached"
                ),
            )
        )

    if version.knowledge_base_id is not None:
        base = await repository.get(KnowledgeBase, version.knowledge_base_id)
        if base is None:
            issues.append(
                ValidationIssue(field="knowledge_base_id", message="the knowledge base is gone")
            )
        elif base.status is not ResourceStatus.ACTIVE:
            issues.append(
                ValidationIssue(
                    field="knowledge_base_id",
                    message=f"the knowledge base {base.name!r} is disabled",
                )
            )
        # When a knowledge base is set, an embedding provider is needed for RAG
        # retrieval. Missing one is a warning rather than a hard block in Phase
        # 4c, because ingestion is not yet active (Phase 6). Agents already
        # published without it must not be broken by a new strict rule.
        if version.embedding_provider_id is None:
            issues.append(
                ValidationIssue(
                    field="embedding_provider_id",
                    message=(
                        "a knowledge base is set but no embedding provider is chosen; "
                        "retrieval will not work until one is configured in AI Setup "
                        "and selected here (Phase 6)"
                    ),
                    severity="warning",
                )
            )

    grants = (
        (
            await tenant.session.execute(
                select(AgentTool, Tool)
                .join(Tool, Tool.id == AgentTool.tool_id)
                .where(
                    AgentTool.tenant_id == tenant.tenant_id,
                    AgentTool.agent_version_id == version.id,
                    AgentTool.enabled.is_(True),
                )
            )
        )
        .all()
    )
    for _grant, tool in grants:
        if tool.status is not ResourceStatus.ACTIVE:
            issues.append(
                ValidationIssue(
                    field="tool_ids",
                    message=f"tool {tool.name!r} is {tool.status.value.lower()} and cannot be used",
                )
            )
        elif not tool.schema_valid:
            issues.append(
                ValidationIssue(
                    field="tool_ids",
                    message=(
                        f"tool {tool.name!r} has an invalid request schema"
                        + (
                            f": {tool.schema_validation_error}"
                            if tool.schema_validation_error
                            else ""
                        )
                    ),
                )
            )
        else:
            dependencies.append(
                ValidationIssue(
                    field="tool_ids",
                    message=f"Tool: {tool.name} ready",
                    severity="ok",
                )
            )

    return ValidationReport(
        publishable=not any(issue.severity == "error" for issue in issues),
        issues=issues,
        dependencies=dependencies,
    )


@router.get(
    "/{agent_id}/versions/{version_number}/validate",
    response_model=ValidationReport,
    summary="Validate a version before publishing",
    dependencies=[Depends(require_permission(Permission.AGENTS_READ))],
)
async def validate_version(
    agent_id: uuid.UUID, version_number: int, tenant: CurrentTenant
) -> ValidationReport:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    version = await _load_version(tenant, repository, agent_id, version_number)
    return await _validate_version(tenant, repository, version)


async def _load_version(
    tenant: CurrentTenant,
    repository: TenantRepository,
    agent_id: uuid.UUID,
    version_number: int,
) -> AgentVersion:
    if not await repository.exists(Agent, agent_id):
        raise _agent_not_found(agent_id)
    version = (
        await tenant.session.execute(
            repository.scoped(AgentVersion).where(
                AgentVersion.agent_id == agent_id,
                AgentVersion.version_number == version_number,
            )
        )
    ).scalar_one_or_none()
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"this agent has no version {version_number}",
        )
    return version


# --------------------------------------------------------------------------- #
# Publish and rollback (spec 19)
# --------------------------------------------------------------------------- #


@router.post(
    "/{agent_id}/publish",
    response_model=AgentVersionResponse,
    summary="Publish the current draft",
    dependencies=[Depends(require_permission(Permission.AGENTS_PUBLISH))],
)
async def publish_agent(
    agent_id: uuid.UUID,
    payload: PublishRequest,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> AgentVersionResponse:
    """Publish the latest draft (spec 19).

    Validation runs first and a failure is a 422 listing every issue, because
    spec 63 forbids an invalid configuration becoming production-active.

    Calls already in progress are untouched: each recorded its
    ``agent_version_id`` at start and the worker cached that version, so this
    only affects calls that arrive afterwards.
    """
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    agent = await repository.get(Agent, agent_id)
    if agent is None:
        raise _agent_not_found(agent_id)

    draft = (
        await tenant.session.execute(
            repository.scoped(AgentVersion)
            .where(
                AgentVersion.agent_id == agent_id,
                AgentVersion.state == AgentVersionState.DRAFT,
            )
            .order_by(AgentVersion.version_number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if draft is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="there is no draft to publish; edit the agent to create one",
        )

    report = await _validate_version(tenant, repository, draft)
    draft.validation_errors = [issue.model_dump() for issue in report.issues]

    if not report.publishable:
        await tenant.session.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "this version cannot be published yet",
                "issues": [issue.model_dump() for issue in report.issues],
            },
        )

    previously_published = agent.published_version_id

    # Archive the version being replaced, so the state list stays truthful:
    # exactly one PUBLISHED version per agent at any time.
    if previously_published:
        old = (
            await tenant.session.execute(
                repository.scoped(AgentVersion).where(AgentVersion.id == previously_published)
            )
        ).scalar_one_or_none()
        if old is not None and old.state == AgentVersionState.PUBLISHED:
            old.state = AgentVersionState.ARCHIVED

    draft.state = AgentVersionState.PUBLISHED
    draft.published_at = datetime.now(UTC)
    draft.published_by_user_id = tenant.principal.user_id
    if payload.change_note:
        draft.change_note = payload.change_note
    agent.published_version_id = draft.id

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="agent.published",
        resource_type="agent_version",
        resource_id=draft.id,
        old_value={
            "published_version_id": str(previously_published) if previously_published else None
        },
        new_value={
            "published_version_id": str(draft.id),
            "version_number": draft.version_number,
            "change_note": draft.change_note,
        },
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()

    logger.info(
        "agent_published",
        extra={"agent_name": agent.name, "version_number": draft.version_number},
    )
    return await _version_response(tenant, draft)


@router.post(
    "/{agent_id}/rollback",
    response_model=AgentVersionResponse,
    summary="Publish an earlier version instead",
    dependencies=[Depends(require_permission(Permission.AGENTS_PUBLISH))],
)
async def rollback_agent(
    agent_id: uuid.UUID,
    payload: RollbackRequest,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> AgentVersionResponse:
    """Roll back to a named version (spec 18, 19).

    The version is named explicitly rather than inferred as "the previous one":
    after several rollbacks, "previous" is ambiguous, and guessing wrong here
    changes which configuration answers every new call.

    Re-validated before publishing, because the world may have moved since:
    a provider it depends on could have been disabled or lost its credential.
    """
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    agent = await repository.get(Agent, agent_id)
    if agent is None:
        raise _agent_not_found(agent_id)

    target = await _load_version(tenant, repository, agent_id, payload.version_number)

    if target.id == agent.published_version_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"v{payload.version_number} is already the published version",
        )

    report = await _validate_version(tenant, repository, target)
    if not report.publishable:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": (
                    f"v{payload.version_number} is no longer valid, so rolling back to it "
                    "would publish a configuration that cannot serve a call"
                ),
                "issues": [issue.model_dump() for issue in report.issues],
            },
        )

    previous = agent.published_version_id
    if previous:
        old = (
            await tenant.session.execute(
                repository.scoped(AgentVersion).where(AgentVersion.id == previous)
            )
        ).scalar_one_or_none()
        if old is not None and old.state == AgentVersionState.PUBLISHED:
            old.state = AgentVersionState.ARCHIVED

    target.state = AgentVersionState.PUBLISHED
    target.published_at = datetime.now(UTC)
    target.published_by_user_id = tenant.principal.user_id
    agent.published_version_id = target.id

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="agent.rolled_back",
        resource_type="agent_version",
        resource_id=target.id,
        old_value={"published_version_id": str(previous) if previous else None},
        new_value={
            "published_version_id": str(target.id),
            "version_number": target.version_number,
        },
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()

    logger.info(
        "agent_rolled_back",
        extra={"agent_name": agent.name, "version_number": target.version_number},
    )
    return await _version_response(tenant, target)
