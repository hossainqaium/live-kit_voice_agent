"""SIP Configuration Wizard coverage (spec 16, Plan 4b.1).

The wizard is frontend composition of existing APIs. These tests assert the
page exists, lists all ten PRD steps, and performs the two writes that are
easy to get wrong: creating the trunk, then re-syncing after the DID so
LiveKit accepts the number.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_WIZARD = (
    Path(__file__).parent.parent.parent.parent
    / "services"
    / "frontend"
    / "app"
    / "sip-wizard"
    / "page.tsx"
)
_SHELL = (
    Path(__file__).parent.parent.parent.parent
    / "services"
    / "frontend"
    / "components"
    / "Shell.tsx"
)

_TEN_STEPS = (
    "Select PBX",
    "SIP Configuration",
    "Authentication",
    "Codec",
    "Security",
    "Connection Test",
    "Phone Number",
    "AI Agent",
    "Routing",
    "Complete",
)


class TestSipWizardPage:
    def test_page_exists_or_skip(self) -> None:
        if not _WIZARD.exists():
            pytest.skip("services/frontend is not mounted in this container")
        assert _WIZARD.exists()

    def test_all_ten_steps_are_listed(self) -> None:
        if not _WIZARD.exists():
            pytest.skip("services/frontend is not mounted in this container")
        src = _WIZARD.read_text()
        missing = [name for name in _TEN_STEPS if name not in src]
        assert not missing, f"Wizard is missing PRD steps: {missing}"

    def test_creates_trunk_through_existing_api(self) -> None:
        if not _WIZARD.exists():
            pytest.skip("services/frontend is not mounted in this container")
        src = _WIZARD.read_text()
        assert "sipTrunks.create" in src

    def test_resyncs_trunk_after_did_assignment(self) -> None:
        """Phone-number create does not push accepted numbers to LiveKit."""
        if not _WIZARD.exists():
            pytest.skip("services/frontend is not mounted in this container")
        src = _WIZARD.read_text()
        assert "phoneNumbers.create" in src
        assert "sipTrunks.sync" in src

    def test_exposes_codecs_direction_and_srtp(self) -> None:
        """These are on the API but not on the standalone trunk form."""
        if not _WIZARD.exists():
            pytest.skip("services/frontend is not mounted in this container")
        src = _WIZARD.read_text()
        assert "codecs" in src
        assert "media_encryption_required" in src
        assert "direction" in src


class TestWizardNav:
    def test_shell_links_the_wizard(self) -> None:
        if not _SHELL.exists():
            pytest.skip("services/frontend is not mounted in this container")
        src = _SHELL.read_text()
        assert "/sip-wizard" in src
        assert "SIP Setup Wizard" in src
