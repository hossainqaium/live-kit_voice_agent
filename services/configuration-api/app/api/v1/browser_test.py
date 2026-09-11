"""Browser test calls (Plan 2b.10).

Opens a test call from the console, with no PBX in the path, so an operator can
hear their own agent before pointing a phone number at it.

**This is the only endpoint in the platform that issues a credential.**
Everything else reads or writes configuration; this mints a LiveKit join token,
which grants media access to a room. It is therefore gated four independent
ways, and they are all in this file so that none of them can be removed without
reading the reason:

1. ``allow_browser_test_sessions`` — off unless configured on.
2. ``environment == "development"`` — refused anywhere else, whatever (1) says.
3. ``agents.write`` — the permission for changing what an agent does, which is
   what testing one is in service of.
4. The DID is resolved through ``TenantRepository``, so a number belonging to
   another tenant is a 404 and not a token.

The token itself is scoped as narrowly as LiveKit allows: join only, one named
room, no create, no admin, no list, and a ten-minute life.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.dependencies import ClientIp, CurrentTenant, require_permission
from app.core.settings import get_settings
from app.db.models import Agent, PhoneNumber
from app.db.repository import TenantRepository
from app.livekit import LiveKitAdminClient, LiveKitError
from app.schemas.browser_test import BrowserTestSession, BrowserTestSessionRequest
from app.services import audit
from shared.logging import get_logger
from shared.models import Permission, ResourceStatus

logger = get_logger(__name__)

router = APIRouter(tags=["browser-test"])


def _refuse_unless_development() -> None:
    """Guards 1 and 2, together, before anything else happens."""
    settings = get_settings()
    if settings.environment != "development":
        # Deliberately 404 rather than 403: outside development this endpoint
        # does not exist as far as a caller is concerned, and saying "disabled"
        # tells an attacker what to go looking for.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if not settings.allow_browser_test_sessions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "browser test calls are switched off; set "
                "ALLOW_BROWSER_TEST_SESSIONS=true to enable them in development"
            ),
        )


@router.post(
    "/browser-test/session",
    response_model=BrowserTestSession,
    summary="Open a browser test call against one of this tenant's numbers",
    dependencies=[Depends(require_permission(Permission.AGENTS_WRITE))],
)
async def create_session(
    payload: BrowserTestSessionRequest,
    tenant: CurrentTenant,
    request: Request,
    client_ip: ClientIp,
) -> BrowserTestSession:
    """Create the room, dispatch the agent, and mint a join token.

    The room carries the DID in its metadata, which is where the worker reads
    it when no SIP participant arrives. That keeps one resolution path: the
    call resolves the same tenant, agent version and providers a real inbound
    call would, rather than a parallel arrangement that drifts.
    """
    _refuse_unless_development()
    settings = get_settings()

    repository = TenantRepository(tenant.session, tenant.tenant_id)

    # Guard 4. Scoped, so another tenant's number is indistinguishable from a
    # number that does not exist (spec 7).
    number = (
        await tenant.session.execute(
            repository.scoped(PhoneNumber).where(PhoneNumber.number == payload.did)
        )
    ).scalar_one_or_none()

    if number is None or number.status is not ResourceStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no active phone number with that value in this tenant",
        )

    if number.inbound_agent_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="that number has no inbound agent, so nothing would answer",
        )

    agent = await repository.get(Agent, number.inbound_agent_id)
    if agent is None or agent.published_version_id is None:
        # Worth its own message: an unpublished agent is the single most likely
        # reason a test call connects to silence, and it is fixable in one click.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="that number's agent has no published version, so it cannot answer",
        )

    room = f"browser-test-{tenant.tenant_id.hex[:8]}-{datetime.now(UTC).strftime('%H%M%S')}"
    metadata = json.dumps({"did": payload.did, "source": "console"})

    client = LiveKitAdminClient()
    try:
        await client.create_room_with_metadata(room, metadata)
        # The worker registers with an explicit agent name, so LiveKit will not
        # dispatch it on its own. Without this the browser joins a room nobody
        # answers, which is indistinguishable from a broken agent.
        await client.dispatch_agent(room, settings.worker_agent_name)
    except LiveKitError as exc:
        logger.error("browser_test_room_failed", extra={"error": str(exc), "room": room})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"could not prepare the room: {exc}",
        ) from exc

    from livekit.api import AccessToken, VideoGrants

    ttl = timedelta(seconds=settings.browser_test_token_ttl_seconds)
    token = (
        AccessToken(
            settings.livekit_api_key.get_secret_value(),
            settings.livekit_api_secret.get_secret_value(),
        )
        .with_identity(f"console-{tenant.principal.user_id.hex[:8]}")
        .with_name(tenant.principal.email)
        .with_attributes({"did": payload.did})
        .with_ttl(ttl)
        # Join one named room. No room_create, no room_admin, no room_list:
        # this token can do exactly what the test needs and nothing else.
        .with_grants(VideoGrants(room_join=True, room=room))
        .to_jwt()
    )

    await audit.record(
        tenant.session,
        tenant_id=tenant.tenant_id,
        principal=tenant.principal,
        action="agent.browser_test_started",
        resource_type="agent",
        resource_id=agent.id,
        new_value={"did": payload.did, "room": room},
        ip_address=client_ip,
        request_id=getattr(request.state, "request_id", None),
    )
    await tenant.session.commit()

    logger.warning(
        "browser_test_session_issued",
        extra={"room": room, "did": payload.did, "user_id": str(tenant.principal.user_id)},
    )

    return BrowserTestSession(
        room=room,
        token=token,
        url=settings.livekit_public_url,
        agent_name=agent.name,
        did=payload.did,
        expires_at=datetime.now(UTC) + ttl,
    )
