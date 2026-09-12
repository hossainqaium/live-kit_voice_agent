"""HTTP tool and knowledge base schemas (spec 30, 31, 32, 33)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import TimestampedResponse
from shared.models import (
    DocumentStatus,
    HttpMethod,
    KnowledgeSourceType,
    ResourceStatus,
    ToolAuthType,
)


class ToolBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The function name exposed to the model. Must be a valid identifier:
    #: providers reject names with spaces or punctuation.
    name: str = Field(min_length=1, max_length=64)

    #: Shown to the model, so this text decides whether the tool gets called at
    #: the right moment. It is prompt surface, not documentation.
    description: str = Field(min_length=1, max_length=2000)

    http_method: HttpMethod = HttpMethod.GET
    url_template: str = Field(min_length=1, max_length=2048)
    headers: dict = Field(default_factory=dict)
    request_schema: dict = Field(default_factory=dict)
    response_schema: dict = Field(default_factory=dict)

    auth_type: ToolAuthType = ToolAuthType.NONE
    auth_header_name: str | None = Field(default=None, max_length=128)

    #: Bounded because a tool call happens inside a live conversation, and
    #: anything above a few seconds is dead air.
    timeout_seconds: int = Field(default=5, ge=1, le=30)
    max_retries: int = Field(default=1, ge=0, le=5)

    @field_validator("name")
    @classmethod
    def _identifier(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned.replace("_", "").isalnum():
            raise ValueError("use letters, digits and underscores only — providers reject the rest")
        if cleaned[0].isdigit():
            raise ValueError("a tool name cannot start with a digit")
        return cleaned


class ToolCreate(ToolBase):
    #: Write-only. Stored encrypted and never returned (spec 54).
    auth_secret: str | None = Field(default=None, min_length=1, max_length=2048)


class ToolUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, min_length=1, max_length=2000)
    http_method: HttpMethod | None = None
    url_template: str | None = Field(default=None, min_length=1, max_length=2048)
    headers: dict | None = None
    request_schema: dict | None = None
    response_schema: dict | None = None
    auth_type: ToolAuthType | None = None
    auth_header_name: str | None = Field(default=None, max_length=128)
    auth_secret: str | None = Field(default=None, min_length=1, max_length=2048)
    timeout_seconds: int | None = Field(default=None, ge=1, le=30)
    max_retries: int | None = Field(default=None, ge=0, le=5)
    status: ResourceStatus | None = None


class ToolResponse(TimestampedResponse):
    name: str
    description: str
    http_method: HttpMethod
    url_template: str
    headers: dict = Field(default_factory=dict)
    request_schema: dict = Field(default_factory=dict)
    response_schema: dict = Field(default_factory=dict)
    auth_type: ToolAuthType
    auth_header_name: str | None
    timeout_seconds: int
    max_retries: int
    status: ResourceStatus

    #: Validated before an agent using it can be published (spec 31, 63).
    schema_valid: bool
    schema_validation_error: str | None

    has_secret: bool = False

    #: Variables the URL and headers reference, so the UI can show what the
    #: model must supply.
    variables: list[str] = Field(default_factory=list)

    #: Platform handler (``builtin://…``) rather than a tenant HTTP endpoint.
    is_builtin: bool = False


class KnowledgeBaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    embedding_model_slug: str | None = Field(default=None, max_length=128)
    embedding_dimensions: int = Field(default=1536, ge=1, le=8192)

    #: Capped so a large k cannot blow the latency budget (spec 56).
    top_k: int = Field(default=4, ge=1, le=20)


class KnowledgeBaseUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    top_k: int | None = Field(default=None, ge=1, le=20)
    status: ResourceStatus | None = None


class KnowledgeBaseResponse(TimestampedResponse):
    name: str
    description: str | None
    embedding_provider_id: uuid.UUID | None
    embedding_model_slug: str | None
    embedding_dimensions: int
    top_k: int
    status: ResourceStatus

    document_count: int = 0
    indexed_count: int = 0
    chunk_count: int = 0


class KnowledgeDocumentResponse(TimestampedResponse):
    knowledge_base_id: uuid.UUID
    title: str
    source_type: KnowledgeSourceType
    source_url: str | None
    status: DocumentStatus
    ingest_error: str | None
    indexed_at: datetime | None
    chunk_count: int
    byte_size: int | None
