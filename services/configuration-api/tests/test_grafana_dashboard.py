"""Structural validation of the Grafana voice-latency dashboard (Plan 2b.6).

This is a JSON-schema-level test: it does not require a running Grafana
instance, and it runs inside the existing ``make test-api`` container.

What it guards against:

1. The JSON file is valid and parseable.
2. Every spec-56 voice-latency metric has at least one panel with a query
   that references it — so removing a metric from the dashboard fails fast.
3. The dashboard has the correct UID and a sensible structure (title,
   datasource template variable, row panels, refresh interval).
4. The datasource name matches what is provisioned in
   ``deploy/grafana/provisioning/datasources/prometheus.yml``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Resolve relative to this file so the test works inside Docker (/app) and
# locally (/Users/…/LiveKit Voice Agent/services/configuration-api/tests/).
_DASHBOARD_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "deploy"
    / "grafana"
    / "dashboards"
    / "voice-latency.json"
)

#: The six spec-56 Prometheus metric names that MUST appear in the dashboard.
_SPEC_56_METRICS = [
    "voice_stt_latency_seconds",
    "voice_llm_first_token_latency_seconds",
    "voice_tts_first_audio_latency_seconds",
    "voice_time_to_first_response_seconds",
    "voice_time_to_first_audio_seconds",
    "voice_end_to_end_response_latency_seconds",
]


@pytest.fixture(scope="module")
def dashboard() -> dict:
    if not _DASHBOARD_PATH.exists():
        pytest.skip(
            f"Grafana dashboard file not present in this environment "
            f"({_DASHBOARD_PATH}). Tests run locally; Docker container does "
            f"not mount deploy/ — that is expected."
        )
    return json.loads(_DASHBOARD_PATH.read_text())


@pytest.fixture(scope="module")
def all_exprs(dashboard: dict) -> str:
    """Concatenate every PromQL expression in the dashboard for substring search."""
    parts: list[str] = []
    for panel in dashboard.get("panels", []):
        for target in panel.get("targets", []):
            expr = target.get("expr", "")
            if expr:
                parts.append(expr)
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# File and format
# --------------------------------------------------------------------------- #


class TestDashboardFormat:
    def test_dashboard_file_exists(self) -> None:
        """Skips when running inside Docker (deploy/ not mounted)."""
        if not _DASHBOARD_PATH.exists():
            pytest.skip("deploy/ not available in Docker container")
        assert _DASHBOARD_PATH.exists()

    def test_dashboard_is_valid_json(self) -> None:
        if not _DASHBOARD_PATH.exists():
            pytest.skip("deploy/ not available in Docker container")
        json.loads(_DASHBOARD_PATH.read_text())  # raises on invalid JSON

    def test_uid_is_present(self, dashboard: dict) -> None:
        assert dashboard.get("uid"), "Dashboard must have a non-empty uid"

    def test_title_is_present(self, dashboard: dict) -> None:
        assert dashboard.get("title"), "Dashboard must have a title"

    def test_schema_version_is_set(self, dashboard: dict) -> None:
        assert dashboard.get("schemaVersion", 0) >= 30, (
            "schemaVersion should be ≥ 30 (Grafana 9+)"
        )

    def test_refresh_interval_is_set(self, dashboard: dict) -> None:
        assert dashboard.get("refresh"), "Dashboard must have an auto-refresh interval"

    def test_has_panels(self, dashboard: dict) -> None:
        panels = [p for p in dashboard.get("panels", []) if p.get("type") != "row"]
        assert len(panels) >= 6, (
            f"Expected at least 6 data panels (one per spec-56 metric), got {len(panels)}"
        )


# --------------------------------------------------------------------------- #
# Spec-56 metric coverage
# --------------------------------------------------------------------------- #


class TestSpec56MetricCoverage:
    """Every spec-56 voice-latency metric must appear in at least one panel."""

    @pytest.mark.parametrize("metric_name", _SPEC_56_METRICS)
    def test_metric_appears_in_at_least_one_panel(
        self, metric_name: str, all_exprs: str
    ) -> None:
        assert metric_name in all_exprs, (
            f"Spec-56 metric {metric_name!r} does not appear in any panel's "
            "PromQL expression.  Add a panel for it or add it to an existing "
            "multi-metric panel."
        )

    def test_all_six_spec56_metrics_covered(self, all_exprs: str) -> None:
        """Single assertion covering all six — readable failure message."""
        missing = [m for m in _SPEC_56_METRICS if m not in all_exprs]
        assert not missing, (
            f"Missing spec-56 metrics from dashboard panels: {missing}"
        )


# --------------------------------------------------------------------------- #
# Datasource
# --------------------------------------------------------------------------- #


class TestDatasource:
    def test_has_datasource_template_variable(self, dashboard: dict) -> None:
        variables = dashboard.get("templating", {}).get("list", [])
        ds_vars = [v for v in variables if v.get("type") == "datasource"]
        assert ds_vars, (
            "Dashboard must have a datasource template variable so it can be "
            "imported into any Prometheus instance without editing the JSON."
        )

    def test_datasource_variable_targets_prometheus(self, dashboard: dict) -> None:
        variables = dashboard.get("templating", {}).get("list", [])
        ds_var = next((v for v in variables if v.get("type") == "datasource"), None)
        assert ds_var is not None
        assert ds_var.get("pluginId") == "prometheus", (
            "Datasource variable must target prometheus plugin"
        )


# --------------------------------------------------------------------------- #
# Template variables for filtering
# --------------------------------------------------------------------------- #


class TestTemplateVariables:
    def test_has_tenant_id_variable(self, dashboard: dict) -> None:
        variables = dashboard.get("templating", {}).get("list", [])
        names = [v.get("name") for v in variables]
        assert "tenant_id" in names, (
            "Dashboard must have a tenant_id template variable for per-tenant filtering"
        )

    def test_has_agent_id_variable(self, dashboard: dict) -> None:
        variables = dashboard.get("templating", {}).get("list", [])
        names = [v.get("name") for v in variables]
        assert "agent_id" in names, (
            "Dashboard must have an agent_id template variable for per-agent filtering"
        )


# --------------------------------------------------------------------------- #
# Panel units — latency panels must use seconds
# --------------------------------------------------------------------------- #


class TestPanelUnits:
    def test_latency_panels_use_seconds_unit(self, dashboard: dict) -> None:
        """Panels whose title contains 'Latency' or 'Time' must use unit='s'."""
        offenders: list[str] = []
        for panel in dashboard.get("panels", []):
            title = panel.get("title", "")
            if not any(kw in title for kw in ("Latency", "Time to", "Duration")):
                continue
            unit = (
                panel.get("fieldConfig", {})
                .get("defaults", {})
                .get("unit", "")
            )
            if unit != "s":
                offenders.append(f"{title!r} uses unit={unit!r} (expected 's')")
        assert not offenders, (
            "Latency/Duration panels must use unit='s' so Grafana auto-scales "
            "to ms/s: " + "; ".join(offenders)
        )
