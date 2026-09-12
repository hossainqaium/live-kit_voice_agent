"""Application settings.

Every value comes from the environment. Nothing tenant-specific appears here:
tenant configuration lives in PostgreSQL and is managed through the API
(spec 9, 10). The variables below are infrastructure wiring only, and in
production their secret-bearing members are injected from a secret manager
rather than a file (spec 54).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, PostgresDsn, SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["development", "staging", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Service identity ------------------------------------------------- #
    service_name: str = "configuration-api"
    environment: Environment = "development"
    log_level: str = "info"
    api_base_path: str = "/api/v1"

    # --- PostgreSQL ------------------------------------------------------- #
    postgres_host: str = "postgresql"
    postgres_port: int = 5432
    postgres_db: str = "voice_agent"
    postgres_user: str = "voice_agent"
    postgres_password: SecretStr = SecretStr("voice_agent")
    database_url: PostgresDsn | None = None
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo: bool = False

    # --- Redis ------------------------------------------------------------ #
    redis_url: str = "redis://redis:6379/0"
    redis_call_state_ttl_seconds: int = 3600

    # --- Auth ------------------------------------------------------------- #
    jwt_secret: SecretStr = SecretStr("change-me-in-every-real-environment")
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 14

    #: Envelope key for provider and SIP credentials at rest (spec 53).
    credential_encryption_key: SecretStr = SecretStr("")

    # --- LiveKit ---------------------------------------------------------- #
    livekit_url: str = "ws://livekit:7880"

    #: The address a *browser* uses, which is not the one the services use.
    #: Inside compose LiveKit is ``livekit:7880``; from the host it is the
    #: published port, which is 7980 by default because 7880 was already taken.
    #: Getting this wrong produces a client that hangs on connect with no error
    #: worth reading.
    livekit_public_url: str = "ws://localhost:7980"
    livekit_api_key: SecretStr = SecretStr("devkey")
    livekit_api_secret: SecretStr = SecretStr("devsecret-at-least-32-characters-long")
    livekit_sip_uri: str = "sip:livekit-sip:5060"

    #: Periodic compare of PostgreSQL against LiveKit (spec 46, Plan 5.7).
    #: Off in the test process so an in-process ASGI client does not call
    #: LiveKit. Compose leaves the default on.
    livekit_drift_check_enabled: bool = True
    livekit_drift_check_interval_seconds: float = 300.0
    livekit_drift_check_jitter_seconds: float = 30.0

    #: HTTP handlers mark the row PENDING and return; LiveKit work runs in a
    #: background task (spec 80). Tests turn this off so an in-process client
    #: does not open a second session against LiveKit.
    livekit_background_sync: bool = True

    #: Worker readiness URLs scraped by the capacity dashboard (spec 48).
    worker_health_urls: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://ai-agent-worker:8090"]
    )
    livekit_metrics_url: str = "http://livekit:6789/metrics"
    sip_metrics_url: str = ""

    # --- Object storage --------------------------------------------------- #
    s3_endpoint_url: str | None = "http://minio:9000"
    s3_bucket_recordings: str = "recordings"
    s3_access_key_id: SecretStr = SecretStr("minioadmin")
    s3_secret_access_key: SecretStr = SecretStr("minioadmin")
    s3_region: str = "us-east-1"

    # --- CORS ------------------------------------------------------------- #
    # NoDecode stops pydantic-settings from JSON-parsing the environment value
    # before the validator below runs, which is what lets a plain
    # comma-separated string work instead of requiring '["http://..."]'.
    cors_allow_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    # --- Readiness -------------------------------------------------------- #
    #: A readiness probe must fail fast. A slow dependency check that outlasts
    #: the probe timeout is indistinguishable from an outage, and worse, it
    #: keeps the connection open while Kubernetes retries.
    readiness_timeout_seconds: float = 2.0

    @field_validator("cors_allow_origins", "worker_health_urls", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    # repr=False: the DSN embeds the password, and a settings object rendered
    # into a traceback or log line would otherwise leak it (spec 54).
    @computed_field(repr=False)  # type: ignore[prop-decorator]
    @property
    def sqlalchemy_dsn(self) -> str:
        """Async DSN for SQLAlchemy.

        ``database_url`` wins when set so a managed database can be supplied as
        a single connection string.
        """
        if self.database_url is not None:
            dsn = str(self.database_url)
            # pydantic normalises to postgresql://; SQLAlchemy needs the driver.
            if dsn.startswith("postgresql://"):
                dsn = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
            return dsn
        password = self.postgres_password.get_secret_value()
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # --- Browser test calls (Plan 2b.10) ---------------------------------- #
    #: Allow the console to open a test call from the browser.
    #:
    #: This is the only endpoint in the platform that issues a credential: it
    #: mints a LiveKit join token. So it carries four independent guards —
    #: this flag, ``environment == "development"``, the ``agents.write``
    #: permission, and the DID being resolved through the tenant repository.
    #:
    #: The code default is False so a deployment that configures nothing is
    #: safe. Development ``.env`` turns it on, because a test path nobody can
    #: reach without editing configuration is a test path nobody uses.
    allow_browser_test_sessions: bool = False

    #: How long a minted join token is valid. Short: it is issued for one test
    #: call that is about to start, not for a session.
    browser_test_token_ttl_seconds: int = 600

    #: Agent-dispatch identity, matching the worker's registered name. Needed
    #: because a browser test room has no SIP trunk and therefore no dispatch
    #: rule to read it from.
    worker_agent_name: str = "voice-agent"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
