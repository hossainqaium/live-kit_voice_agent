"""Plan 6.9–6.10c: transfer builder fields, seed destination, tool description."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.conftest import frontend_file


class TestConsole:
    def test_agent_builder_saves_whisper_fields(self) -> None:
        path = frontend_file("app", "agents", "page.tsx")
        if path is None:
            pytest.skip("services/frontend is not mounted in this container")
        src = path.read_text()
        assert "transfer_summary_template" in src
        assert "transfer_summary_max_seconds" in src
        assert "transfer_skip_dtmf" in src
        assert "{{customer}}" in src
        assert "{{required_next_action}}" in src
        assert "DTMF skip key" in src

    def test_transfer_destinations_page_exists(self) -> None:
        path = frontend_file("app", "transfer-destinations", "page.tsx")
        if path is None:
            pytest.skip("services/frontend is not mounted in this container")
        src = path.read_text()
        assert "whisper_summary" in src or "Whisper" in src


class TestSeed:
    def test_transfer_call_is_no_longer_a_phase_placeholder(self) -> None:
        root = Path(__file__).resolve().parents[1]
        src = (root / "app" / "services" / "seed.py").read_text()
        assert "Warm-transfer this call to a human agent" in src
        assert "Not available until Phase 6.9." not in src.split("_BUILTIN_TOOLS")[1].split(
            "refund_order"
        )[0]
        assert "destination" in src
        assert 'name="Reception"' in src or 'name == "Reception"' in src
        assert "transfer_call" in src
        assert "PBX_EXTENSION" in src


class TestAgentSchema:
    def test_version_schema_includes_whisper_controls(self) -> None:
        root = Path(__file__).resolve().parents[1]
        src = (root / "app" / "schemas" / "agent.py").read_text()
        tree = ast.parse(src)
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
        assert "transfer_summary_template" in names
        assert "transfer_summary_max_seconds" in names
        assert "transfer_skip_dtmf" in names
