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
    health_port: int = 8081
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
