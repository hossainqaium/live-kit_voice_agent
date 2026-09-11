"""Routing rule, business hours and transfer destination schemas (spec 20, 37, 35)."""

from __future__ import annotations

import uuid
from datetime import time

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.common import TimestampedResponse
from shared.models import DayOfWeek, FallbackAction, ResourceStatus, TransferDestinationKind


class RoutingConditions(BaseModel):
    """Match conditions from spec 20.

    Every field optional, and an empty object matches every call — which is
    what a tenant-wide default rule needs. Stored as JSONB because spec 20
    lists nine dimensions and tenants use different subsets; a column each
    would be mostly nulls and still not extensible.
    """

    model_config = ConfigDict(extra="forbid")

    pbx_id: uuid.UUID | None = None
    sip_trunk_id: uuid.UUID | None = None
    did: str | None = Field(default=None, max_length=32)
    caller_number: str | None = Field(default=None, max_length=64)
    caller_number_prefix: str | None = Field(default=None, max_length=32)
    destination_number: str | None = Field(default=None, max_length=64)
    campaign: str | None = Field(default=None, max_length=128)


class RoutingRuleBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)

    #: Lower runs first. Ties break on specificity, so a broad catch-all
    #: cannot shadow a precise rule.
    priority: int = Field(default=100, ge=0, le=10000)

    conditions: RoutingConditions = Field(default_factory=RoutingConditions)

    agent_id: uuid.UUID | None = None
    business_hours_id: uuid.UUID | None = None

    fallback_action: FallbackAction | None = None
    fallback_agent_id: uuid.UUID | None = None
    fallback_transfer_destination_id: uuid.UUID | None = None

    #: Where calls go outside business hours. Separate from the failure
    #: fallback: "we are closed" and "the agent could not be reached" deserve
    #: different handling.
    closed_action: FallbackAction | None = None
    closed_transfer_destination_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _fallback_needs_a_target(self) -> RoutingRuleBase:
        if self.fallback_action is FallbackAction.SECONDARY_AGENT and not self.fallback_agent_id:
            raise ValueError("a secondary-agent fallback needs a fallback agent")
        action = self.fallback_action
        if (
            action in (FallbackAction.PBX_QUEUE, FallbackAction.VOICEMAIL)
            and not self.fallback_transfer_destination_id
        ):
            raise ValueError(f"a {action.value if action else action} fallback needs a destination")
        return self


class RoutingRuleCreate(RoutingRuleBase):
    pass


class RoutingRuleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    priority: int | None = Field(default=None, ge=0, le=10000)
    conditions: RoutingConditions | None = None
    agent_id: uuid.UUID | None = None
    business_hours_id: uuid.UUID | None = None
    fallback_action: FallbackAction | None = None
    fallback_agent_id: uuid.UUID | None = None
    fallback_transfer_destination_id: uuid.UUID | None = None
    closed_action: FallbackAction | None = None
    closed_transfer_destination_id: uuid.UUID | None = None
    status: ResourceStatus | None = None


class RoutingRuleResponse(TimestampedResponse):
    name: str
    description: str | None
    priority: int
    conditions: dict
    agent_id: uuid.UUID | None
    business_hours_id: uuid.UUID | None
    fallback_action: FallbackAction | None
    fallback_agent_id: uuid.UUID | None
    fallback_transfer_destination_id: uuid.UUID | None
    closed_action: FallbackAction | None
    closed_transfer_destination_id: uuid.UUID | None
    status: ResourceStatus

    agent_name: str | None = None
    business_hours_name: str | None = None


class IntervalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day_of_week: DayOfWeek
    opens_at: time
    closes_at: time

    @model_validator(mode="after")
    def _forward(self) -> IntervalInput:
        if self.opens_at >= self.closes_at:
            raise ValueError("opens_at must be before closes_at")
        return self


class IntervalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    day_of_week: DayOfWeek
    opens_at: time
    closes_at: time


class BusinessHoursCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)

    #: Overrides the tenant timezone when a tenant operates in more than one.
    timezone: str | None = Field(default=None, max_length=64)

    intervals: list[IntervalInput] = Field(default_factory=list)

    #: Dates that override the weekly schedule.
    holidays: list[dict] = Field(default_factory=list)


class BusinessHoursUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    timezone: str | None = Field(default=None, max_length=64)
    intervals: list[IntervalInput] | None = None
    holidays: list[dict] | None = None


class BusinessHoursResponse(TimestampedResponse):
    name: str
    timezone: str | None
    holidays: list = Field(default_factory=list)
    intervals: list[IntervalResponse] = Field(default_factory=list)

    #: Whether the schedule says "open" right now, computed in the tenant's
    #: timezone. The single most useful thing to show on a list row.
    open_now: bool | None = None


class TransferDestinationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    kind: TransferDestinationKind
    target: str = Field(min_length=1, max_length=512)
    pbx_id: uuid.UUID | None = None
    sip_trunk_id: uuid.UUID | None = None

    #: Whether the AI summary is whispered to the receiving agent before the
    #: legs are bridged (CR-1 TR-5).
    whisper_summary: bool = True
    ring_timeout_seconds: int = Field(default=30, ge=5, le=300)


class TransferDestinationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    kind: TransferDestinationKind | None = None
    target: str | None = Field(default=None, min_length=1, max_length=512)
    pbx_id: uuid.UUID | None = None
    sip_trunk_id: uuid.UUID | None = None
    whisper_summary: bool | None = None
    ring_timeout_seconds: int | None = Field(default=None, ge=5, le=300)
    status: ResourceStatus | None = None


class TransferDestinationResponse(TimestampedResponse):
    name: str
    kind: TransferDestinationKind
    target: str
    pbx_id: uuid.UUID | None
    sip_trunk_id: uuid.UUID | None
    whisper_summary: bool
    ring_timeout_seconds: int
    status: ResourceStatus
