"""LiveKit administration section (spec 11, Plan 5.1).

The thirteen topics are reported here. Tenant configuration is linked; cluster
topology is not editable. Rooms are a live read, never written from the UI.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from app.api.v1 import platform as platform_api
from app.schemas.platform import LiveKitOverview

_LIVEKIT_PAGE = (
    Path(__file__).parent.parent.parent.parent
    / "services"
    / "frontend"
    / "app"
    / "platform"
    / "livekit"
    / "page.tsx"
)

_SPEC11 = (
    "clusters",
    "sip_configuration",
    "sip_trunks",
    "dispatch_rules",
    "agent_dispatch",
    "rooms",
    "participants",
    "media",
    "codecs",
    "recording_egress",
    "turn_ice",
    "health",
    "metrics",
)


class TestAdminSections:
    def test_all_thirteen_topics_are_present(self) -> None:
        sections = platform_api._livekit_admin_sections(
            trunk_count=1,
            rule_count=1,
            room_count=0,
            participant_count=0,
            dispatch_names=["voice-agent"],
        )
        assert [item.id for item in sections] == list(_SPEC11)

    def test_infra_topics_are_not_tenant_editable(self) -> None:
        sections = {
            item.id: item
            for item in platform_api._livekit_admin_sections(
                trunk_count=0,
                rule_count=0,
                room_count=0,
                participant_count=0,
                dispatch_names=[],
            )
        }
        for topic in ("clusters", "media", "turn_ice"):
            assert sections[topic].scope == "infra"
            assert sections[topic].href == "/platform/infrastructure"

    def test_overview_schema_carries_rooms_and_sections(self) -> None:
        fields = LiveKitOverview.model_fields
        assert "sections" in fields
        assert "rooms" in fields
        assert "agent_dispatch_names" in fields

    def test_overview_lists_rooms_from_one_probe(self) -> None:
        src = inspect.getsource(platform_api.livekit_overview)
        assert "list_rooms" in inspect.getsource(platform_api._probe_livekit)
        assert "sections=" in src
        assert "rooms=" in src


class TestConsole:
    def test_page_renders_the_section_table(self) -> None:
        if not _LIVEKIT_PAGE.exists():
            import pytest

            pytest.skip("services/frontend is not mounted in this container")
        src = _LIVEKIT_PAGE.read_text()
        assert "Administration" in src
        assert "data.sections" in src
        assert "data.rooms" in src
