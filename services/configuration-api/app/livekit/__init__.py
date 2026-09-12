"""LiveKit integration.

A dedicated module (spec 80 rule 7). Nothing outside it should import the
LiveKit SDK or hold the LiveKit API secret.
"""

from app.livekit.client import LiveKitAdminClient, RoomSnapshot
from app.livekit.drift import (
    DriftFinding,
    DriftKind,
    DriftReport,
    detect_drift,
    last_report,
)
from app.livekit.errors import (
    LiveKitError,
    LiveKitRejectedError,
    LiveKitResourceMissingError,
    LiveKitUnavailableError,
    LiveKitUnsupportedError,
)
from app.livekit.jobs import (
    apply_dispatch_rule_sync,
    apply_trunk_sync,
    enqueue_dispatch_rule_sync,
    enqueue_trunk_sync,
)
from app.livekit.sip import DispatchRuleSnapshot, SipResourceManager, TrunkSnapshot

__all__ = [
    "DispatchRuleSnapshot",
    "DriftFinding",
    "DriftKind",
    "DriftReport",
    "LiveKitAdminClient",
    "LiveKitError",
    "LiveKitRejectedError",
    "LiveKitResourceMissingError",
    "LiveKitUnavailableError",
    "LiveKitUnsupportedError",
    "RoomSnapshot",
    "SipResourceManager",
    "TrunkSnapshot",
    "apply_dispatch_rule_sync",
    "apply_trunk_sync",
    "detect_drift",
    "enqueue_dispatch_rule_sync",
    "enqueue_trunk_sync",
    "last_report",
]
