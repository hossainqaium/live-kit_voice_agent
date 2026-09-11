"""Browser test call schemas (Plan 2b.10)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class BrowserTestSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The number to call. Validated against this tenant's own phone numbers,
    #: which is what stops the endpoint being a way to reach another tenant's
    #: agent.
    did: str = Field(min_length=1, max_length=32)


class BrowserTestSession(BaseModel):
    """Everything the browser needs to join, and nothing else.

    ``token`` is a short-lived LiveKit join grant for one room. It is the only
    credential any endpoint in this API returns, which is why the endpoint that
    produces it carries its guards in one file.
    """

    room: str
    token: str
    url: str

    #: Shown in the console so the operator can see which agent will answer
    #: before speaking to it.
    agent_name: str
    did: str
    expires_at: datetime
