"""Tenant-facing AI catalog and provider credentials (spec 24, 25, 26, 62).

Two things a tenant administrator needs before an agent can speak, and neither
had an API before this module: what may I select, and here is my key for it.

Both were CLI-only (``set-agent-provider``, ``set-credential``), which is what
kept spec 77 — configure everything through the UI — out of reach.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select

from app.core.dependencies import ClientIp, CurrentTenant, require_permission
from app.db.models import Model, Provider, ProviderCredential, Voice
from app.db.repository import TenantRepository
from app.schemas.catalog import (
    CatalogModel,
    CatalogProvider,
    CatalogResponse,
    CatalogVoice,
    CredentialResponse,
    CredentialUpsert,
    CredentialVerifyResponse,
)
from app.services import audit
from shared.crypto import CredentialCipher, CredentialEncryptionError
from shared.logging import get_logger
from shared.models import Permission, ResourceStatus

logger = get_logger(__name__)

router = APIRouter(tags=["catalog"])

#: How long a verification probe may take. Long enough for a cold self-hosted
#: endpoint to answer, short enough that an operator does not conclude the
#: console has hung.
_PROBE_TIMEOUT_SECONDS = 10.0

#: Hostname fragments that mean "somebody else's cloud". Used only to label a
#: provider in the catalog, never to allow or block one: a wrong guess here
#: mislabels a row, it does not change what a call does.
_PUBLIC_API_HOSTS = ("api.openai.com", "api.elevenlabs.io", "api.deepgram.com", "api.anthropic.com")


def _is_self_hosted(default_base_url: str | None) -> bool:
    """Whether a provider's endpoint is on the platform's own network.

    A provider with no default URL is not self-hosted: it is a vendor SDK
    talking to the vendor. One with a URL that is not a known public API is
    something an operator stood up, which is the distinction the local
    fallback tier depends on (spec 25, 55).
    """
    if not default_base_url:
        return False
    return not any(host in default_base_url for host in _PUBLIC_API_HOSTS)


@router.get(
    "/catalog",
    response_model=CatalogResponse,
    summary="The AI providers, models and voices this tenant may select",
    dependencies=[Depends(require_permission(Permission.AGENTS_READ))],
)
async def get_catalog(tenant: CurrentTenant) -> CatalogResponse:
    """Everything the agent builder needs to render its provider controls.

    **Only ``ACTIVE`` rows.** A disabled provider or a retired model would be
    rejected by pre-publish validation (spec 63), so offering it in a dropdown
    produces a choice that cannot be published and an error that reads like a
    fault. Filtering here is what keeps the builder's options and the
    validator's rules the same set.

    A version that already references a row since retired keeps it — the
    version is an immutable snapshot and nothing here rewrites one. The label
    still resolves, so the builder shows what is configured even when it is no
    longer selectable.
    """
    providers = list(
        (
            await tenant.session.execute(
                select(Provider)
                .where(Provider.status == ResourceStatus.ACTIVE)
                .order_by(Provider.kind, Provider.display_name)
            )
        )
        .scalars()
        .all()
    )
    provider_ids = [p.id for p in providers]
    kinds = {p.id: p.kind for p in providers}

    models = (
        list(
            (
                await tenant.session.execute(
                    select(Model)
                    .where(
                        Model.status == ResourceStatus.ACTIVE,
                        Model.provider_id.in_(provider_ids),
                    )
                    .order_by(Model.display_name)
                )
            )
            .scalars()
            .all()
        )
        if provider_ids
        else []
    )

    voices = (
        list(
            (
                await tenant.session.execute(
                    select(Voice)
                    .where(
                        Voice.status == ResourceStatus.ACTIVE,
                        Voice.provider_id.in_(provider_ids),
                    )
                    .order_by(Voice.name)
                )
            )
            .scalars()
            .all()
        )
        if provider_ids
        else []
    )

    # Which providers this tenant already holds a key for. Scoped through the
    # repository because credentials are tenant-owned — the one table in this
    # response that is not platform data.
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    credentialed = set(
        (
            await tenant.session.execute(
                repository.scoped(ProviderCredential).with_only_columns(
                    ProviderCredential.provider_id
                )
            )
        ).scalars()
    )

    return CatalogResponse(
        providers=[
            CatalogProvider(
                id=p.id,
                kind=p.kind,
                slug=p.slug,
                display_name=p.display_name,
                supports_streaming=p.supports_streaming,
                requires_credential=p.requires_credential,
                credential_set=p.id in credentialed,
                self_hosted=_is_self_hosted(p.default_base_url),
            )
            for p in providers
        ],
        models=[
            CatalogModel(
                id=m.id,
                provider_id=m.provider_id,
                provider_kind=kinds[m.provider_id],
                slug=m.slug,
                display_name=m.display_name,
                languages=list(m.languages or []),
                is_default=m.is_default,
            )
            for m in models
        ],
        voices=[
            CatalogVoice(
                id=v.id,
                provider_id=v.provider_id,
                name=v.name,
                language=v.language,
                accent=v.accent,
                description=v.description,
                is_default=v.is_default,
            )
            for v in voices
        ],
    )


# --------------------------------------------------------------------------- #
# Credentials (spec 26, 53, 54)
# --------------------------------------------------------------------------- #


async def _credential_response(
    tenant: CurrentTenant, row: ProviderCredential
) -> CredentialResponse:
    provider = (
        await tenant.session.execute(select(Provider).where(Provider.id == row.provider_id))
    ).scalar_one_or_none()
    payload = CredentialResponse.model_validate(row, from_attributes=True)
    if provider is None:
        return payload
    return payload.model_copy(
        update={
            "provider_slug": provider.slug,
            "provider_kind": provider.kind,
            "provider_display_name": provider.display_name,
        }
    )


@router.get(
    "/catalog/credentials",
    response_model=list[CredentialResponse],
    summary="Provider keys this tenant has stored",
    dependencies=[Depends(require_permission(Permission.AGENTS_READ))],
)
async def list_credentials(tenant: CurrentTenant) -> list[CredentialResponse]:
    """What is stored, never what was stored.

    The response carries a four-character hint and nothing more. Reading a key
    back is not a feature that was left out; there is no code path that returns
    one (spec 26).
    """
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    rows = (
        (await tenant.session.execute(repository.scoped(ProviderCredential))).scalars().all()
    )
    return [await _credential_response(tenant, row) for row in rows]


@router.put(
    "/catalog/credentials",
    response_model=CredentialResponse,
    summary="Store or rotate a provider key",
    dependencies=[Depends(require_permission(Permission.AGENTS_WRITE))],
)
async def upsert_credential(
    payload: CredentialUpsert,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> CredentialResponse:
    """Encrypt and store one provider key.

    ``PUT`` rather than ``POST`` because there is one key per provider and
    label: storing a key for a provider that already has one is a rotation, not
    a second credential, and making the caller discover an existing row first
    would invite a duplicate.

    The plaintext never reaches the audit trail, a log line, or any response.
    What is recorded is that a key was set, by whom, and its last four
    characters — enough to answer "did someone change this" without being
    enough to use.
    """
    provider = (
        await tenant.session.execute(select(Provider).where(Provider.id == payload.provider_id))
    ).scalar_one_or_none()
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no such provider in the platform catalog",
        )
    if provider.status is not ResourceStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="that provider is not active, so a key for it could not be used",
        )

    try:
        cipher = CredentialCipher.from_env()
    except CredentialEncryptionError as exc:
        # A configuration fault on the platform, not a client error. Returning
        # 400 here would send an operator looking at their own key.
        logger.error("credential_cipher_unavailable", extra={"error": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="credential encryption is not configured on this deployment",
        ) from exc

    api_key = payload.api_key.strip()
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="the key is blank"
        )

    encrypted = cipher.encrypt(api_key)
    hint = CredentialCipher.hint(api_key)

    repository = TenantRepository(tenant.session, tenant.tenant_id)
    existing = (
        await tenant.session.execute(
            repository.scoped(ProviderCredential).where(
                ProviderCredential.provider_id == provider.id,
                ProviderCredential.label == payload.label,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        row = ProviderCredential(
            tenant_id=tenant.tenant_id,
            provider_id=provider.id,
            label=payload.label,
            api_key_ciphertext=encrypted.ciphertext,
            encryption_key_version=encrypted.key_version,
            key_hint=hint,
            base_url=payload.base_url,
            status=ResourceStatus.ACTIVE,
        )
        tenant.session.add(row)
        await tenant.session.flush()
        action = "provider_credential.created"
        before = None
    else:
        row = existing
        before = audit.snapshot(row, "key_hint", "base_url", "label", "status")
        row.api_key_ciphertext = encrypted.ciphertext
        row.encryption_key_version = encrypted.key_version
        row.key_hint = hint
        row.base_url = payload.base_url
        row.rotated_at = datetime.now(UTC)
        # A rotated key has not been checked against the provider yet, and
        # leaving the old timestamp would show a green tick for a key nobody
        # has tried.
        row.last_verified_at = None
        action = "provider_credential.rotated"

    await audit.record(
        tenant.session,
        tenant_id=tenant.tenant_id,
        principal=tenant.principal,
        action=action,
        resource_type="provider_credential",
        resource_id=row.id,
        old_value=before,
        new_value=audit.snapshot(row, "key_hint", "base_url", "label", "status"),
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    await tenant.session.refresh(row)
    return await _credential_response(tenant, row)


@router.post(
    "/catalog/credentials/{credential_id}/verify",
    response_model=CredentialVerifyResponse,
    summary="Check a stored key against the provider",
    dependencies=[Depends(require_permission(Permission.AGENTS_WRITE))],
)
async def verify_credential(
    credential_id: uuid.UUID,
    tenant: CurrentTenant,
) -> CredentialVerifyResponse:
    """Make one real request to the provider with the stored key.

    The probe is ``GET {base_url}/models``, which every OpenAI-compatible
    endpoint answers — including the self-hosted ones (spec 25). It proves
    three things together: the endpoint is reachable from this deployment, TLS
    and DNS work, and the key is accepted.

    **It does not prove a particular model is available.** That is a different
    request with a different failure mode, and reporting it as one result would
    put a green tick beside a model the key cannot reach. The response says
    exactly what was checked, including the URL.

    A failure is not an HTTP error here: the endpoint succeeded in finding out
    that the credential does not work, and an operator needs the detail, not a
    stack of 502s.
    """
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(ProviderCredential, credential_id)
    if row is None:
        # 404, not 403 — a credential in another tenant must be indistinguishable
        # from one that does not exist (spec 7).
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no such credential"
        )

    provider = (
        await tenant.session.execute(select(Provider).where(Provider.id == row.provider_id))
    ).scalar_one_or_none()

    base_url = (row.base_url or (provider.default_base_url if provider else None) or "").rstrip("/")
    if not base_url:
        return CredentialVerifyResponse(
            ok=False,
            detail=(
                "no endpoint to check: this credential has no base URL and the "
                "provider has no default"
            ),
            checked_url="",
        )

    url = f"{base_url}/models"
    try:
        cipher = CredentialCipher.from_env()
        api_key = cipher.decrypt(row.api_key_ciphertext, row.encryption_key_version)
    except CredentialEncryptionError as exc:
        logger.error("credential_decrypt_failed", extra={"error": str(exc)})
        return CredentialVerifyResponse(
            ok=False,
            detail="the stored key could not be decrypted with this deployment's keys",
            checked_url=url,
        )

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        # The exception text can contain the URL but never the header, so this
        # is safe to return. The key itself is not in scope for the message.
        return CredentialVerifyResponse(
            ok=False,
            detail=f"could not reach the provider: {type(exc).__name__}",
            checked_url=url,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    ok = response.status_code < 400

    if ok:
        row.last_verified_at = datetime.now(UTC)
        await tenant.session.commit()
        detail = "the provider accepted this key"
    elif response.status_code in (401, 403):
        detail = "the provider rejected this key"
    else:
        detail = f"the provider answered {response.status_code}"

    return CredentialVerifyResponse(
        ok=ok,
        status_code=response.status_code,
        detail=detail,
        checked_url=url,
        latency_ms=latency_ms,
        verified_at=row.last_verified_at if ok else None,
    )


@router.delete(
    "/catalog/credentials/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a stored provider key",
    dependencies=[Depends(require_permission(Permission.AGENTS_WRITE))],
)
async def delete_credential(
    credential_id: uuid.UUID,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> Response:
    """Delete a key.

    Deliberately does **not** check whether an agent version references the
    provider. Unlike a catalog row, a credential is the tenant's own secret,
    and refusing to remove it because something still uses it would mean a
    leaked key cannot be withdrawn without first editing every agent. Removing
    it stops future calls on that provider, which is the intended effect.
    """
    repository = TenantRepository(tenant.session, tenant.tenant_id)
    row = await repository.get(ProviderCredential, credential_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no such credential"
        )

    before = audit.snapshot(row, "key_hint", "base_url", "label", "status")
    await tenant.session.delete(row)
    await audit.record(
        tenant.session,
        tenant_id=tenant.tenant_id,
        principal=tenant.principal,
        action="provider_credential.deleted",
        resource_type="provider_credential",
        resource_id=credential_id,
        old_value=before,
        new_value=None,
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
