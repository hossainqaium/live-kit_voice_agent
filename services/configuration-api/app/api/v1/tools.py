"""HTTP tool and knowledge base endpoints (spec 30, 31, 32, 33)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select

from app.core.dependencies import ClientIp, CurrentTenant, require_permission
from app.db.models import (
    AgentTool,
    AgentVersion,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    Tool,
)
from app.db.repository import TenantRepository
from app.schemas.common import Page
from app.schemas.tool import (
    KnowledgeBaseCreate,
    KnowledgeBaseResponse,
    KnowledgeBaseUpdate,
    KnowledgeDocumentResponse,
    ToolCreate,
    ToolResponse,
    ToolUpdate,
)
from app.services import audit
from shared.crypto import CredentialCipher, CredentialEncryptionError
from shared.logging import get_logger
from shared.models import Permission
from shared.tools import extract_variables, is_builtin, validate_request_schema

logger = get_logger(__name__)

router = APIRouter(tags=["tools"])

_TOOL_AUDITED = (
    "name",
    "description",
    "http_method",
    "url_template",
    "auth_type",
    "timeout_seconds",
    "max_retries",
    "status",
)
_KB_AUDITED = ("name", "description", "top_k", "status")

def _variables(tool: Tool) -> list[str]:
    """Variables the URL and headers reference.

    Surfaced so the UI can show what the model must supply, and so a typo in a
    placeholder is visible before a call rather than as a failed tool
    invocation mid-conversation.
    """
    header_values = [str(value) for value in (tool.headers or {}).values()]
    return extract_variables(tool.url_template or "", *header_values)


def _validate_schema(tool: Tool) -> tuple[bool, str | None]:
    """Check the request schema is usable as a function definition (spec 31)."""
    return validate_request_schema(tool.request_schema, _variables(tool))


async def _tool_response(tool: Tool, *, has_secret: bool) -> ToolResponse:
    return ToolResponse.model_validate(tool, from_attributes=True).model_copy(
        update={
            "has_secret": has_secret,
            "variables": _variables(tool),
            "is_builtin": is_builtin(tool.url_template),
        }
    )


@router.get(
    "/tools",
    response_model=Page[ToolResponse],
    summary="List HTTP tools",
    dependencies=[Depends(require_permission(Permission.AGENTS_READ))],
)
async def list_tools(
    tenant: CurrentTenant,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[ToolResponse]:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    rows = await repository.list(Tool, limit=limit, offset=offset, order_by=Tool.name)
    total = await repository.count(Tool)
    return Page(
        items=[
            await _tool_response(row, has_secret=row.auth_secret_ciphertext is not None)
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/tools",
    response_model=ToolResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an HTTP tool",
    dependencies=[Depends(require_permission(Permission.AGENTS_WRITE))],
)
async def create_tool(
    payload: ToolCreate,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> ToolResponse:
    repository = TenantRepository(tenant.session, tenant.tenant_id)

    clash = await tenant.session.execute(repository.scoped(Tool).where(Tool.name == payload.name))
    if clash.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a tool named {payload.name!r} already exists",
        )

    fields = payload.model_dump(exclude={"auth_secret"})
    row = Tool(**fields)

    if payload.auth_secret:
        try:
            cipher = CredentialCipher.from_env()
        except CredentialEncryptionError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"credential encryption is not configured: {exc}",
            ) from exc
        encrypted = cipher.encrypt(payload.auth_secret)
        row.auth_secret_ciphertext = encrypted.ciphertext
        row.encryption_key_version = encrypted.key_version

    valid, error = _validate_schema(row)
    row.schema_valid = valid
    row.schema_validation_error = error

    repository.add(row)
    await tenant.session.flush()

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="tool.created",
        resource_type="tool",
        resource_id=row.id,
        new_value=audit.snapshot(row, *_TOOL_AUDITED),
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return await _tool_response(row, has_secret=bool(payload.auth_secret))


@router.put(
    "/tools/{tool_id}",
    response_model=ToolResponse,
    summary="Update an HTTP tool",
    dependencies=[Depends(require_permission(Permission.AGENTS_WRITE))],
)
async def update_tool(
    tool_id: uuid.UUID,
    payload: ToolUpdate,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> ToolResponse:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(Tool, tool_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no tool with id {tool_id}")

    before = audit.snapshot(row, *_TOOL_AUDITED)
    changes = payload.model_dump(exclude_unset=True, exclude={"auth_secret"})
    for field, value in changes.items():
        setattr(row, field, value)

    if payload.auth_secret:
        cipher = CredentialCipher.from_env()
        encrypted = cipher.encrypt(payload.auth_secret)
        row.auth_secret_ciphertext = encrypted.ciphertext
        row.encryption_key_version = encrypted.key_version

    valid, error = _validate_schema(row)
    row.schema_valid = valid
    row.schema_validation_error = error

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="tool.updated",
        resource_type="tool",
        resource_id=row.id,
        old_value=before,
        new_value=audit.snapshot(row, *_TOOL_AUDITED),
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return await _tool_response(row, has_secret=row.auth_secret_ciphertext is not None)


@router.delete(
    "/tools/{tool_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete an HTTP tool",
    dependencies=[Depends(require_permission(Permission.AGENTS_WRITE))],
)
async def delete_tool(
    tool_id: uuid.UUID, tenant: CurrentTenant, request: Request, client_ip: ClientIp
) -> Response:
    """Delete a tool.

    Refused while an agent version still grants it, because removing it would
    silently change what that agent can do — and spec 32 makes the allow-list
    explicit precisely so that cannot happen by accident.
    """
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(Tool, tool_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no tool with id {tool_id}")

    granted = await tenant.session.execute(
        select(func.count())
        .select_from(AgentTool)
        .where(AgentTool.tenant_id == tenant.tenant_id, AgentTool.tool_id == tool_id)
    )
    if int(granted.scalar_one()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "this tool is still granted to an agent version. Remove the grant "
                "first, so an agent's capabilities never change silently."
            ),
        )

    before = audit.snapshot(row, *_TOOL_AUDITED)
    await repository.delete(Tool, tool_id)
    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="tool.deleted",
        resource_type="tool",
        resource_id=tool_id,
        old_value=before,
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Knowledge bases (spec 33)
# --------------------------------------------------------------------------- #


async def _kb_response(tenant: CurrentTenant, row: KnowledgeBase) -> KnowledgeBaseResponse:
    documents = int(
        (
            await tenant.session.execute(
                select(func.count())
                .select_from(KnowledgeDocument)
                .where(
                    KnowledgeDocument.tenant_id == tenant.tenant_id,
                    KnowledgeDocument.knowledge_base_id == row.id,
                )
            )
        ).scalar_one()
    )
    indexed = int(
        (
            await tenant.session.execute(
                select(func.count())
                .select_from(KnowledgeDocument)
                .where(
                    KnowledgeDocument.tenant_id == tenant.tenant_id,
                    KnowledgeDocument.knowledge_base_id == row.id,
                    KnowledgeDocument.status == "INDEXED",
                )
            )
        ).scalar_one()
    )
    chunks = int(
        (
            await tenant.session.execute(
                select(func.count())
                .select_from(KnowledgeChunk)
                .where(
                    KnowledgeChunk.tenant_id == tenant.tenant_id,
                    KnowledgeChunk.knowledge_base_id == row.id,
                )
            )
        ).scalar_one()
    )
    return KnowledgeBaseResponse.model_validate(row, from_attributes=True).model_copy(
        update={"document_count": documents, "indexed_count": indexed, "chunk_count": chunks}
    )


@router.get(
    "/knowledge-bases",
    response_model=Page[KnowledgeBaseResponse],
    summary="List knowledge bases",
    dependencies=[Depends(require_permission(Permission.AGENTS_READ))],
)
async def list_knowledge_bases(
    tenant: CurrentTenant,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[KnowledgeBaseResponse]:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    rows = await repository.list(
        KnowledgeBase, limit=limit, offset=offset, order_by=KnowledgeBase.name
    )
    total = await repository.count(KnowledgeBase)
    return Page(
        items=[await _kb_response(tenant, row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/knowledge-bases",
    response_model=KnowledgeBaseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a knowledge base",
    dependencies=[Depends(require_permission(Permission.AGENTS_WRITE))],
)
async def create_knowledge_base(
    payload: KnowledgeBaseCreate,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> KnowledgeBaseResponse:
    """Create a knowledge base.

    Document ingestion and retrieval are Phase 6; this creates the container
    and its retrieval settings so an agent can be pointed at it.
    """
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    clash = await tenant.session.execute(
        repository.scoped(KnowledgeBase).where(KnowledgeBase.name == payload.name)
    )
    if clash.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a knowledge base named {payload.name!r} already exists",
        )

    row = KnowledgeBase(**payload.model_dump())
    repository.add(row)
    await tenant.session.flush()

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="knowledge_base.created",
        resource_type="knowledge_base",
        resource_id=row.id,
        new_value=audit.snapshot(row, *_KB_AUDITED),
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return await _kb_response(tenant, row)


@router.put(
    "/knowledge-bases/{kb_id}",
    response_model=KnowledgeBaseResponse,
    summary="Update a knowledge base",
    dependencies=[Depends(require_permission(Permission.AGENTS_WRITE))],
)
async def update_knowledge_base(
    kb_id: uuid.UUID,
    payload: KnowledgeBaseUpdate,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> KnowledgeBaseResponse:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(KnowledgeBase, kb_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no knowledge base with id {kb_id}")

    before = audit.snapshot(row, *_KB_AUDITED)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field, value)

    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="knowledge_base.updated",
        resource_type="knowledge_base",
        resource_id=row.id,
        old_value=before,
        new_value=audit.snapshot(row, *_KB_AUDITED),
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return await _kb_response(tenant, row)


@router.get(
    "/knowledge-bases/{kb_id}/documents",
    response_model=Page[KnowledgeDocumentResponse],
    summary="List a knowledge base's documents",
    dependencies=[Depends(require_permission(Permission.AGENTS_READ))],
)
async def list_documents(
    kb_id: uuid.UUID,
    tenant: CurrentTenant,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[KnowledgeDocumentResponse]:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    if not await repository.exists(KnowledgeBase, kb_id):
        raise HTTPException(status_code=404, detail=f"no knowledge base with id {kb_id}")

    rows = list(
        (
            await tenant.session.execute(
                repository.scoped(KnowledgeDocument)
                .where(KnowledgeDocument.knowledge_base_id == kb_id)
                .order_by(KnowledgeDocument.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    total = int(
        (
            await tenant.session.execute(
                select(func.count())
                .select_from(KnowledgeDocument)
                .where(
                    KnowledgeDocument.tenant_id == tenant.tenant_id,
                    KnowledgeDocument.knowledge_base_id == kb_id,
                )
            )
        ).scalar_one()
    )
    return Page(
        items=[KnowledgeDocumentResponse.model_validate(r, from_attributes=True) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.delete(
    "/knowledge-bases/{kb_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a knowledge base",
    dependencies=[Depends(require_permission(Permission.AGENTS_WRITE))],
)
async def delete_knowledge_base(
    kb_id: uuid.UUID, tenant: CurrentTenant, request: Request, client_ip: ClientIp
) -> Response:
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    if not await repository.exists(KnowledgeBase, kb_id):
        raise HTTPException(status_code=404, detail=f"no knowledge base with id {kb_id}")

    using = await tenant.session.execute(
        repository.scoped(AgentVersion).where(AgentVersion.knowledge_base_id == kb_id)
    )
    versions = [f"v{v.version_number}" for v in using.scalars().all()]
    if versions:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"still used by agent version(s): {', '.join(sorted(set(versions)))}",
        )

    await repository.delete(KnowledgeBase, kb_id)
    await audit.record(
        tenant.session,
        principal=tenant.principal,
        action="knowledge_base.deleted",
        resource_type="knowledge_base",
        resource_id=kb_id,
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
