"""LiveKit integration.

A dedicated module (spec 80 rule 7). Nothing outside it should import the
LiveKit SDK or hold the LiveKit API secret.
"""

from app.livekit.client import LiveKitAdminClient
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
    "SipResourceManager",
    "TrunkSnapshot",
    "detect_drift",
    "last_report",
]
