"""Hard audio isolation between the caller and the human agent (TR-6).

The whisper is published on a dedicated track that only the human SIP
participant is allowed to subscribe to. The caller and the human are
unsubscribed from each other until the bridge, so the caller cannot hear
the summary even if both legs share one LiveKit room.
"""

from __future__ import annotations

from typing import Any

from shared.logging import get_logger

logger = get_logger(__name__)

HUMAN_IDENTITY_PREFIX = "human-agent-"

WHISPER_TRACK_NAME = "agent-whisper"
HOLD_TRACK_NAME = "caller-hold"


def human_identity(call_id: str) -> str:
    return f"{HUMAN_IDENTITY_PREFIX}{call_id}"


def is_human_agent(identity: str | None) -> bool:
    return bool(identity) and str(identity).startswith(HUMAN_IDENTITY_PREFIX)


def is_inbound_caller(participant: Any) -> bool:
    """True for the original caller, never the human we just dialled."""
    identity = getattr(participant, "identity", None)
    return not is_human_agent(identity)


def caller_identity(room: Any) -> str | None:
    """The remote SIP (or browser-test) participant that is not the human."""
    remote = getattr(room, "remote_participants", None) or {}
    if hasattr(remote, "values"):
        participants = list(remote.values())
    elif isinstance(remote, dict):
        participants = list(remote.values())
    else:
        participants = list(remote or [])
    for participant in participants:
        identity = getattr(participant, "identity", None)
        if identity and not is_human_agent(identity):
            return str(identity)
    return None


async def restrict_local_tracks(
    room: Any,
    *,
    permissions: list[tuple[str, list[str]]],
) -> None:
    """Allow only named participants to subscribe to the listed local tracks."""
    local = getattr(room, "local_participant", None)
    setter = getattr(local, "set_track_subscription_permissions", None)
    if setter is None:
        logger.warning("transfer_isolation_permissions_unavailable")
        return
    try:
        from livekit import rtc
    except Exception:
        logger.warning("transfer_isolation_rtc_unavailable")
        return

    grants = [
        rtc.ParticipantTrackPermission(
            participant_identity=identity,
            allow_all=False,
            allowed_track_sids=list(sids),
        )
        for identity, sids in permissions
        if identity
    ]
    result = setter(allow_all_participants=False, participant_permissions=grants)
    if hasattr(result, "__await__"):
        await result
    logger.info(
        "transfer_track_permissions_set",
        extra={"participants": [identity for identity, _ in permissions]},
    )


async def isolate_remote_legs(
    *,
    room_name: str,
    caller_id: str | None,
    human_id: str,
) -> None:
    """Unsubscribe the two SIP legs from each other (TR-6)."""
    if not caller_id:
        return
    try:
        from livekit.api import LiveKitAPI, UpdateSubscriptionsRequest
    except Exception:
        logger.warning("transfer_isolation_api_unavailable")
        return

    from worker.settings import get_settings

    settings = get_settings()
    lk_url = settings.livekit_url.replace("ws://", "http://").replace("wss://", "https://")
    try:
        async with LiveKitAPI(
            url=lk_url,
            api_key=settings.livekit_api_key.get_secret_value(),
            api_secret=settings.livekit_api_secret.get_secret_value(),
        ) as lk:
            for identity, other in ((human_id, caller_id), (caller_id, human_id)):
                await lk.room.update_subscriptions(
                    UpdateSubscriptionsRequest(
                        room=room_name,
                        identity=identity,
                        track_sids=[],
                        subscribe=False,
                        participant_tracks=[],
                    )
                )
                logger.debug(
                    "transfer_unsubscribed_leg",
                    extra={"identity": identity, "from": other},
                )
    except Exception:
        logger.exception("transfer_isolation_unsubscribe_failed")


async def bridge_remote_legs(
    *,
    room_name: str,
    caller_id: str | None,
    human_id: str,
) -> None:
    """Let the caller and the human hear each other after the whisper (TR-7)."""
    if not caller_id:
        return
    try:
        from livekit.api import LiveKitAPI, UpdateSubscriptionsRequest
    except Exception:
        logger.warning("transfer_bridge_api_unavailable")
        return

    from worker.settings import get_settings

    settings = get_settings()
    lk_url = settings.livekit_url.replace("ws://", "http://").replace("wss://", "https://")
    try:
        async with LiveKitAPI(
            url=lk_url,
            api_key=settings.livekit_api_key.get_secret_value(),
            api_secret=settings.livekit_api_secret.get_secret_value(),
        ) as lk:
            for identity in (human_id, caller_id):
                await lk.room.update_subscriptions(
                    UpdateSubscriptionsRequest(
                        room=room_name,
                        identity=identity,
                        track_sids=[],
                        subscribe=True,
                    )
                )
    except Exception:
        logger.exception("transfer_bridge_subscribe_failed")


async def allow_all_local_tracks(room: Any) -> None:
    """Clear the allow-list so leftover local tracks are not stuck isolated."""
    local = getattr(room, "local_participant", None)
    setter = getattr(local, "set_track_subscription_permissions", None)
    if setter is None:
        return
    result = setter(allow_all_participants=True, participant_permissions=[])
    if hasattr(result, "__await__"):
        await result
