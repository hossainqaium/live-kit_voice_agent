"""Call-time knowledge retrieval over PostgreSQL + pgvector (spec 33)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.knowledge import DEFAULT_EMBEDDING_MODEL, embed_texts, format_vector
from shared.logging import get_logger

logger = get_logger(__name__)

_VECTOR_SQL = text(
    """
    SELECT kc.content,
           kc.doc_metadata,
           1 - (kc.embedding <=> CAST(:query AS vector)) AS score
    FROM knowledge_chunks kc
    WHERE kc.tenant_id = :tenant_id
      AND kc.knowledge_base_id = :kb_id
      AND kc.embedding IS NOT NULL
    ORDER BY kc.embedding <=> CAST(:query AS vector)
    LIMIT :top_k
    """
)

_KEYWORD_SQL = text(
    """
    SELECT kc.content,
           kc.doc_metadata,
           0.0 AS score
    FROM knowledge_chunks kc
    WHERE kc.tenant_id = :tenant_id
      AND kc.knowledge_base_id = :kb_id
      AND kc.content ILIKE :needle
    ORDER BY length(kc.content) ASC
    LIMIT :top_k
    """
)


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    content: str
    title: str | None
    score: float


@dataclass(frozen=True, slots=True)
class KnowledgeRetrieval:
    knowledge_base_id: uuid.UUID
    top_k: int
    embedding_model: str
    api_key: str | None
    base_url: str | None


def format_context(chunks: list[RetrievedChunk]) -> str:
    """Prompt block the model must treat as the only source of extra facts."""
    if not chunks:
        return ""
    lines = [
        "Relevant knowledge from this tenant's documents. "
        "Use only these passages. If they do not answer the caller, say you do not know."
    ]
    for index, chunk in enumerate(chunks, start=1):
        label = chunk.title or "document"
        lines.append(f"[{index}] ({label}) {chunk.content}")
    return "\n".join(lines)


def _row_to_chunk(row: object) -> RetrievedChunk:
    metadata = getattr(row, "doc_metadata", None) or {}
    title = metadata.get("title") if isinstance(metadata, dict) else None
    return RetrievedChunk(
        content=str(row.content),
        title=str(title) if title else None,
        score=float(getattr(row, "score", 0.0) or 0.0),
    )


async def search_chunks(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    knowledge_base_id: uuid.UUID,
    query: str,
    top_k: int,
    api_key: str | None,
    base_url: str | None,
    model: str = DEFAULT_EMBEDDING_MODEL,
) -> list[RetrievedChunk]:
    """Vector search when a key exists; otherwise keyword ILIKE on the query."""
    cleaned = query.strip()
    if not cleaned or top_k < 1:
        return []

    if api_key:
        try:
            vectors = await embed_texts(
                [cleaned], api_key=api_key, base_url=base_url, model=model
            )
            rows = (
                await session.execute(
                    _VECTOR_SQL,
                    {
                        "tenant_id": tenant_id,
                        "kb_id": knowledge_base_id,
                        "query": format_vector(vectors[0]),
                        "top_k": top_k,
                    },
                )
            ).fetchall()
            if rows:
                return [_row_to_chunk(row) for row in rows]
        except Exception:
            logger.exception("knowledge_vector_search_failed")

    terms = [part for part in cleaned.split() if len(part) > 2][:6]
    needle = "%" + "%".join(terms or [cleaned[:80]]) + "%"
    rows = (
        await session.execute(
            _KEYWORD_SQL,
            {
                "tenant_id": tenant_id,
                "kb_id": knowledge_base_id,
                "needle": needle,
                "top_k": top_k,
            },
        )
    ).fetchall()
    return [_row_to_chunk(row) for row in rows]
