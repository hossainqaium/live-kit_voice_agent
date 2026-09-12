"""Fallback after a failed human leg (spec 38, TR-10)."""

from __future__ import annotations

from dataclasses import dataclass

from shared.models import TRANSFER_FALLBACK_TRIGGERS, FallbackAction, TransferStatus
from worker.transfer.destinations import TransferDestinationInfo, TransferFallback, resolve_destination


@dataclass(frozen=True, slots=True)
class FallbackPlan:
    """What to do so the caller is never left in silence."""

    action: FallbackAction
    destination: TransferDestinationInfo | None
    spoken: str
    hangup: bool


_APOLOGY = {
    FallbackAction.SECONDARY_AGENT: (
        "I'm sorry, no one is available right now. A colleague will follow up. Goodbye."
    ),
    FallbackAction.PBX_QUEUE: (
        "I'm transferring you to the queue. Please stay on the line."
    ),
    FallbackAction.VOICEMAIL: (
        "No one is available. Please leave a message after the tone."
    ),
    FallbackAction.HANGUP: (
        "I'm sorry, I could not reach a human agent. Goodbye."
    ),
}


def is_fallback_trigger(status: TransferStatus) -> bool:
    return status in TRANSFER_FALLBACK_TRIGGERS


def plan_fallback(
    status: TransferStatus,
    *,
    fallback: TransferFallback | None,
    destinations: tuple[TransferDestinationInfo, ...],
    already_tried: frozenset | None = None,
) -> FallbackPlan | None:
    """Map a failed human-leg status onto the configured chain.

    A queue or voicemail destination that has not been tried yet is returned
    so the engine can dial it while still in ``DIALING_AGENT``. Everything
    else is a spoken apology and a clean hangup — the call state machine
    cannot return from ``TRANSFERRING`` to ``IN_PROGRESS``.
    """
    if status not in TRANSFER_FALLBACK_TRIGGERS:
        return None

    action = fallback.action if fallback is not None else FallbackAction.HANGUP
    if action is None:
        action = FallbackAction.HANGUP

    tried = already_tried or frozenset()
    destination = None
    if fallback is not None and fallback.destination_id is not None:
        destination = resolve_destination(destinations, destination_id=fallback.destination_id)
        if destination is not None and destination.id in tried:
            destination = None

    retry = (
        action in {FallbackAction.PBX_QUEUE, FallbackAction.VOICEMAIL}
        and destination is not None
    )
    spoken = _APOLOGY.get(action, _APOLOGY[FallbackAction.HANGUP])
    if retry:
        spoken = _APOLOGY.get(action, spoken)
    return FallbackPlan(
        action=action,
        destination=destination if retry else None,
        spoken=spoken,
        hangup=not retry,
    )
