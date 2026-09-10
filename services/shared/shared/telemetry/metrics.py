"""Prometheus metric definitions (spec 56, 57).

Metrics are declared once here so both planes emit the same names and label
sets. Voice-latency metrics (spec 56) use explicit buckets tuned for
conversational speech: anything above roughly two seconds is already a bad
call, so fine resolution below that matters more than a long tail.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.core import REGISTRY

CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

# Latency buckets in seconds, weighted toward the sub-second range that decides
# whether a conversation feels natural.
_VOICE_LATENCY_BUCKETS = (
    0.05,
    0.1,
    0.15,
    0.2,
    0.3,
    0.4,
    0.5,
    0.75,
    1.0,
    1.5,
    2.0,
    3.0,
    5.0,
    10.0,
)

# Tenant and agent labels let capacity and quality be sliced per tenant without
# joining against the database.
_CALL_LABELS = ("tenant_id", "agent_id")


def _registry(registry: CollectorRegistry | None) -> CollectorRegistry:
    return registry if registry is not None else REGISTRY


class VoiceMetrics:
    """Voice pipeline metrics, instantiated once per worker process.

    Accepts an explicit registry so tests can assert on a clean collector
    instead of the process-global one.
    """

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        reg = _registry(registry)

        # --- Latency (spec 56) ------------------------------------------- #
        self.stt_latency = Histogram(
            "voice_stt_latency_seconds",
            "Speech-to-text latency from end of caller speech to final transcript",
            (
                *_CALL_LABELS,
                "provider",
            ),
            buckets=_VOICE_LATENCY_BUCKETS,
            registry=reg,
        )
        self.llm_first_token_latency = Histogram(
            "voice_llm_first_token_latency_seconds",
            "LLM latency from prompt submission to first token",
            (*_CALL_LABELS, "provider", "model"),
            buckets=_VOICE_LATENCY_BUCKETS,
            registry=reg,
        )
        self.tts_first_audio_latency = Histogram(
            "voice_tts_first_audio_latency_seconds",
            "TTS latency from text submission to first audio frame",
            (
                *_CALL_LABELS,
                "provider",
            ),
            buckets=_VOICE_LATENCY_BUCKETS,
            registry=reg,
        )
        self.time_to_first_response = Histogram(
            "voice_time_to_first_response_seconds",
            "Time from end of caller speech to first LLM token",
            _CALL_LABELS,
            buckets=_VOICE_LATENCY_BUCKETS,
            registry=reg,
        )
        self.time_to_first_audio = Histogram(
            "voice_time_to_first_audio_seconds",
            "Time from end of caller speech to first audio frame sent to caller",
            _CALL_LABELS,
            buckets=_VOICE_LATENCY_BUCKETS,
            registry=reg,
        )
        self.end_to_end_latency = Histogram(
            "voice_end_to_end_response_latency_seconds",
            "Full turnaround from end of caller speech to end of agent utterance",
            _CALL_LABELS,
            buckets=_VOICE_LATENCY_BUCKETS,
            registry=reg,
        )

        # --- Calls and workers (spec 57) --------------------------------- #
        self.active_calls = Gauge(
            "voice_active_calls",
            "Calls currently in progress on this worker",
            ("tenant_id",),
            registry=reg,
        )
        self.calls_total = Counter(
            "voice_calls_total",
            "Calls handled, by final state",
            (
                *_CALL_LABELS,
                "state",
            ),
            registry=reg,
        )
        self.call_duration = Histogram(
            "voice_call_duration_seconds",
            "Call duration",
            _CALL_LABELS,
            buckets=(5, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600),
            registry=reg,
        )
        self.worker_utilization = Gauge(
            "voice_worker_utilization_ratio",
            "Active calls on this worker divided by its configured capacity",
            registry=reg,
        )
        self.interruptions_total = Counter(
            "voice_interruptions_total",
            "Barge-in events where caller speech cancelled in-flight TTS",
            _CALL_LABELS,
            registry=reg,
        )

        # --- Provider health (spec 55) ----------------------------------- #
        self.provider_requests_total = Counter(
            "voice_provider_requests_total",
            "Provider calls by outcome",
            ("kind", "provider", "outcome"),
            registry=reg,
        )
        self.provider_circuit_state = Gauge(
            "voice_provider_circuit_state",
            "Circuit breaker state per provider (0=closed, 1=half-open, 2=open)",
            ("kind", "provider"),
            registry=reg,
        )
        self.provider_fallbacks_total = Counter(
            "voice_provider_fallbacks_total",
            "Failovers from a primary provider to its fallback",
            ("kind", "primary", "fallback"),
            registry=reg,
        )

        # --- Tools and retrieval ----------------------------------------- #
        self.tool_calls_total = Counter(
            "voice_tool_calls_total",
            "Tool invocations by outcome",
            ("tenant_id", "tool", "outcome"),
            registry=reg,
        )
        self.tool_latency = Histogram(
            "voice_tool_latency_seconds",
            "Tool execution latency",
            ("tenant_id", "tool"),
            buckets=_VOICE_LATENCY_BUCKETS,
            registry=reg,
        )
        self.rag_latency = Histogram(
            "voice_rag_retrieval_latency_seconds",
            "Knowledge base retrieval latency",
            ("tenant_id",),
            buckets=_VOICE_LATENCY_BUCKETS,
            registry=reg,
        )


class ControlPlaneMetrics:
    """Configuration API metrics."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        reg = _registry(registry)

        self.http_requests_total = Counter(
            "api_http_requests_total",
            "HTTP requests by route, method and status",
            ("method", "route", "status"),
            registry=reg,
        )
        self.http_latency = Histogram(
            "api_http_request_duration_seconds",
            "HTTP request duration",
            ("method", "route"),
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
            registry=reg,
        )
        self.livekit_sync_total = Counter(
            "api_livekit_sync_total",
            "LiveKit resource synchronisation attempts by resource and outcome",
            ("resource", "outcome"),
            registry=reg,
        )
        self.livekit_drift_detected = Gauge(
            "api_livekit_drift_detected",
            "Resources currently in DRIFTED state, by resource type",
            ("resource",),
            registry=reg,
        )
        self.tenant_limit_rejections_total = Counter(
            "api_tenant_limit_rejections_total",
            "Calls rejected before acceptance because a tenant limit was reached",
            ("tenant_id", "limit"),
            registry=reg,
        )
        self.authz_denials_total = Counter(
            "api_authz_denials_total",
            "Requests denied by RBAC or tenant isolation",
            ("reason",),
            registry=reg,
        )


def render_metrics(registry: CollectorRegistry | None = None) -> bytes:
    """Serialise the registry in Prometheus exposition format."""
    return generate_latest(_registry(registry))
