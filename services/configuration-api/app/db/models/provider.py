"""AI provider catalog, models, voices and credentials.

Spec 25 (supported providers), 26 (ElevenLabs), 27 (voice library).

Providers, models and voices are a **platform** catalog managed by
SUPER_ADMIN (spec 60), so they carry no ``tenant_id``. Credentials are
different: spec 7 forbids one tenant seeing another's provider credentials, so
``provider_credentials`` is strictly tenant-owned.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
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
from shared.models import ProviderKind, ResourceStatus


class Provider(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A supported AI provider, e.g. Deepgram or ElevenLabs (spec 25).

    Platform-level. Adding a provider row does not enable it for anyone — a
    tenant still needs its own credential.
    """

    __tablename__ = "providers"
    __table_args__ = (UniqueConstraint("kind", "slug", name="uq_providers_kind_slug"),)

    kind: Mapped[ProviderKind] = enum_column(ProviderKind, nullable=False, index=True)

    #: Adapter key, e.g. "deepgram". This is what selects the code path in the
    #: worker's provider registry, so it is a contract value, not a label.
    slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    display_name: Mapped[str] = mapped_column(String(128), nullable=False)

    #: Whether this adapter streams (spec 28 requires streaming where
    #: supported). Surfaced in the UI so an operator can see the latency
    #: consequence of a choice.
    supports_streaming: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    status: Mapped[ResourceStatus] = enum_column(
        ResourceStatus, nullable=False, default=ResourceStatus.ACTIVE, index=True
    )

    #: Optional endpoint override, for self-hosted or regional deployments
    #: (spec 25 requires local/self-hosted LLM support).
    default_base_url: Mapped[str | None] = mapped_column(String(512))

    #: Whether this provider needs an API credential.
    #:
    #: False for a self-hosted endpoint — ollama, vLLM, speaches — which
    #: authenticates by network reachability rather than by key. Pre-publish
    #: validation (spec 63) must not demand a credential for these, or a valid
    #: self-hosted configuration becomes unpublishable and the local-model
    #: support spec 25 requires is unusable in practice.
    requires_credential: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    notes: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<Provider {self.kind}:{self.slug}>"


class Model(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A model offered by a provider, e.g. ``nova-2`` or ``gpt-4o``."""

    __tablename__ = "models"
    __table_args__ = (UniqueConstraint("provider_id", "slug", name="uq_models_provider_slug"),)

    provider_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("providers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)

    #: BCP-47 tags this model supports. Empty means unrestricted.
    languages: Mapped[list[str]] = json_column(nullable=False, default=list)

    status: Mapped[ResourceStatus] = enum_column(
        ResourceStatus, nullable=False, default=ResourceStatus.ACTIVE, index=True
    )
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def __repr__(self) -> str:
        return f"<Model {self.slug}>"


class Voice(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A TTS voice (spec 27).

    Platform-level, because the Voice Library lives in the platform console
    (spec 60) and the same vendor voice is shared across tenants.
    """

    __tablename__ = "voices"
    __table_args__ = (
        UniqueConstraint("provider_id", "voice_id", name="uq_voices_provider_voice_id"),
    )

    provider_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("providers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: The provider's own voice identifier, passed straight to the adapter.
    voice_id: Mapped[str] = mapped_column(String(128), nullable=False)

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    language: Mapped[str | None] = mapped_column(String(16), index=True)
    accent: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)

    status: Mapped[ResourceStatus] = enum_column(
        ResourceStatus, nullable=False, default=ResourceStatus.ACTIVE, index=True
    )

    #: Voice used when an agent selects none. Spec 27 requires a Set Default
    #: operation.
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: Sample stored in object storage, never in the database (spec 3).
    sample_object_key: Mapped[str | None] = mapped_column(String(512))

    def __repr__(self) -> str:
        return f"<Voice {self.name}>"


class ProviderCredential(Base, UUIDPrimaryKeyMixin, TenantOwnedMixin, TimestampMixin):
    """A tenant's API key for a provider, encrypted at rest (spec 53, 54).

    Strictly tenant-owned. A nullable ``tenant_id`` for platform-wide shared
    keys was considered and rejected: every authorization query would then need
    ``tenant_id = :t OR tenant_id IS NULL``, and one forgotten clause is a
    cross-tenant credential leak. If shared keys are ever needed they belong in
    a separate table with its own explicit access path.
    """

    __tablename__ = "provider_credentials"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "provider_id",
            "label",
            name="uq_provider_credentials_tenant_provider_label",
        ),
    )

    provider_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("providers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: Distinguishes multiple keys for one provider, e.g. "primary" and
    #: "failover" (spec 55 requires a fallback path).
    label: Mapped[str] = mapped_column(String(64), nullable=False, default="primary")

    #: Ciphertext only. Decrypted at call start, held in memory for the call,
    #: never written to a log or returned by the API (spec 54).
    api_key_ciphertext: Mapped[bytes] = mapped_column(postgresql.BYTEA, nullable=False)
    encryption_key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    #: Last four characters, so the UI can show which key is configured
    #: without ever decrypting it.
    key_hint: Mapped[str | None] = mapped_column(String(8))

    base_url: Mapped[str | None] = mapped_column(String(512))

    status: Mapped[ResourceStatus] = enum_column(
        ResourceStatus, nullable=False, default=ResourceStatus.ACTIVE, index=True
    )

    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<ProviderCredential {self.label} ***{self.key_hint or ''}>"
