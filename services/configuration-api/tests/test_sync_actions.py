"""Synchronize / Retry / Repair appear on tenant and platform screens (Plan 5.6)."""

from __future__ import annotations

import pytest

from tests.conftest import frontend_file


class TestConsoleActions:
    def test_sip_trunks_offers_the_three_actions(self) -> None:
        path = frontend_file("app", "sip-trunks", "page.tsx")
        if path is None:
            pytest.skip("services/frontend is not mounted in this container")
        src = path.read_text()
        assert "Synchronize" in src
        assert "Retry" in src
        assert "Repair" in src
        assert "sipTrunks.repair" in src
        assert "running in the background" in src

    def test_dispatch_rules_offers_the_three_actions(self) -> None:
        path = frontend_file("app", "dispatch-rules", "page.tsx")
        if path is None:
            pytest.skip("services/frontend is not mounted in this container")
        src = path.read_text()
        assert "Synchronize" in src
        assert "dispatchRules.repair" in src

    def test_platform_livekit_acts_per_row(self) -> None:
        path = frontend_file("app", "platform", "livekit", "page.tsx")
        if path is None:
            pytest.skip("services/frontend is not mounted in this container")
        src = path.read_text()
        assert "syncLivekitResource" in src
        assert "Repair" in src
        assert "Synchronize" in src
        assert "platform-wide re-sync" not in src
