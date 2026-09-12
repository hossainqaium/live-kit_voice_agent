"""Dial the human agent through the tenant PBX (spec 35, TR-1)."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Any

from shared.logging import get_logger
from worker.transfer.destinations import TransferDestinationInfo
from worker.transfer.isolation import human_identity

logger = get_logger(__name__)


class DialOutcome(StrEnum):
    ANSWERED = "ANSWERED"
    NO_ANSWER = "NO_ANSWER"
    BUSY = "BUSY"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


def map_sip_error(exc: BaseException) -> DialOutcome:
    """Translate a LiveKit / SIP failure into a transfer branch (TR-10)."""
    text = str(exc).lower()
    if "486" in text or "busy" in text:
        return DialOutcome.BUSY
    if "603" in text or "603 decline" in text or "rejected" in text or "declin" in text:
        return DialOutcome.REJECTED
    if "480" in text or "408" in text or "no answer" in text or "timeout" in text:
        return DialOutcome.NO_ANSWER
    return DialOutcome.FAILED


async def dial_human(
    destination: TransferDestinationInfo,
    *,
    room_name: str,
    call_id: str,
    headers: dict[str, str] | None = None,
    participant_metadata: str | None = None,
) -> DialOutcome:
    """Create a SIP participant in the same room and wait until they answer."""
    if not destination.target:
        return DialOutcome.FAILED
    if not destination.livekit_trunk_id:
        logger.error(
            "transfer_dial_missing_trunk",
            extra={"destination": destination.name, "kind": destination.kind.value},
        )
        return DialOutcome.FAILED

    try:
        from livekit.api import CreateSIPParticipantRequest, LiveKitAPI
    except Exception:
        logger.exception("transfer_dial_api_unavailable")
        return DialOutcome.FAILED

    from worker.settings import get_settings

    settings = get_settings()
    lk_url = settings.livekit_url.replace("ws://", "http://").replace("wss://", "https://")
    identity = human_identity(call_id)
    timeout = max(5, int(destination.ring_timeout_seconds or 30))
    request_kwargs: dict[str, Any] = {
        "sip_trunk_id": destination.livekit_trunk_id,
        "sip_call_to": destination.target,
        "room_name": room_name,
        "participant_identity": identity,
        "participant_name": "Human Agent",
        "wait_until_answered": True,
        "play_dialtone": False,
        "participant_attributes": {"role": "human_agent"},
    }
    if headers:
        request_kwargs["headers"] = headers
    if participant_metadata:
        request_kwargs["participant_metadata"] = participant_metadata
    try:
        request_kwargs["ringing_timeout"] = timedelta(seconds=timeout)
    except Exception:
        pass

    try:
        req = CreateSIPParticipantRequest(**request_kwargs)
        async with LiveKitAPI(
            url=lk_url,
            api_key=settings.livekit_api_key.get_secret_value(),
            api_secret=settings.livekit_api_secret.get_secret_value(),
        ) as lk:
            await lk.sip.create_sip_participant(req)
        logger.info(
            "transfer_human_answered",
            extra={"destination": destination.name, "target": destination.target},
        )
        return DialOutcome.ANSWERED
    except TypeError:
        # Older livekit-api builds reject unknown fields; retry the minimum set.
        try:
            req = CreateSIPParticipantRequest(
                sip_trunk_id=destination.livekit_trunk_id,
                sip_call_to=destination.target,
                room_name=room_name,
                participant_identity=identity,
                participant_name="Human Agent",
                wait_until_answered=True,
            )
            async with LiveKitAPI(
                url=lk_url,
                api_key=settings.livekit_api_key.get_secret_value(),
                api_secret=settings.livekit_api_secret.get_secret_value(),
            ) as lk:
                await lk.sip.create_sip_participant(req)
            return DialOutcome.ANSWERED
        except Exception as exc:
            logger.warning("transfer_dial_failed", extra={"error": str(exc)})
            return map_sip_error(exc)
    except Exception as exc:
        logger.warning("transfer_dial_failed", extra={"error": str(exc)})
        return map_sip_error(exc)
