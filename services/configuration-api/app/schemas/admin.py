"""User management, tenant settings, analytics and usage schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.schemas.common import TimestampedResponse
from shared.models import TenantRole, TenantStatus


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    full_name: str = Field(min_length=1, max_length=255)

    #: Minimum length is enforced server-side by the hasher, which also
    #: rejects anything bcrypt would truncate.
    password: str = Field(min_length=12, max_length=72)

    role: TenantRole


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    role: TenantRole | None = None
    is_active: bool | None = None


class PasswordReset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=12, max_length=72)


class UserResponse(TimestampedResponse):
    email: str
    full_name: str
    is_active: bool
    last_login_at: datetime | None
    roles: list[str] = Field(default_factory=list)

    #: True once sessions have been revoked, so the UI can show that existing
    #: tokens stopped working rather than only that the account was edited.
    sessions_revoked: bool = False


class TenantSettingsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    status: TenantStatus
    timezone: str
    default_language: str
    max_concurrent_calls: int | None
    max_daily_calls: int | None
    max_monthly_minutes: int | None
    notes: str | None


class TenantImportSummary(BaseModel):
    """What an import created, updated, skipped, or refused (spec 65)."""

    created: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class TenantSettingsUpdate(BaseModel):
    """What a tenant administrator may change about their own tenant.

    Deliberately excludes the call limits: those are commercial terms the
    platform sets, and a tenant raising its own concurrency cap would make
    spec 47 meaningless.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    timezone: str | None = Field(default=None, max_length=64)
    default_language: str | None = Field(default=None, max_length=16)
    notes: str | None = Field(default=None, max_length=4000)


class AnalyticsResponse(BaseModel):
    """Business metrics from spec 57, computed from the calls table."""

    window_days: int

    total_calls: int
    answered_calls: int
    failed_calls: int
    transferred_calls: int

    #: Seconds, so the client decides how to render it.
    total_seconds: int
    average_duration_seconds: float | None

    answer_rate: float | None
    by_state: dict[str, int] = Field(default_factory=dict)
    by_agent: list[dict] = Field(default_factory=list)
    by_day: list[dict] = Field(default_factory=list)

    #: Voice-latency percentiles are absent on purpose: they live in
    #: Prometheus, not PostgreSQL, and computing them here would give a second
    #: number that disagrees with the dashboard.
    latency_note: str = (
        "Per-turn latency is in Prometheus and Grafana, not here — "
        "one source avoids two numbers that disagree."
    )


class UsageResponse(BaseModel):
    """Usage against the tenant's limits (spec 47, 61)."""

    model_config = ConfigDict(from_attributes=True)

    usage_date: date
    call_count: int
    answered_count: int
    failed_count: int
    transferred_count: int
    total_seconds: int
    ai_seconds: int


class UsageSummary(BaseModel):
    """Current standing against each limit.

    Reports what is actually enforced. ``max_concurrent_calls`` is checked
    before a call is accepted; the daily and monthly limits read the usage
    rollup, which is not yet written, so they are reported as unenforced
    rather than shown as if they were live.
    """

    max_concurrent_calls: int | None
    max_daily_calls: int | None
    max_monthly_minutes: int | None

    calls_today: int
    minutes_this_month: int
    active_calls: int

    daily_limit_enforced: bool = False
    monthly_limit_enforced: bool = False
    concurrent_limit_enforced: bool = True

    days: list[UsageResponse] = Field(default_factory=list)


class RecordingResponse(TimestampedResponse):
    """Recording metadata (spec 39).

    Metadata only: spec 3 and 39 both keep the audio in object storage, so
    this is a pointer plus what a list view needs to show.
    """

    call_id: uuid.UUID
    bucket: str
    object_key: str
    content_type: str | None
    byte_size: int | None
    duration_seconds: int | None
    livekit_egress_id: str | None
    started_at: datetime | None
    ended_at: datetime | None
    delete_after: datetime | None
