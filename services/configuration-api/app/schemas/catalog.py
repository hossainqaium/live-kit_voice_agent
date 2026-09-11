"""Tenant-facing catalog and provider credentials (spec 24, 25, 26, 62).

The platform owns the catalog; a tenant only reads it, and only to answer one
question: what may I point an agent at? So these responses are deliberately
narrower than their ``/platform`` equivalents. ``credential_count`` tells an
operator a provider is in use across tenants and is none of a tenant's
business; ``default_base_url`` is the address of a self-hosted endpoint on the
platform's own network, which a tenant has no reason to learn from a dropdown.

Nothing here ever carries an API key outward. The key is write-only by
construction: it appears on the request model and on no response (spec 26).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import TimestampedResponse
from shared.models import ProviderKind, ResourceStatus


class CatalogProvider(BaseModel):
    """A provider a tenant may select."""

    id: uuid.UUID
    kind: ProviderKind
    slug: str
    display_name: str
    supports_streaming: bool

    #: False for a self-hosted endpoint reached over the platform's network.
    #: The builder reads this to decide whether to demand a key before letting
    #: a version be published, which is the same rule the validator applies.
    requires_credential: bool

    #: Whether this tenant has stored a key for it. Not a property of the
    #: catalog at all, but the builder's most important question about a
    #: provider, and answering it here avoids a second request per option.
    credential_set: bool = False

    #: Whether it runs on the platform's own network rather than a public API.
    #: Derived, and deliberately a flag rather than the URL: an operator needs
    #: to know which option survives the internet being down, and the address
    #: of an internal endpoint is platform infrastructure (spec 13).
    self_hosted: bool = False


class CatalogModel(BaseModel):
    id: uuid.UUID
    provider_id: uuid.UUID
    provider_kind: ProviderKind
    slug: str
    display_name: str
    languages: list[str]
    is_default: bool


class CatalogVoice(BaseModel):
    id: uuid.UUID
    provider_id: uuid.UUID
    name: str
    language: str | None
    accent: str | None
    description: str | None
    is_default: bool


class CatalogResponse(BaseModel):
    """Everything the agent builder needs to render its provider controls.

    One response rather than three endpoints: the builder cannot draw a single
    correct control until it holds all three lists, so three round trips would
    only add latency and a partial-failure state to reason about.
    """

    providers: list[CatalogProvider]
    models: list[CatalogModel]
    voices: list[CatalogVoice]


# --------------------------------------------------------------------------- #
# Credentials (spec 26, 53, 54)
# --------------------------------------------------------------------------- #


class CredentialUpsert(BaseModel):
    """Store or rotate the key for one provider."""

    model_config = ConfigDict(extra="forbid")

    provider_id: uuid.UUID

    #: Write-only. Present on this model and on no response model anywhere in
    #: the API, which is what makes "never returned" a property of the types
    #: rather than a rule each endpoint has to remember.
    api_key: str = Field(min_length=8, max_length=512)

    #: Endpoint override, for a self-hosted deployment or a regional endpoint.
    base_url: str | None = Field(default=None, max_length=512)

    #: ``primary`` is what the worker loads. Other labels are stored but not
    #: yet consulted at call time; the field exists so rotation has somewhere
    #: to stage a new key.
    label: str = Field(default="primary", max_length=64)


class CredentialResponse(TimestampedResponse):
    provider_id: uuid.UUID
    provider_slug: str | None = None
    provider_kind: ProviderKind | None = None
    provider_display_name: str | None = None
    label: str
    status: ResourceStatus

    #: The last four characters, so a human can tell two keys apart without the
    #: platform ever handing one back.
    key_hint: str | None
    base_url: str | None
    last_verified_at: datetime | None
    rotated_at: datetime | None


class CredentialVerifyResponse(BaseModel):
    """Outcome of a live probe against the provider.

    ``ok`` means the provider accepted the key on a real request. It does not
    mean a particular model is available to it — that is a different question,
    and conflating the two would let a green tick sit next to a model the key
    cannot actually reach.
    """

    ok: bool
    status_code: int | None = None
    detail: str
    checked_url: str
    latency_ms: int | None = None
    verified_at: datetime | None = None
