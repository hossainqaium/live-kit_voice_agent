"""Loads a call's configuration from the Control Plane (spec 23, 45).

This is what keeps the worker generic. At call start it resolves
tenant → PBX → trunk → DID → routing rule → agent → published version, reads
every setting that version specifies, decrypts the credentials it needs, and
returns one frozen object.

Two rules shape the design:

* **Load once.** Spec 45 forbids querying PostgreSQL for every audio or
  conversation event. Everything the call needs is read here and held in
  memory for its lifetime.
* **Freeze it.** A call must use one consistent agent configuration version
  throughout (spec 45), so publishing a new version mid-call cannot change
  behaviour. The returned context is immutable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.crypto import CredentialCipher, CredentialEncryptionError
from shared.logging import get_logger
from shared.models import ProviderKind
from worker.providers.base import ProviderConfig

logger = get_logger(__name__)


class ConfigurationError(RuntimeError):
    """The call cannot be served with the configuration on record."""


class NoRouteError(ConfigurationError):
    """Nothing in the configuration says who should answer this call."""


class NotPublishedError(ConfigurationError):
    """The agent exists but has no published version.

    A draft agent must not answer calls (spec 19), so this is a refusal rather
    than a fallback to whatever version happens to exist.
    """


class TenantLimitExceededError(ConfigurationError):
    """A tenant call limit would be breached by accepting this call.

    Checked before the call is accepted, as spec 47 requires.
    """

    def __init__(self, message: str, *, limit: str) -> None:
        super().__init__(message)
        self.limit = limit


@dataclass(frozen=True, slots=True)
class TransferPolicy:
    """Warm transfer settings for this call (spec 35, 36; CR-1)."""

    enabled: bool
    announcement_text: str | None
    hold_media_object_key: str | None
    summary_template: str | None
    summary_max_seconds: int
    skip_dtmf: str | None


@dataclass(frozen=True, slots=True)
class CallPolicy:
    """Limits that end a call regardless of what the model wants."""

    silence_timeout_seconds: int | None
    max_call_duration_seconds: int | None
    interruption_enabled: bool
    interruption_min_words: int
    recording_enabled: bool
    transcription_enabled: bool


@dataclass(frozen=True, slots=True)
class CallContext:
    """Everything one call needs, resolved once and never reloaded.

    Frozen on purpose: a mutable context would let a later database read change
    behaviour halfway through a conversation, which is exactly what spec 45
    forbids.
    """

    # --- Correlation (spec 43) -------------------------------------------- #
    call_id: str
    call_row_id: uuid.UUID

    # --- Identity --------------------------------------------------------- #
    tenant_id: uuid.UUID
    tenant_slug: str
    agent_id: uuid.UUID
    agent_version_id: uuid.UUID
    agent_name: str
    version_number: int

    # --- Telephony -------------------------------------------------------- #
    room_name: str
    did: str | None
    caller_number: str | None
    sip_trunk_id: uuid.UUID | None
    pbx_id: uuid.UUID | None

    # --- Conversation ----------------------------------------------------- #
    language: str
    greeting: str | None
    system_prompt: str

    # --- Providers (spec 24) ---------------------------------------------- #
    stt: ProviderConfig
    llm: ProviderConfig
    tts: ProviderConfig

    # --- Policy ----------------------------------------------------------- #
    call_policy: CallPolicy
    transfer_policy: TransferPolicy

    #: Resolved tool definitions. Empty in Phase 1; populated in Phase 6.
    tools: tuple[dict[str, Any], ...] = ()

    knowledge_base_id: uuid.UUID | None = None

    #: Further tiers to try when the primary fails, in order (spec 55). Empty
    #: for a version that configures only a primary, which is why these are
    #: separate fields rather than a chain replacing ``stt``/``llm``/``tts``:
    #: every existing caller keeps reading the primary without knowing that
    #: tiers exist.
    #:
    #: Declared here rather than beside the primaries because this is a frozen
    #: dataclass and a defaulted field cannot precede a required one — placing
    #: them next to their own stage made the class impossible to construct.
    stt_fallbacks: tuple[ProviderConfig, ...] = ()
    llm_fallbacks: tuple[ProviderConfig, ...] = ()
    tts_fallbacks: tuple[ProviderConfig, ...] = ()

    #: Correlation fields for every log line this call produces (spec 58).
    def log_fields(self) -> dict[str, str]:
        return {
            "tenant_id": str(self.tenant_id),
            "call_id": self.call_id,
            "room_id": self.room_name,
            "agent_id": str(self.agent_id),
            "agent_version_id": str(self.agent_version_id),
        }

    def redacted(self) -> dict[str, Any]:
        """Log-safe description. Never includes a credential (spec 54)."""
        return {
            **self.log_fields(),
            "tenant_slug": self.tenant_slug,
            "agent_name": self.agent_name,
            "version_number": self.version_number,
            "did": self.did,
            "language": self.language,
            "stt": self.stt.redacted(),
            "llm": self.llm.redacted(),
            "tts": self.tts.redacted(),
        }


@dataclass(frozen=True, slots=True)
class SipCallInfo:
    """What LiveKit tells us about an inbound SIP call.

    Extracted from participant attributes rather than guessed from the room
    name, so a renamed room cannot silently change routing.
    """

    livekit_trunk_id: str | None
    called_number: str | None
    caller_number: str | None
    livekit_call_id: str | None

    @classmethod
    def from_attributes(cls, attributes: dict[str, str]) -> SipCallInfo:
        return cls(
            livekit_trunk_id=attributes.get("sip.trunkID"),
            # The number the trunk received, which is what routing matches on
            # (spec 20). Deliberately does NOT fall back to sip.phoneNumber:
            # that attribute holds the *caller's* number, so using it here
            # would silently route a call by the wrong number rather than
            # failing to route it.
            called_number=(
                attributes.get("sip.trunkPhoneNumber") or attributes.get("sip.calledNumber")
            ),
            # sip.phoneNumber is the caller's number in LiveKit's SIP
            # attributes; the others cover naming across versions.
            caller_number=(
                attributes.get("sip.phoneNumber")
                or attributes.get("sip.from")
                or attributes.get("sip.fromUser")
                or attributes.get("sip.callerNumber")
            ),
            livekit_call_id=attributes.get("sip.callID"),
        )


#: One statement rather than a chain of ORM round-trips. The whole point of
#: this module is to resolve configuration before the caller hears silence, and
#: seven sequential queries against a pooled connection is most of a second in
#: the worst case.
_RESOLVE_SQL = text(
    """
    SELECT
        t.id                AS tenant_id,
        t.slug              AS tenant_slug,
        t.max_concurrent_calls,
        t.max_daily_calls,
        t.max_monthly_minutes,
        a.id                AS agent_id,
        a.name              AS agent_name,
        a.published_version_id,
        pn.id               AS phone_number_id,
        pn.pbx_id           AS pbx_id,
        pn.sip_trunk_id     AS sip_trunk_id
    FROM phone_numbers pn
    JOIN tenants t   ON t.id = pn.tenant_id
    LEFT JOIN agents a ON a.id = pn.inbound_agent_id
    WHERE pn.number = :did
      AND pn.status = 'ACTIVE'
      AND t.status  = 'ACTIVE'
    LIMIT 1
    """
)

_VERSION_SQL = text(
    """
    SELECT
        av.id, av.version_number, av.language, av.greeting, av.system_prompt,
        av.temperature, av.interruption_enabled, av.interruption_min_words,
        av.silence_timeout_seconds, av.max_call_duration_seconds,
        av.recording_enabled, av.transcription_enabled,
        av.transfer_enabled, av.transfer_announcement_text,
        av.hold_media_object_key, av.transfer_summary_template,
        av.transfer_summary_max_seconds, av.transfer_skip_dtmf,
        av.knowledge_base_id,

        COALESCE(stt_p.adapter, stt_p.slug) AS stt_slug, stt_p.default_base_url AS stt_base_url,
        stt_m.slug AS stt_model,
        COALESCE(llm_p.adapter, llm_p.slug) AS llm_slug, llm_p.default_base_url AS llm_base_url,
        llm_m.slug AS llm_model,
        COALESCE(tts_p.adapter, tts_p.slug) AS tts_slug, tts_p.default_base_url AS tts_base_url,
        tts_m.slug AS tts_model,
        v.voice_id AS voice_external_id,

        stt_p.id AS stt_provider_id,
        llm_p.id AS llm_provider_id,
        tts_p.id AS tts_provider_id,

        -- Fallback tier (spec 55). Every join is LEFT: a version that names
        -- only a primary provider must resolve exactly as it did before.
        COALESCE(sttf_p.adapter, sttf_p.slug) AS sttf_slug, sttf_p.default_base_url AS sttf_base_url,
        sttf_m.slug AS sttf_model, sttf_p.id AS sttf_provider_id,
        COALESCE(llmf_p.adapter, llmf_p.slug) AS llmf_slug, llmf_p.default_base_url AS llmf_base_url,
        llmf_m.slug AS llmf_model, llmf_p.id AS llmf_provider_id,
        COALESCE(ttsf_p.adapter, ttsf_p.slug) AS ttsf_slug, ttsf_p.default_base_url AS ttsf_base_url,
        ttsf_m.slug AS ttsf_model, ttsf_p.id AS ttsf_provider_id,
        vf.voice_id AS ttsf_voice,

        -- Local last resort (spec 25, 55).
        COALESCE(sttl_p.adapter, sttl_p.slug) AS sttl_slug, sttl_p.default_base_url AS sttl_base_url,
        sttl_m.slug AS sttl_model, sttl_p.id AS sttl_provider_id,
        COALESCE(ttsl_p.adapter, ttsl_p.slug) AS ttsl_slug, ttsl_p.default_base_url AS ttsl_base_url,
        ttsl_m.slug AS ttsl_model, ttsl_p.id AS ttsl_provider_id,
        vl.voice_id AS ttsl_voice
    FROM agent_versions av
    LEFT JOIN providers stt_p ON stt_p.id = av.stt_provider_id
    LEFT JOIN models    stt_m ON stt_m.id = av.stt_model_id
    LEFT JOIN providers llm_p ON llm_p.id = av.llm_provider_id
    LEFT JOIN models    llm_m ON llm_m.id = av.llm_model_id
    LEFT JOIN providers tts_p ON tts_p.id = av.tts_provider_id
    LEFT JOIN models    tts_m ON tts_m.id = av.tts_model_id
    LEFT JOIN voices    v     ON v.id     = av.voice_id

    LEFT JOIN providers sttf_p ON sttf_p.id = av.stt_fallback_provider_id
    LEFT JOIN models    sttf_m ON sttf_m.id = av.stt_fallback_model_id
    LEFT JOIN providers llmf_p ON llmf_p.id = av.llm_fallback_provider_id
    LEFT JOIN models    llmf_m ON llmf_m.id = av.llm_fallback_model_id
    LEFT JOIN providers ttsf_p ON ttsf_p.id = av.tts_fallback_provider_id
    LEFT JOIN models    ttsf_m ON ttsf_m.id = av.tts_fallback_model_id
    LEFT JOIN voices    vf     ON vf.id     = av.tts_fallback_voice_id

    LEFT JOIN providers sttl_p ON sttl_p.id = av.stt_local_provider_id
    LEFT JOIN models    sttl_m ON sttl_m.id = av.stt_local_model_id
    LEFT JOIN providers ttsl_p ON ttsl_p.id = av.tts_local_provider_id
    LEFT JOIN models    ttsl_m ON ttsl_m.id = av.tts_local_model_id
    LEFT JOIN voices    vl     ON vl.id     = av.tts_local_voice_id
    WHERE av.id = :version_id
    """
)

_CREDENTIALS_SQL = text(
    """
    SELECT provider_id, api_key_ciphertext, encryption_key_version, base_url
    FROM provider_credentials
    WHERE tenant_id = :tenant_id
      AND provider_id = ANY(:provider_ids)
      AND status = 'ACTIVE'
      AND label = 'primary'
    """
)

_CONCURRENCY_SQL = text(
    """
    SELECT count(*) FROM calls
    WHERE tenant_id = :tenant_id
      AND state NOT IN ('COMPLETED','FAILED','TIMEOUT','CANCELLED','BUSY','NO_ANSWER')
    """
)


class CallConfigLoader:
    """Resolves a call's configuration and records the call."""

    def __init__(self, cipher: CredentialCipher | None = None) -> None:
        # Constructed lazily: a worker with no credentials configured can still
        # serve calls whose providers need none, which is what makes a keyless
        # development setup possible.
        self._cipher = cipher
        self._cipher_error: str | None = None
        if cipher is None:
            try:
                self._cipher = CredentialCipher.from_env()
            except CredentialEncryptionError as exc:
                self._cipher_error = str(exc)

    async def load(
        self,
        session: AsyncSession,
        *,
        call_id: str,
        room_name: str,
        sip: SipCallInfo,
        worker_id: str,
    ) -> CallContext:
        """Resolve everything this call needs, and create its ``calls`` row."""
        did = sip.called_number
        if not did:
            raise NoRouteError(
                "the inbound call carried no dialled number, so no DID could be matched"
            )

        row = (await session.execute(_RESOLVE_SQL, {"did": did})).mappings().first()
        if row is None:
            raise NoRouteError(f"no active DID matches {did!r} for any active tenant")

        if row["agent_id"] is None:
            raise NoRouteError(f"DID {did!r} has no inbound agent assigned")

        if row["published_version_id"] is None:
            raise NotPublishedError(
                f"agent {row['agent_name']!r} has no published version, so it cannot answer"
            )

        # Enforced before accepting the call, as spec 47 requires. Doing it
        # after would mean the caller has already been answered.
        await self._enforce_limits(session, row)

        version = (
            (await session.execute(_VERSION_SQL, {"version_id": row["published_version_id"]}))
            .mappings()
            .first()
        )
        if version is None:
            raise NotPublishedError(f"published version {row['published_version_id']} is missing")

        credentials = await self._load_credentials(session, row["tenant_id"], version)

        stt = self._provider_config(
            ProviderKind.STT, version, "stt", credentials, language=version["language"]
        )
        llm = self._provider_config(
            ProviderKind.LLM, version, "llm", credentials, temperature=version["temperature"]
        )
        tts = self._provider_config(
            ProviderKind.TTS,
            version,
            "tts",
            credentials,
            voice_id=version["voice_external_id"],
        )

        # Optional tiers. ``_optional_provider_config`` returns None when the
        # version does not name one, so the chains are exactly as long as what
        # was configured.
        stt_fallbacks = self._chain(
            (
                self._optional_provider_config(
                    ProviderKind.STT, version, "sttf", credentials, language=version["language"]
                ),
                self._optional_provider_config(
                    ProviderKind.STT, version, "sttl", credentials, language=version["language"]
                ),
            )
        )
        llm_fallbacks = self._chain(
            (
                self._optional_provider_config(
                    ProviderKind.LLM,
                    version,
                    "llmf",
                    credentials,
                    temperature=version["temperature"],
                ),
            )
        )
        tts_fallbacks = self._chain(
            (
                self._optional_provider_config(
                    ProviderKind.TTS, version, "ttsf", credentials, voice_id=version["ttsf_voice"]
                ),
                self._optional_provider_config(
                    ProviderKind.TTS, version, "ttsl", credentials, voice_id=version["ttsl_voice"]
                ),
            )
        )

        call_row_id = await self._create_call_row(
            session,
            call_id=call_id,
            room_name=room_name,
            sip=sip,
            row=row,
            version_id=version["id"],
            worker_id=worker_id,
        )

        context = CallContext(
            call_id=call_id,
            call_row_id=call_row_id,
            tenant_id=row["tenant_id"],
            tenant_slug=row["tenant_slug"],
            agent_id=row["agent_id"],
            agent_version_id=version["id"],
            agent_name=row["agent_name"],
            version_number=version["version_number"],
            room_name=room_name,
            did=did,
            caller_number=sip.caller_number,
            sip_trunk_id=row["sip_trunk_id"],
            pbx_id=row["pbx_id"],
            language=version["language"],
            greeting=version["greeting"],
            system_prompt=version["system_prompt"] or "",
            stt=stt,
            llm=llm,
            tts=tts,
            stt_fallbacks=stt_fallbacks,
            llm_fallbacks=llm_fallbacks,
            tts_fallbacks=tts_fallbacks,
            call_policy=CallPolicy(
                silence_timeout_seconds=version["silence_timeout_seconds"],
                max_call_duration_seconds=version["max_call_duration_seconds"],
                interruption_enabled=version["interruption_enabled"],
                interruption_min_words=version["interruption_min_words"],
                recording_enabled=version["recording_enabled"],
                transcription_enabled=version["transcription_enabled"],
            ),
            transfer_policy=TransferPolicy(
                enabled=version["transfer_enabled"],
                announcement_text=version["transfer_announcement_text"],
                hold_media_object_key=version["hold_media_object_key"],
                summary_template=version["transfer_summary_template"],
                summary_max_seconds=version["transfer_summary_max_seconds"],
                skip_dtmf=version["transfer_skip_dtmf"],
            ),
            knowledge_base_id=version["knowledge_base_id"],
        )

        logger.info("call_configuration_loaded", extra=context.redacted())
        return context

    # ------------------------------------------------------------------ #

    async def _enforce_limits(self, session: AsyncSession, row: Any) -> None:
        """Reject the call if a tenant limit is already reached (spec 47)."""
        max_concurrent = row["max_concurrent_calls"]
        if max_concurrent is None:
            return

        active = (
            await session.execute(_CONCURRENCY_SQL, {"tenant_id": row["tenant_id"]})
        ).scalar_one()

        if active >= max_concurrent:
            raise TenantLimitExceededError(
                f"tenant {row['tenant_slug']!r} already has {active} active call(s), "
                f"at its limit of {max_concurrent}",
                limit="MAX_CONCURRENT_CALLS",
            )

    async def _load_credentials(
        self, session: AsyncSession, tenant_id: uuid.UUID, version: Any
    ) -> dict[uuid.UUID, tuple[str, str | None]]:
        """Decrypt this tenant's credentials for the providers in use.

        Returns ``{provider_id: (api_key, base_url)}``. Providers with no
        credential are simply absent, which is valid for a self-hosted
        endpoint that needs none.
        """
        provider_ids = [
            version[key]
            for key in (
                "stt_provider_id",
                "llm_provider_id",
                "tts_provider_id",
                # A fallback the tenant has no key for is not a fallback, so
                # every tier's provider is resolved here rather than only the
                # primary's. Absent credentials stay absent — correct for a
                # self-hosted endpoint that needs none.
                "sttf_provider_id",
                "llmf_provider_id",
                "ttsf_provider_id",
                "sttl_provider_id",
                "ttsl_provider_id",
            )
            if version[key] is not None
        ]
        if not provider_ids:
            return {}

        rows = (
            (
                await session.execute(
                    _CREDENTIALS_SQL, {"tenant_id": tenant_id, "provider_ids": provider_ids}
                )
            )
            .mappings()
            .all()
        )

        resolved: dict[uuid.UUID, tuple[str, str | None]] = {}
        for record in rows:
            if self._cipher is None:
                # A stored credential we cannot read is a configuration fault
                # worth shouting about, not something to silently skip: the
                # call would otherwise fail later with an opaque provider 401.
                raise ConfigurationError(
                    "a provider credential is configured but no encryption key is "
                    f"available to decrypt it: {self._cipher_error}"
                )
            plaintext = self._cipher.decrypt(
                bytes(record["api_key_ciphertext"]), record["encryption_key_version"]
            )
            resolved[record["provider_id"]] = (plaintext, record["base_url"])

        return resolved

    @staticmethod
    def _chain(configs: tuple[ProviderConfig | None, ...]) -> tuple[ProviderConfig, ...]:
        """Drop the tiers this version did not configure, keeping the order."""
        return tuple(config for config in configs if config is not None)

    def _optional_provider_config(
        self,
        kind: ProviderKind,
        version: Any,
        prefix: str,
        credentials: dict[uuid.UUID, tuple[str, str | None]],
        *,
        language: str | None = None,
        temperature: float | None = None,
        voice_id: str | None = None,
    ) -> ProviderConfig | None:
        """A tier's configuration, or None when the version does not name one.

        The difference from ``_provider_config`` is the missing case: no
        primary provider is a configuration error that must stop the call, and
        no fallback is the ordinary state of most agents.
        """
        if version[f"{prefix}_slug"] is None:
            return None
        return self._provider_config(
            kind,
            version,
            prefix,
            credentials,
            language=language,
            temperature=temperature,
            voice_id=voice_id,
        )

    def _provider_config(
        self,
        kind: ProviderKind,
        version: Any,
        prefix: str,
        credentials: dict[uuid.UUID, tuple[str, str | None]],
        *,
        language: str | None = None,
        temperature: float | None = None,
        voice_id: str | None = None,
    ) -> ProviderConfig:
        slug = version[f"{prefix}_slug"]
        if slug is None:
            raise ConfigurationError(
                f"the published agent version has no {kind} provider configured"
            )

        provider_id = version[f"{prefix}_provider_id"]
        credential, credential_base_url = credentials.get(provider_id, (None, None))

        return ProviderConfig(
            kind=kind,
            provider=slug,
            model=version[f"{prefix}_model"],
            api_key=credential,
            # A tenant's own endpoint overrides the platform default, which is
            # what lets one tenant point at a self-hosted model (spec 25).
            base_url=credential_base_url or version[f"{prefix}_base_url"],
            language=language,
            temperature=temperature,
            voice_id=voice_id,
        )

    async def _create_call_row(
        self,
        session: AsyncSession,
        *,
        call_id: str,
        room_name: str,
        sip: SipCallInfo,
        row: Any,
        version_id: uuid.UUID,
        worker_id: str,
    ) -> uuid.UUID:
        """Insert the call record (spec 41) in its initial answered state."""
        call_row_id = uuid.uuid4()
        now = datetime.now(UTC)

        await session.execute(
            text(
                """
                INSERT INTO calls (
                    id, tenant_id, call_id, agent_id, agent_version_id,
                    pbx_id, sip_trunk_id, did, room_id, livekit_call_id,
                    caller_number, destination_number, direction,
                    start_time, answer_time, state, transfer_status,
                    worker_id, transfer_summary, created_at, updated_at
                ) VALUES (
                    :id, :tenant_id, :call_id, :agent_id, :agent_version_id,
                    :pbx_id, :sip_trunk_id, :did, :room_id, :livekit_call_id,
                    :caller_number, :destination_number, 'INBOUND',
                    :start_time, :answer_time, 'ANSWERED', 'NOT_REQUESTED',
                    :worker_id, '{}'::jsonb, :now, :now
                )
                """
            ),
            {
                "id": call_row_id,
                "tenant_id": row["tenant_id"],
                "call_id": call_id,
                "agent_id": row["agent_id"],
                "agent_version_id": version_id,
                "pbx_id": row["pbx_id"],
                "sip_trunk_id": row["sip_trunk_id"],
                "did": sip.called_number,
                "room_id": room_name,
                "livekit_call_id": sip.livekit_call_id,
                "caller_number": sip.caller_number,
                "destination_number": sip.called_number,
                "start_time": now,
                "answer_time": now,
                "worker_id": worker_id,
                "now": now,
            },
        )
        await session.commit()
        return call_row_id
