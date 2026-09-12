"""Standalone SIP trunk form covers every spec-15 field (Plan 5.2).

The wizard already offered direction, codecs, DTMF and SRTP. The trunk
dialog did not, so a tenant who skipped the wizard could not set them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_PAGE = (
    Path(__file__).parent.parent.parent.parent
    / "services"
    / "frontend"
    / "app"
    / "sip-trunks"
    / "page.tsx"
)


class TestTrunkFormOffersSpec15Fields:
    def test_page_exists_or_skip(self) -> None:
        if not _PAGE.exists():
            pytest.skip("services/frontend is not mounted in this container")

    def test_direction_codecs_dtmf_and_srtp(self) -> None:
        if not _PAGE.exists():
            pytest.skip("services/frontend is not mounted in this container")
        src = _PAGE.read_text()
        assert "direction" in src
        assert "INBOUND" in src
        assert "BIDIRECTIONAL" in src
        assert "PCMU" in src
        assert "dtmf_mode" in src
        assert "RFC2833" in src
        assert "media_encryption_required" in src
        assert "Require SRTP" in src
