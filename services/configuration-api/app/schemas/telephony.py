"""SIP trunk and phone number schemas (spec 15, 17)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import TimestampedResponse
from shared.models import (
    ConnectionTestResult,
    ResourceStatus,
    RoomStrategy,
    SipTransport,
    SyncStatus,
    TrunkDirection,
)


def _normalise_e164(value: str) -> str:
    """Normalise a phone number to E.164.

    Stored one way so a lookup during call setup is an index hit rather than a
    scan over format variants — ``+8801700000000``, ``008801700000000`` and
    ``8801700000000`` must not become three different DIDs. Extensions are
    allowed through unchanged, since a PBX extension is not an E.164 number.
    """
    cleaned = "".join(ch for ch in value.strip() if ch.isdigit() or ch == "+")
    if not cleaned:
        raise ValueError("a phone number or extension is required")
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    if cleaned.count("+") > 1 or ("+" in cleaned and not cleaned.startswith("+")):
        raise ValueError("'+' may only appear once, at the start")
    digits = cleaned.lstrip("+")
    if not digits.isdigit():
        raise ValueError("a number may contain only digits and a leading '+'")
    if len(digits) > 15:
        raise ValueError("E.164 numbers are at most 15 digits")
    return cleaned


class SipTrunkBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    pbx_id: uuid.UUID | None = None
    direction: TrunkDirection = TrunkDirection.INBOUND
    sip_host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=5060, ge=1, le=65535)
    transport: SipTransport = SipTransport.UDP

    #: Source addresses permitted to send calls (spec 15, 53). Empty means no
    #: IP restriction.
    allowed_ips: list[str] = Field(default_factory=list)

    auth_username: str | None = Field(default=None, max_length=255)
    media_encryption_required: bool = False
    codecs: list[str] = Field(default_factory=list)
    dtmf_mode: str | None = Field(default=None, max_length=32)

    @field_validator("sip_host")
    @classmethod
    def _clean_host(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("SIP host cannot be blank")
        for scheme in ("sip:", "sips:"):
            if cleaned.lower().startswith(scheme):
                raise ValueError("give a hostname or IP, not a sip: URI")
        return cleaned


class SipTrunkCreate(SipTrunkBase):
    """Create a trunk.

    The password is write-only and never returned. It is stored encrypted in
    ``sip_credentials`` rather than on the trunk row, so listing trunks never
    loads ciphertext (spec 53).
    """

    auth_password: str | None = Field(default=None, min_length=1, max_length=255)


class SipTrunkUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    pbx_id: uuid.UUID | None = None
    direction: TrunkDirection | None = None
    sip_host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    transport: SipTransport | None = None
    allowed_ips: list[str] | None = None
    auth_username: str | None = Field(default=None, max_length=255)
    auth_password: str | None = Field(default=None, min_length=1, max_length=255)
    media_encryption_required: bool | None = None
    codecs: list[str] | None = None
    dtmf_mode: str | None = Field(default=None, max_length=32)
    status: ResourceStatus | None = None


class SipTrunkResponse(TimestampedResponse):
    name: str
    pbx_id: uuid.UUID | None
    direction: TrunkDirection
    sip_host: str
    port: int
    transport: SipTransport
    allowed_ips: list[str] = Field(default_factory=list)
    auth_username: str | None
    media_encryption_required: bool
    codecs: list[str] = Field(default_factory=list)
    dtmf_mode: str | None
    status: ResourceStatus

    #: Whether a password is configured. The value is never returned; this is
    #: what lets the UI show "configured" without decrypting anything.
    has_credential: bool = False

    #: The DIDs assigned to this trunk, which is what LiveKit matches an
    #: inbound call against. Derived from the phone_numbers table rather than
    #: stored on the trunk, so the two can never disagree — assigning a number
    #: to a trunk is the single action that changes what it accepts.
    accepted_numbers: list[str] = Field(default_factory=list)

    # --- LiveKit mirror (spec 12, 46) ------------------------------------- #
    livekit_resource_id: str | None
    sync_status: SyncStatus
    last_synced_at: datetime | None
    sync_error: str | None
    sync_attempts: int

    last_test_result: ConnectionTestResult
    last_tested_at: datetime | None
    last_test_detail: str | None


class DispatchRuleBase(BaseModel):
    """A long-lived LiveKit dispatch rule (spec 21).

    Created once and reused. A rule per call is refused — that would add an
    admin API round-trip to the call path and leave stale rules in LiveKit.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    sip_trunk_id: uuid.UUID | None = None
    room_strategy: RoomStrategy = RoomStrategy.INDIVIDUAL
    room_prefix: str | None = Field(default=None, max_length=128)
    agent_dispatch_name: str = Field(min_length=1, max_length=128)

    #: Caller-number allow-list on the LiveKit rule. Empty means any caller.
    #: This is **not** the dialled DID — LiveKit matches the caller's number
    #: here, and putting a DID in it is how inbound calls get ``486 flood``.
    matched_numbers: list[str] = Field(default_factory=list)

    @field_validator("name", "agent_dispatch_name")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("cannot be blank")
        return cleaned

    @field_validator("room_prefix")
    @classmethod
    def _strip_prefix(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class DispatchRuleCreate(DispatchRuleBase):
    pass


class DispatchRuleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    sip_trunk_id: uuid.UUID | None = None
    room_strategy: RoomStrategy | None = None
    room_prefix: str | None = Field(default=None, max_length=128)
    agent_dispatch_name: str | None = Field(default=None, min_length=1, max_length=128)
    matched_numbers: list[str] | None = None
    status: ResourceStatus | None = None


class DispatchRuleResponse(TimestampedResponse):
    name: str
    sip_trunk_id: uuid.UUID | None
    sip_trunk_name: str | None = None
    room_strategy: RoomStrategy
    room_prefix: str | None
    agent_dispatch_name: str
    matched_numbers: list[str] = Field(default_factory=list)
    status: ResourceStatus

    #: DIDs the attached trunk currently accepts — the numbers callers dial,
    #: which is how a rule is associated with an agent/DID (spec 21).
    trunk_dids: list[str] = Field(default_factory=list)

    livekit_resource_id: str | None
    sync_status: SyncStatus
    last_synced_at: datetime | None
    sync_error: str | None
    sync_attempts: int


class PhoneNumberBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: str = Field(min_length=1, max_length=32)
    label: str | None = Field(default=None, max_length=255)
    pbx_id: uuid.UUID | None = None
    sip_trunk_id: uuid.UUID | None = None
    inbound_agent_id: uuid.UUID | None = None
    routing_rule_id: uuid.UUID | None = None
    business_hours_id: uuid.UUID | None = None

    @field_validator("number")
    @classmethod
    def _clean_number(cls, value: str) -> str:
        return _normalise_e164(value)


class PhoneNumberCreate(PhoneNumberBase):
    pass


class PhoneNumberUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, max_length=255)
    pbx_id: uuid.UUID | None = None
    sip_trunk_id: uuid.UUID | None = None
    inbound_agent_id: uuid.UUID | None = None
    routing_rule_id: uuid.UUID | None = None
    business_hours_id: uuid.UUID | None = None
    status: ResourceStatus | None = None


class PhoneNumberResponse(TimestampedResponse):
    number: str
    label: str | None
    pbx_id: uuid.UUID | None
    sip_trunk_id: uuid.UUID | None
    inbound_agent_id: uuid.UUID | None
    routing_rule_id: uuid.UUID | None
    business_hours_id: uuid.UUID | None
    status: ResourceStatus

    #: Resolved names, so the UI can render a readable row without a request
    #: per foreign key.
    pbx_name: str | None = None
    sip_trunk_name: str | None = None
    agent_name: str | None = None
    routing_rule_name: str | None = None
    business_hours_name: str | None = None
