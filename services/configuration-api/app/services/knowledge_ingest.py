"""Ingest a knowledge document: extract, chunk, embed, store (spec 33)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import KnowledgeBase, KnowledgeChunk, KnowledgeDocument, Provider, ProviderCredential
from app.services import object_store
from shared.crypto import CredentialCipher, CredentialEncryptionError
from shared.knowledge import (
    DEFAULT_EMBEDDING_MODEL,
    embed_texts,
    extract_text,
    format_vector,
)
from shared.knowledge import chunk_text as split_chunks
from shared.logging import get_logger
from shared.models import DocumentStatus, KnowledgeSourceType, ProviderKind, ResourceStatus

logger = get_logger(__name__)


class IngestError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


async def resolve_embedding_auth(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    knowledge_base: KnowledgeBase,
) -> tuple[str | None, str | None, str]:
    """Return ``(api_key, base_url, model)`` for this base.

    Prefers the base's own embedding provider, then any EMBEDDING credential,
    then an OpenAI-compatible LLM key (the same key serves ``/embeddings``).
    """
    model = knowledge_base.embedding_model_slug or DEFAULT_EMBEDDING_MODEL
    provider_id = knowledge_base.embedding_provider_id
    if provider_id is None:
        provider_id = await _first_embedding_or_llm_provider(session, tenant_id)
    if provider_id is None:
        return None, None, model

    credential = (
        await session.execute(
            select(ProviderCredential).where(
                ProviderCredential.tenant_id == tenant_id,
                ProviderCredential.provider_id == provider_id,
                ProviderCredential.status == ResourceStatus.ACTIVE,
            )
        )
    ).scalar_one_or_none()
    provider = await session.get(Provider, provider_id)
    base_url = None
    api_key = None
    if credential is not None:
        base_url = credential.base_url
        try:
            cipher = CredentialCipher.from_env()
            api_key = cipher.decrypt(
                bytes(credential.api_key_ciphertext), credential.encryption_key_version
            )
        except (CredentialEncryptionError, ValueError) as exc:
            raise IngestError(f"could not decrypt the embedding credential: {exc}") from exc
    if provider is not None and base_url is None:
        base_url = provider.default_base_url
    return api_key, base_url, model


async def _first_embedding_or_llm_provider(
    session: AsyncSession, tenant_id: uuid.UUID
) -> uuid.UUID | None:
    row = (
        await session.execute(
            select(ProviderCredential.provider_id)
            .join(Provider, Provider.id == ProviderCredential.provider_id)
            .where(
                ProviderCredential.tenant_id == tenant_id,
                ProviderCredential.status == ResourceStatus.ACTIVE,
                Provider.kind.in_((ProviderKind.EMBEDDING, ProviderKind.LLM)),
            )
            .order_by(Provider.kind.asc())
            .limit(1)
        )
    ).first()
    return row[0] if row else None


async def ingest_document(session: AsyncSession, document: KnowledgeDocument) -> None:
    """Extract, chunk and (when a key exists) embed one document.

    Missing embeddings are not fatal: retrieval falls back to keyword match
    so a seeded TXT still answers questions before AI Setup has a key.
    """
    document.status = DocumentStatus.PROCESSING
    document.ingest_error = None
    await session.flush()

    try:
        payload = await _load_bytes(document)
        text = extract_text(payload, document.source_type)
        chunks = split_chunks(text)
        if not chunks:
            raise IngestError("the document produced no text to index")

        base = await session.get(KnowledgeBase, document.knowledge_base_id)
        if base is None:
            raise IngestError("the knowledge base no longer exists")

        api_key, base_url, model = await resolve_embedding_auth(
            session, tenant_id=document.tenant_id, knowledge_base=base
        )
        vectors: list[list[float]] | None = None
        if api_key:
            vectors = await embed_texts(chunks, api_key=api_key, base_url=base_url, model=model)
            if base.embedding_model_slug is None:
                base.embedding_model_slug = model
            if vectors and base.embedding_dimensions != len(vectors[0]):
                base.embedding_dimensions = len(vectors[0])

        await session.execute(
            delete(KnowledgeChunk).where(KnowledgeChunk.knowledge_document_id == document.id)
        )
        for index, content in enumerate(chunks):
            session.add(
                KnowledgeChunk(
                    tenant_id=document.tenant_id,
                    knowledge_document_id=document.id,
                    knowledge_base_id=document.knowledge_base_id,
                    chunk_index=index,
                    content=content,
                    doc_metadata={"title": document.title, "source_type": document.source_type.value},
                    embedding=vectors[index] if vectors else None,
                )
            )

        document.chunk_count = len(chunks)
        document.content_sha256 = hashlib.sha256(payload).hexdigest()
        document.byte_size = len(payload)
        document.status = DocumentStatus.INDEXED
        document.indexed_at = datetime.now(UTC)
        document.ingest_error = None if vectors else "indexed without embeddings — keyword retrieval only"
        await session.flush()
    except Exception as exc:
        document.status = DocumentStatus.FAILED
        document.ingest_error = str(exc)[:2000]
        await session.flush()
        logger.exception("knowledge_ingest_failed", extra={"document_id": str(document.id)})
        if isinstance(exc, IngestError):
            raise
        raise IngestError(str(exc)) from exc


async def _load_bytes(document: KnowledgeDocument) -> bytes:
    if document.source_type is KnowledgeSourceType.WEB:
        if not document.source_url:
            raise IngestError("a web document needs a URL")
        import httpx

        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(document.source_url)
            response.raise_for_status()
            return response.content
    if not document.source_object_key:
        raise IngestError("the uploaded file is missing from object storage")
    return await object_store.get_object(document.source_object_key)


def document_object_key(
    tenant_id: uuid.UUID, kb_id: uuid.UUID, document_id: uuid.UUID, filename: str
) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ".-_" else "_" for ch in filename)[:180]
    return f"knowledge/{tenant_id}/{kb_id}/{document_id}/{safe}"


def vector_literal(values: list[float]) -> str:
    return format_vector(values)
