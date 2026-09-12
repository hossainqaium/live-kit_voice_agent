"""Simple ticketing UI and seed (Phase 6.0)."""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from app.api.v1 import tickets as tickets_api
from app.db.models import TENANT_OWNED_TABLES
from app.db.models.ticket import Ticket
from app.schemas.ticket import TicketCreate, TicketUpdate
from app.services import seed as seed_mod
from tests.conftest import frontend_file


class TestSchema:
    def test_tickets_are_tenant_owned(self) -> None:
        assert "tickets" in TENANT_OWNED_TABLES
        assert Ticket.__tablename__ == "tickets"

    def test_create_requires_a_title(self) -> None:
        with pytest.raises(ValidationError):
            TicketCreate.model_validate({"title": ""})

    def test_create_defaults_priority(self) -> None:
        payload = TicketCreate.model_validate({"title": "AC not cooling"})
        assert payload.priority.value == "NORMAL"

    def test_update_forbids_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            TicketUpdate.model_validate({"tenant_id": "x"})


class TestRoutes:
    async def test_openapi_lists_tickets(self, client) -> None:
        paths = (await client.get("/openapi.json")).json()["paths"]
        assert "/api/v1/tickets" in paths
        assert "/api/v1/tickets/{ticket_id}" in paths

    async def test_writes_require_auth(self, client) -> None:
        response = await client.post("/api/v1/tickets", json={"title": "x"})
        assert response.status_code == 401

    def test_create_is_audited(self) -> None:
        src = inspect.getsource(tickets_api.create_ticket)
        assert "audit.record" in src
        assert "ticket.created" in src

    def test_create_assigns_a_ticket_number(self) -> None:
        src = inspect.getsource(tickets_api.create_ticket)
        assert "next_ticket_number" in src
        assert "MANUAL" in src


class TestSeed:
    def test_seed_creates_server_agent(self) -> None:
        src = inspect.getsource(seed_mod.seed_dev_tenant)
        assert 'name == "Server Agent"' in src or 'name="Server Agent"' in src
        assert "Files support tickets" in src or "file a support ticket" in src

    def test_seed_creates_one_ticket(self) -> None:
        src = inspect.getsource(seed_mod.seed_dev_tenant)
        assert "TCK-0001" in src
        assert "Lobby access card reader offline" in src


class TestConsole:
    def test_page_exists_or_skip(self) -> None:
        path = frontend_file("app", "tickets", "page.tsx")
        if path is None:
            pytest.skip("services/frontend is not mounted in this container")
        src = path.read_text()
        assert "tickets.create" in src
        assert "Server Agent" in src
        assert "Create a ticket" in src

    def test_nav_lists_tickets(self) -> None:
        path = frontend_file("components", "Shell.tsx")
        if path is None:
            pytest.skip("services/frontend is not mounted in this container")
        src = path.read_text()
        assert "/tickets" in src
        assert "Tickets" in src
