"""Phase 6.6–6.7: knowledge ingest, documents API, agent assignment."""

from __future__ import annotations

import inspect

import pytest

from app.api.v1 import tools as tools_api
from app.schemas.tool import KnowledgeBaseCreate, WebDocumentCreate
from app.services import knowledge_ingest, seed as seed_mod
from tests.conftest import frontend_file


class TestSchemas:
    def test_create_accepts_embedding_provider(self) -> None:
        payload = KnowledgeBaseCreate.model_validate(
            {"name": "Policies", "embedding_provider_id": None}
        )
        assert payload.name == "Policies"

    def test_web_url_must_be_http(self) -> None:
        with pytest.raises(Exception):
            WebDocumentCreate.model_validate({"url": "ftp://example.com"})


class TestIngestContract:
    def test_ingest_extracts_chunks_and_embeds(self) -> None:
        src = inspect.getsource(knowledge_ingest.ingest_document)
        assert "split_chunks" in src or "chunk_text" in src
        assert "embed_texts" in src
        assert "KnowledgeChunk" in src

    def test_upload_and_web_are_audited(self) -> None:
        assert "audit.record" in inspect.getsource(tools_api.upload_document)
        assert "audit.record" in inspect.getsource(tools_api.add_web_document)
        assert "audit.record" in inspect.getsource(tools_api.delete_document)

    def test_upload_stores_in_object_storage(self) -> None:
        src = inspect.getsource(tools_api.upload_document)
        assert "object_store.put_object" in src
        assert "ingest_document" in src


class TestSeed:
    def test_seed_creates_hotel_policies(self) -> None:
        src = inspect.getsource(seed_mod)
        assert "Hotel policies" in src
        assert "harbour-1842" in src
        assert "_seed_hotel_knowledge" in src


class TestConsole:
    def test_knowledge_page_has_upload(self) -> None:
        path = frontend_file("app", "knowledge-bases", "page.tsx")
        if path is None:
            pytest.skip("services/frontend is not mounted in this container")
        src = path.read_text()
        assert "uploadDocument" in src
        assert "addWebDocument" in src
        assert "Ingestion is Phase 6" not in src

    def test_agent_builder_assigns_a_base(self) -> None:
        path = frontend_file("app", "agents", "page.tsx")
        if path is None:
            pytest.skip("services/frontend is not mounted in this container")
        src = path.read_text()
        assert "knowledge_base_id" in src
        assert "Assigned base" in src

    def test_nav_no_longer_says_no_ingest(self) -> None:
        path = frontend_file("components", "Shell.tsx")
        if path is None:
            pytest.skip("services/frontend is not mounted in this container")
        src = path.read_text()
        assert "no ingest" not in src
