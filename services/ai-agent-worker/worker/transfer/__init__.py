"""Human transfer and conversation summary generation (spec 35, 36)."""

from worker.transfer.destinations import (
    TransferDestinationInfo,
    TransferFallback,
    load_destinations,
    resolve_destination,
)
from worker.transfer.isolation import HUMAN_IDENTITY_PREFIX, human_identity, is_human_agent
from worker.transfer.registry import get as get_transfer
from worker.transfer.registry import register as register_transfer
from worker.transfer.registry import unregister as unregister_transfer
from worker.transfer.summary import (
    DEFAULT_SPOKEN_TEMPLATE,
    SUMMARY_FIELDS,
    TransferSummary,
    fallback_whisper,
    generate_transfer_summary,
    render_spoken,
)

# Engine imports CallContext under TYPE_CHECKING only; keep this last so a
# config_loader → destinations import cannot cycle through WarmTransfer.
from worker.transfer.engine import DEFAULT_ANNOUNCEMENT, WarmTransfer

__all__ = [
    "DEFAULT_ANNOUNCEMENT",
    "DEFAULT_SPOKEN_TEMPLATE",
    "HUMAN_IDENTITY_PREFIX",
    "SUMMARY_FIELDS",
    "TransferDestinationInfo",
    "TransferFallback",
    "TransferSummary",
    "WarmTransfer",
    "fallback_whisper",
    "generate_transfer_summary",
    "get_transfer",
    "human_identity",
    "is_human_agent",
    "load_destinations",
    "register_transfer",
    "render_spoken",
    "resolve_destination",
    "unregister_transfer",
]
