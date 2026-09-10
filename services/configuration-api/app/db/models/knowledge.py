"""Knowledge bases and RAG documents (spec 33).

Retrieval runs on PostgreSQL + pgvector, so chunk embeddings live in the same
database as the configuration. Recordings and source files do not — those go to
object storage (spec 3).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    Base,
    TenantOwnedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    enum_column,
    json_column,
)
from shared.models import DocumentStatus, KnowledgeSourceType, ResourceStatus


class KnowledgeBase(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin):
    """A tenant's document collection, assignable to agents (spec 33)."""

    __tablename__ = "knowledge_bases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_knowledge_bases_tenant_name"),
        CheckConstraint("embedding_dimensions > 0", name="embedding_dimensions_positive"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    #: Which embedding model produced the vectors in this base. Recorded
    #: because embeddings from different models are not comparable — mixing
    #: them silently degrades retrieval rather than failing.
    embedding_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("providers.id", ondelete="RESTRICT")
    )
    embedding_model_slug: Mapped[str | None] = mapped_column(String(128))

    #: Vector width, fixed per base for the same reason.
    embedding_dimensions: Mapped[int] = mapped_column(Integer, nullable=False, default=1536)

    #: Retrieval tuning, capped so a large k cannot blow the latency budget
    #: (spec 56).
    top_k: Mapped[int] = mapped_column(Integer, nullable=False, default=4)

    status: Mapped[ResourceStatus] = enum_column(
        ResourceStatus, nullable=False, default=ResourceStatus.ACTIVE, index=True
    )

    def __repr__(self) -> str:
        return f"<KnowledgeBase {self.name}>"


class KnowledgeDocument(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin):
    """A source document ingested into a knowledge base (spec 33)."""

    __tablename__ = "knowledge_documents"

    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    source_type: Mapped[KnowledgeSourceType] = enum_column(
        KnowledgeSourceType, nullable=False, index=True
    )

    #: Original file in object storage, or the crawled URL for WEB sources.
    #: The file itself never goes in PostgreSQL (spec 3).
    source_object_key: Mapped[str | None] = mapped_column(String(512))
    source_url: Mapped[str | None] = mapped_column(String(2048))

    #: Content hash, so re-uploading an unchanged document skips re-embedding
    #: instead of paying for it again.
    content_sha256: Mapped[str | None] = mapped_column(String(64), index=True)

    status: Mapped[DocumentStatus] = enum_column(
        DocumentStatus, nullable=False, default=DocumentStatus.PENDING, index=True
    )
    ingest_error: Mapped[str | None] = mapped_column(Text)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    byte_size: Mapped[int | None] = mapped_column(Integer)

    def __repr__(self) -> str:
        return f"<KnowledgeDocument {self.title}>"


class KnowledgeChunk(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin):
    """An embedded passage retrieved at call time (spec 33).

    Carries ``tenant_id`` even though it is reachable through the document,
    because retrieval queries this table directly and the isolation filter must
    be applied at the point of the query, not inferred from a join (spec 7).
    """

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint(
            "knowledge_document_id", "chunk_index", name="uq_knowledge_chunks_document_index"
        ),
    )

    knowledge_document_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    #: Page number, section heading, source offsets — whatever lets the agent
    #: cite where an answer came from.
    doc_metadata: Mapped[dict] = json_column(nullable=False, default=dict)

    #: The embedding vector.
    #:
    #: Declared without a fixed dimension on purpose. pgvector requires a fixed
    #: width to build an HNSW or IVFFlat index, and the width is a property of
    #: the embedding model, which is chosen per knowledge base. Pinning a width
    #: now would force every tenant onto one model.
    #:
    #: The consequence is honest rather than hidden: retrieval is an exact scan
    #: until Phase 6 selects the embedding model and adds a matching typed
    #: column and ANN index. That is fine at the corpus sizes spec 33 implies,
    #: and a fake index would be worse than none.
    embedding: Mapped[object | None] = mapped_column(Vector())

    def __repr__(self) -> str:
        return f"<KnowledgeChunk {self.chunk_index}>"
