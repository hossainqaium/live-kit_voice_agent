"""Enumerations shared by the Control Plane and the Voice Execution Plane.

These are contract values: they appear in the database, on the API, in logs and
in the frontend. They live here so the two planes cannot drift apart.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """String-valued enum that serialises to its value."""

    def __str__(self) -> str:
        return self.value


# --------------------------------------------------------------------------- #
# Calls
# --------------------------------------------------------------------------- #


class CallState(StrEnum):
    """Call state machine (spec 42)."""

    NEW = "NEW"
    RINGING = "RINGING"
    ANSWERED = "ANSWERED"
    AI_CONNECTED = "AI_CONNECTED"
    IN_PROGRESS = "IN_PROGRESS"
    TRANSFERRING = "TRANSFERRING"
    HUMAN_AGENT = "HUMAN_AGENT"
    COMPLETED = "COMPLETED"
    # Terminal failure states.
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    BUSY = "BUSY"
    NO_ANSWER = "NO_ANSWER"


#: States from which no further transition is legal.
TERMINAL_CALL_STATES = frozenset(
    {
        CallState.COMPLETED,
        CallState.FAILED,
        CallState.TIMEOUT,
        CallState.CANCELLED,
        CallState.BUSY,
        CallState.NO_ANSWER,
    }
)

#: Legal transitions. Enforced by the call service rather than the database so
#: that an illegal transition is a loud application error, not a silent write.
CALL_STATE_TRANSITIONS: dict[CallState, frozenset[CallState]] = {
    CallState.NEW: frozenset(
        {CallState.RINGING, CallState.FAILED, CallState.CANCELLED, CallState.BUSY}
    ),
    CallState.RINGING: frozenset(
        {
            CallState.ANSWERED,
            CallState.NO_ANSWER,
            CallState.BUSY,
            CallState.CANCELLED,
            CallState.FAILED,
            CallState.TIMEOUT,
        }
    ),
    CallState.ANSWERED: frozenset(
        {CallState.AI_CONNECTED, CallState.FAILED, CallState.COMPLETED, CallState.TIMEOUT}
    ),
    CallState.AI_CONNECTED: frozenset(
        {CallState.IN_PROGRESS, CallState.FAILED, CallState.COMPLETED, CallState.TIMEOUT}
    ),
    CallState.IN_PROGRESS: frozenset(
        {
            CallState.TRANSFERRING,
            CallState.COMPLETED,
            CallState.FAILED,
            CallState.TIMEOUT,
        }
    ),
    CallState.TRANSFERRING: frozenset(
        {CallState.HUMAN_AGENT, CallState.COMPLETED, CallState.FAILED, CallState.TIMEOUT}
    ),
    CallState.HUMAN_AGENT: frozenset({CallState.COMPLETED, CallState.FAILED}),
}


class CallDirection(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class HangupReason(StrEnum):
    CALLER_HANGUP = "CALLER_HANGUP"
    AGENT_HANGUP = "AGENT_HANGUP"
    TRANSFERRED = "TRANSFERRED"
    MAX_DURATION = "MAX_DURATION"
    SILENCE_TIMEOUT = "SILENCE_TIMEOUT"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    SYSTEM_ERROR = "SYSTEM_ERROR"


class SpeakerType(StrEnum):
    """Transcript speaker types (spec 40)."""

    CALLER = "CALLER"
    AI = "AI"
    HUMAN_AGENT = "HUMAN_AGENT"


# --------------------------------------------------------------------------- #
# LiveKit resource synchronisation
# --------------------------------------------------------------------------- #


class SyncStatus(StrEnum):
    """Synchronisation state of a mirrored LiveKit resource (spec 12, 46)."""

    SYNCED = "SYNCED"
    PENDING = "PENDING"
    FAILED = "FAILED"
    DRIFTED = "DRIFTED"


# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #


class AgentVersionState(StrEnum):
    """Agent version lifecycle (spec 19)."""

    DRAFT = "DRAFT"
    TESTING = "TESTING"
    PUBLISHED = "PUBLISHED"
    ARCHIVED = "ARCHIVED"


class ResourceStatus(StrEnum):
    """Generic enable/disable status used by PBXs, trunks, DIDs, voices."""

    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    ARCHIVED = "ARCHIVED"


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #


class ProviderKind(StrEnum):
    STT = "STT"
    LLM = "LLM"
    TTS = "TTS"


# --------------------------------------------------------------------------- #
# Telephony
# --------------------------------------------------------------------------- #


class SipTransport(StrEnum):
    UDP = "UDP"
    TCP = "TCP"
    TLS = "TLS"


class TrunkDirection(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"
    BIDIRECTIONAL = "BIDIRECTIONAL"


class TransferDestinationKind(StrEnum):
    """Human transfer destinations (spec 35)."""

    SIP_EXTENSION = "SIP_EXTENSION"
    PBX_EXTENSION = "PBX_EXTENSION"
    PBX_QUEUE = "PBX_QUEUE"
    EXTERNAL_NUMBER = "EXTERNAL_NUMBER"
    SIP_URI = "SIP_URI"


class TransferStatus(StrEnum):
    """Warm transfer progress (spec 41 ``transfer_status``, clarification CR-1).

    The platform performs an attended transfer back to the PBX: the caller hears
    an announcement while the human agent is dialled and whispered the AI
    conversation summary, and the legs are bridged only afterwards.

    The top-level call state stays ``TRANSFERRING`` for this whole sequence and
    becomes ``HUMAN_AGENT`` on bridge, because spec 42 fixes the state list.
    The step detail belongs here instead.
    """

    NOT_REQUESTED = "NOT_REQUESTED"
    REQUESTED = "REQUESTED"
    #: Caller is hearing the transfer announcement.
    ANNOUNCING = "ANNOUNCING"
    #: The human agent leg is being dialled through the PBX.
    DIALING_AGENT = "DIALING_AGENT"
    #: Summary playing into the agent leg only. The caller must not hear this.
    WHISPERING_SUMMARY = "WHISPERING_SUMMARY"
    #: Caller and human agent connected; the AI has left the conversation.
    BRIDGED = "BRIDGED"
    AGENT_NO_ANSWER = "AGENT_NO_ANSWER"
    AGENT_BUSY = "AGENT_BUSY"
    AGENT_REJECTED = "AGENT_REJECTED"
    FAILED = "FAILED"
    #: The caller hung up mid-transfer.
    ABANDONED = "ABANDONED"


#: Outcomes that must divert the caller into the configured fallback chain
#: (spec 38). Reaching any of these without a fallback means the caller was
#: dropped, which CR-1 forbids.
TRANSFER_FALLBACK_TRIGGERS = frozenset(
    {
        TransferStatus.AGENT_NO_ANSWER,
        TransferStatus.AGENT_BUSY,
        TransferStatus.AGENT_REJECTED,
        TransferStatus.FAILED,
    }
)

#: Legal warm-transfer transitions, enforced by the transfer service.
TRANSFER_STATUS_TRANSITIONS: dict[TransferStatus, frozenset[TransferStatus]] = {
    TransferStatus.NOT_REQUESTED: frozenset({TransferStatus.REQUESTED}),
    TransferStatus.REQUESTED: frozenset(
        {TransferStatus.ANNOUNCING, TransferStatus.FAILED, TransferStatus.ABANDONED}
    ),
    # The caller announcement and the agent dial start together, so ANNOUNCING
    # can move straight on while announcement audio is still playing.
    TransferStatus.ANNOUNCING: frozenset(
        {TransferStatus.DIALING_AGENT, TransferStatus.FAILED, TransferStatus.ABANDONED}
    ),
    TransferStatus.DIALING_AGENT: frozenset(
        {
            TransferStatus.WHISPERING_SUMMARY,
            TransferStatus.AGENT_NO_ANSWER,
            TransferStatus.AGENT_BUSY,
            TransferStatus.AGENT_REJECTED,
            TransferStatus.FAILED,
            TransferStatus.ABANDONED,
        }
    ),
    # An agent may hang up during the whisper, which counts as a rejection.
    TransferStatus.WHISPERING_SUMMARY: frozenset(
        {
            TransferStatus.BRIDGED,
            TransferStatus.AGENT_REJECTED,
            TransferStatus.FAILED,
            TransferStatus.ABANDONED,
        }
    ),
}


class TransferSummaryDelivery(StrEnum):
    """How a transfer summary reaches the receiving human agent (CR-1).

    ``SPOKEN`` is the guaranteed path because it works with any SIP-compatible
    PBX. ``STRUCTURED`` is best-effort and must never block a transfer.
    """

    SPOKEN = "SPOKEN"
    STRUCTURED = "STRUCTURED"


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #


class PlatformRole(StrEnum):
    """Platform-scoped roles (spec 8)."""

    SUPER_ADMIN = "SUPER_ADMIN"
    PLATFORM_OPERATOR = "PLATFORM_OPERATOR"


class TenantRole(StrEnum):
    """Tenant-scoped roles (spec 8)."""

    TENANT_ADMIN = "TENANT_ADMIN"
    MANAGER = "MANAGER"
    AGENT_MANAGER = "AGENT_MANAGER"
    ANALYST = "ANALYST"
    VIEWER = "VIEWER"


class Permission(StrEnum):
    """Granular permissions (spec 8).

    The backend enforces these independently of the frontend.
    """

    AGENTS_READ = "agents.read"
    AGENTS_WRITE = "agents.write"
    AGENTS_PUBLISH = "agents.publish"
    PBXS_READ = "pbxs.read"
    PBXS_WRITE = "pbxs.write"
    SIP_TRUNKS_READ = "sip_trunks.read"
    SIP_TRUNKS_WRITE = "sip_trunks.write"
    CALLS_READ = "calls.read"
    RECORDINGS_READ = "recordings.read"
    ANALYTICS_READ = "analytics.read"
    USERS_MANAGE = "users.manage"
    BILLING_READ = "billing.read"


#: Tenant role to permission mapping. Platform roles are handled separately
#: because they are not scoped to a single tenant.
TENANT_ROLE_PERMISSIONS: dict[TenantRole, frozenset[Permission]] = {
    TenantRole.TENANT_ADMIN: frozenset(Permission),
    TenantRole.MANAGER: frozenset(
        {
            Permission.AGENTS_READ,
            Permission.AGENTS_WRITE,
            Permission.AGENTS_PUBLISH,
            Permission.PBXS_READ,
            Permission.PBXS_WRITE,
            Permission.SIP_TRUNKS_READ,
            Permission.SIP_TRUNKS_WRITE,
            Permission.CALLS_READ,
            Permission.RECORDINGS_READ,
            Permission.ANALYTICS_READ,
        }
    ),
    TenantRole.AGENT_MANAGER: frozenset(
        {
            Permission.AGENTS_READ,
            Permission.AGENTS_WRITE,
            Permission.AGENTS_PUBLISH,
            Permission.PBXS_READ,
            Permission.SIP_TRUNKS_READ,
            Permission.CALLS_READ,
            Permission.ANALYTICS_READ,
        }
    ),
    TenantRole.ANALYST: frozenset(
        {
            Permission.AGENTS_READ,
            Permission.CALLS_READ,
            Permission.RECORDINGS_READ,
            Permission.ANALYTICS_READ,
        }
    ),
    TenantRole.VIEWER: frozenset(
        {
            Permission.AGENTS_READ,
            Permission.PBXS_READ,
            Permission.SIP_TRUNKS_READ,
            Permission.CALLS_READ,
        }
    ),
}
