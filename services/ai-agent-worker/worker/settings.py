"""Worker settings.

Infrastructure wiring only. Everything about how a call behaves — prompt,
providers, voice, tools, knowledge base, transfer rules, call policies — is
loaded per call from the Control Plane (spec 23) and never configured here.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import PostgresDsn, SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "staging", "production"]


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    service_name: str = "ai-agent-worker"
    environment: Environment = "development"
    log_level: str = "info"

    # --- Health endpoint -------------------------------------------------- #
    health_host: str = "0.0.0.0"

    #: Not 8081: the LiveKit Agents runtime runs its own HTTP server there, and
    #: two listeners on one port means the worker fails to start.
    health_port: int = 8090
    readiness_timeout_seconds: float = 2.0

    # --- LiveKit ---------------------------------------------------------- #
    livekit_url: str = "ws://livekit:7880"
    livekit_api_key: SecretStr = SecretStr("devkey")
    livekit_api_secret: SecretStr = SecretStr("devsecret-at-least-32-characters-long")

    #: Agent-dispatch identity registered with LiveKit. Dispatch rules created
    #: by the Control Plane target this name (spec 21).
    worker_agent_name: str = "voice-agent"

    # --- Capacity --------------------------------------------------------- #
    #: A soft cap only. The real safe figure comes from load testing
    #: (spec 49, 74) — this default is a starting point, not a claim.
    worker_max_concurrent_calls: int = 10

    #: Drain budget on SIGTERM (spec 51). Must exceed the longest expected call
    #: in production, or a deploy will cut conversations off mid-sentence.
    worker_drain_timeout_seconds: int = 600

    # --- Development test path (Plan 2b.10) -------------------------------- #
    #: Accept a browser participant that supplies a DID, so the pipeline can be
    #: exercised without a PBX.
    #:
    #: **Defaults to off, and is refused outside development.** The SIP gate it
    #: relaxes is what guarantees every call has a tenant: SIP attributes carry
    #: the DID, the DID is the only route to a tenant, and a call row with a
    #: null tenant would violate spec 6. This path still resolves a DID — it
    #: accepts one from a participant instead of from the SIP stack — so the
    #: isolation guarantee holds. What it gives up is the assurance that the
    #: DID came from the telephony network, which is why it is not a production
    #: configuration.
    allow_browser_test_participant: bool = False

    #: How long to wait for a browser participant once no SIP one arrived.
    #: Short: by this point the job is already past the SIP timeout, and a
    #: browser client that is coming has usually arrived first.
    browser_test_participant_timeout_seconds: float = 5.0

    # --- Conversation ------------------------------------------------------ #
    #: Speak a short acknowledgement while a reply is being produced.
    #:
    #: **Off, because it cannot be made safe on livekit-agents 1.8.** Measured
    #: rather than assumed, over several live calls:
    #:
    #: * ``session.say`` has no priority argument, and LiveKit queues the reply
    #:   the moment the turn commits. A filler started after that plays *after*
    #:   the answer.
    #: * The only earlier window is before the turn commits — and an agent that
    #:   speaks there destroys the caller's pending turn. Three consecutive
    #:   calls produced a greeting, a filler, and **zero caller transcript
    #:   segments**: the question was discarded, so nothing was ever answered.
    #:
    #: That is strictly worse than the silence it set out to cover, so it is
    #: off. The code and its tests are kept because the mechanism is sound —
    #: it needs an SDK that can either prioritise a speech handle or emit a
    #: backchannel, and `_AgentBackchannelOpportunityEvent` suggests one is
    #: coming.
    #:
    #: The real fix for the gap is to make it shorter: Plan 2b.9 (STT
    #: placement) and preemptive generation.
    enable_thinking_filler: bool = False

    # --- Configuration source --------------------------------------------- #
    postgres_host: str = "postgresql"
    postgres_port: int = 5432
    postgres_db: str = "voice_agent"
    postgres_user: str = "voice_agent"
    postgres_password: SecretStr = SecretStr("voice_agent")
    database_url: PostgresDsn | None = None

    redis_url: str = "redis://redis:6379/0"

    credential_encryption_key: SecretStr = SecretStr("")

    # --- Object storage --------------------------------------------------- #
    s3_endpoint_url: str | None = "http://minio:9000"
    s3_bucket_recordings: str = "recordings"
    s3_access_key_id: SecretStr = SecretStr("minioadmin")
    s3_secret_access_key: SecretStr = SecretStr("minioadmin")
    s3_region: str = "us-east-1"

    # repr=False: the DSN embeds the password, and a settings object rendered
    # into a traceback or log line would otherwise leak it (spec 54).
    @computed_field(repr=False)  # type: ignore[prop-decorator]
    @property
    def sqlalchemy_dsn(self) -> str:
        if self.database_url is not None:
            dsn = str(self.database_url)
            if dsn.startswith("postgresql://"):
                dsn = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
            return dsn
        password = self.postgres_password.get_secret_value()
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache(maxsize=1)
def get_settings() -> WorkerSettings:
    return WorkerSettings()
