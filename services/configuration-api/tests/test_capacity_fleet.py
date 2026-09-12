"""Capacity dashboard fleet probes (spec 48, Plan 5.10)."""

from __future__ import annotations

import pytest

from app.schemas.platform import CapacityResponse
from app.services.fleet import (
    _count_nodes,
    _sip_node_count,
    _WorkerReady,
    gauge_value,
    parse_prometheus_text,
    unique_label_count,
)
from tests.conftest import frontend_file

_PROM = """
# HELP process_resident_memory_bytes Resident memory size in bytes.
# TYPE process_resident_memory_bytes gauge
process_resident_memory_bytes 104857600
# TYPE voice_worker_utilization_ratio gauge
voice_worker_utilization_ratio 0.3
# TYPE livekit_node_available gauge
livekit_node_available{node_id="NK_a"} 1
livekit_node_available{node_id="NK_b"} 1
# TYPE voice_provider_circuit_state gauge
voice_provider_circuit_state{kind="llm",provider="openai"} 2
"""


class TestPrometheusParser:
    def test_skips_comments_and_reads_labels(self) -> None:
        samples = parse_prometheus_text(_PROM)
        assert gauge_value(samples, "voice_worker_utilization_ratio") == 0.3
        assert unique_label_count(samples, "livekit_node_available", "node_id") == 2
        memory = gauge_value(samples, "process_resident_memory_bytes")
        assert memory == 104857600

    def test_node_count_uses_node_id_labels(self) -> None:
        samples = parse_prometheus_text(_PROM)
        assert _count_nodes(samples, reachable=True, fallback=1) == 2

    def test_node_count_falls_back_when_metrics_missing(self) -> None:
        assert _count_nodes([], reachable=True, fallback=1) == 1
        assert _count_nodes([], reachable=False, fallback=0) == 0

    def test_sip_nodes_are_one_when_uri_is_configured(self) -> None:
        assert _sip_node_count([], sip_uri="sip:livekit-sip:5060") == 1
        assert _sip_node_count([], sip_uri="") == 0


class TestUtilization:
    def test_ready_ratio(self) -> None:
        ready = _WorkerReady(responding=1, active_calls=3, capacity=10, utilization=0.3)
        assert ready.utilization == pytest.approx(0.3)


class TestCapacitySchema:
    def test_spec48_fields_are_present(self) -> None:
        required = {
            "total_capacity",
            "available_capacity",
            "livekit_nodes",
            "sip_nodes",
            "ai_workers",
            "worker_utilization",
            "cpu",
            "memory",
            "network",
            "providers",
        }
        assert required <= set(CapacityResponse.model_fields)

    async def test_openapi_documents_the_fields(self, client) -> None:
        spec = (await client.get("/openapi.json")).json()
        props = spec["components"]["schemas"]["CapacityResponse"]["properties"]
        for name in (
            "total_capacity",
            "available_capacity",
            "livekit_nodes",
            "sip_nodes",
            "ai_workers",
            "worker_utilization",
            "providers",
        ):
            assert name in props


class TestCapacityPage:
    def test_page_names_spec48_cards(self) -> None:
        path = frontend_file("app", "platform", "capacity", "page.tsx")
        if path is None:
            pytest.skip("services/frontend is not mounted in this container")
        src = path.read_text()
        for label in (
            "Total Capacity",
            "Available Capacity",
            "LiveKit Nodes",
            "SIP Nodes",
            "AI Workers",
            "Worker Utilization",
            "Provider Health",
            "CPU",
            "Memory",
            "Network",
        ):
            assert label in src, label
        assert "autoscaling signal is Phase 5" not in src
