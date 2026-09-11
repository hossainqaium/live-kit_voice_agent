"""PBX schemas (spec 14)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import TimestampedResponse
from shared.models import ConnectionTestResult, PbxType, ResourceStatus, SipTransport


class PbxBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    pbx_type: PbxType
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=5060, ge=1, le=65535)
    transport: SipTransport = SipTransport.UDP
    description: str | None = Field(default=None, max_length=4000)

    @field_validator("host")
    @classmethod
    def _strip_host(cls, value: str) -> str:
        """Normalise the host.

        A trailing space or a stray scheme is the kind of thing that produces a
        SIP failure with no obvious cause, so it is rejected at the edge rather
        than carried into a trunk definition.
        """
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("host cannot be blank")
        for scheme in ("sip:", "sips:", "http://", "https://"):
            if cleaned.lower().startswith(scheme):
                raise ValueError(f"host should be a hostname or IP, not a {scheme} URI")
        if "/" in cleaned:
            raise ValueError("host should not contain a path")
        return cleaned


class PbxCreate(PbxBase):
    """Create a PBX record.

    This registers something the tenant already operates; the platform does not
    provision it.
    """


class PbxUpdate(BaseModel):
    """Partial update. Every field optional so a PATCH-style PUT is possible."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    pbx_type: PbxType | None = None
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    transport: SipTransport | None = None
    status: ResourceStatus | None = None
    description: str | None = Field(default=None, max_length=4000)


class PbxResponse(TimestampedResponse):
    name: str
    pbx_type: PbxType
    host: str
    port: int
    transport: SipTransport
    status: ResourceStatus
    description: str | None

    #: Connection test outcome (spec 14). Exposed so the UI can show whether
    #: the record has ever been proven to work, rather than only that it was
    #: saved.
    last_test_result: ConnectionTestResult
    last_tested_at: datetime | None
    last_test_detail: str | None


class PbxTestResult(BaseModel):
    """Outcome of a connection test (spec 14)."""

    result: ConnectionTestResult
    detail: str
    tested_at: datetime
    latency_ms: float | None = None
