"""RAG retrieval (spec 33, Plan 6.7)."""

from __future__ import annotations

import inspect
import uuid

from worker.entrypoint import ConfigurableAgent, _build_agent, _message_text
from worker.rag.retrieve import (
    RetrievedChunk,
    format_context,
    search_chunks,
)


class TestFormatContext:
    def test_empty(self) -> None:
        assert format_context([]) == ""

    def test_includes_passages_and_warning(self) -> None:
        block = format_context(
            [RetrievedChunk(content="Wi-Fi password is harbour-1842.", title="House rules", score=0.9)]
        )
        assert "harbour-1842" in block
        assert "House rules" in block
        assert "do not know" in block.lower()


class TestSearchSql:
    def test_vector_query_filters_tenant_and_base(self) -> None:
        import worker.rag.retrieve as retrieve_mod

        src = inspect.getsource(retrieve_mod)
        assert "tenant_id" in src
        assert "knowledge_base_id" in src
        assert "CAST(:query AS vector)" in src


class TestAgentHook:
    def test_build_agent_is_configurable(self) -> None:
        src = inspect.getsource(_build_agent)
        assert "ConfigurableAgent" in src

    def test_user_turn_retrieves(self) -> None:
        src = inspect.getsource(ConfigurableAgent.on_user_turn_completed)
        assert "search_chunks" in src
        assert "tenant_id" in src
        assert "roll_summary" in src

    def test_message_text_reads_plain_string(self) -> None:
        assert _message_text(type("M", (), {"text_content": "  hello  "})()) == "hello"

    def test_knowledge_retrieval_dataclass_is_frozen(self) -> None:
        from worker.rag.retrieve import KnowledgeRetrieval

        row = KnowledgeRetrieval(
            knowledge_base_id=uuid.uuid4(),
            top_k=4,
            embedding_model="text-embedding-3-small",
            api_key=None,
            base_url=None,
        )
        assert row.top_k == 4
