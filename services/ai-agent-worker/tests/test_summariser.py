"""Post-call summarisation tests (spec 34 — Plan 2b.5).

Three areas are tested:

1. ``_build_full_text`` — assembles the dialogue string correctly.
2. ``generate_summary`` — reads segments, skips when too few, calls the LLM,
   handles NO_SUMMARY, and uses the tenant's custom template when set.
3. ``write_transcript_summary`` — SQL coverage (right columns, right table).
4. ``CallObserver`` properties — ``transcript_row_id`` and ``segments_written``
   are readable after start().
"""

from __future__ import annotations

import inspect
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.models import ProviderKind
from worker.config_loader import (
    CallContext,
    CallPolicy,
    TransferPolicy,
    write_transcript_summary,
    _WRITE_SUMMARY_SQL,
)
from worker.providers.base import ProviderConfig
from worker.summariser import (
    _DEFAULT_SYSTEM_PROMPT,
    _MIN_SEGMENTS,
    _build_full_text,
    generate_summary,
)


# --------------------------------------------------------------------------- #
# Shared fixtures
# --------------------------------------------------------------------------- #


def _provider(kind: ProviderKind) -> ProviderConfig:
    return ProviderConfig(
        kind=kind,
        provider="openai_compatible",
        model="gpt-4o-mini",
        api_key="test-key",
        base_url="http://llm.local",
    )


def _policy(**kwargs) -> CallPolicy:
    defaults = {
        "recording_enabled": False,
        "interruption_enabled": False,
        "interruption_min_words": 1,
        "silence_timeout_seconds": None,
        "max_call_duration_seconds": None,
        "transcription_enabled": True,
    }
    defaults.update(kwargs)
    return CallPolicy(**defaults)


def _context(*, summary_template: str | None = None) -> CallContext:
    return CallContext(
        call_id="test-call-summariser",
        call_row_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        tenant_slug="acme",
        agent_id=uuid.uuid4(),
        agent_version_id=uuid.uuid4(),
        agent_name="test-agent",
        version_number=1,
        room_name="test-room",
        did="1001",
        caller_number="+15550001111",
        sip_trunk_id=uuid.uuid4(),
        pbx_id=uuid.uuid4(),
        language="en",
        greeting=None,
        system_prompt="You are a helpful assistant.",
        stt=_provider(ProviderKind.STT),
        llm=_provider(ProviderKind.LLM),
        tts=_provider(ProviderKind.TTS),
        call_policy=_policy(),
        transfer_policy=TransferPolicy(
            enabled=False,
            announcement_text=None,
            hold_media_object_key=None,
            summary_template=summary_template,
            summary_max_seconds=30,
            skip_dtmf=None,
        ),
    )


def _fake_row(speaker: str, text: str) -> Any:
    row = MagicMock()
    row.speaker = speaker
    row.text = text
    return row


def _make_factory(rows: list) -> Any:
    """Return a mock session factory that yields the given rows from execute."""

    async def _execute(sql: Any, params: Any = None) -> Any:
        result = MagicMock()
        result.fetchall.return_value = rows
        return result

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_execute)

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _make_openai_response(content: str) -> Any:
    choice = MagicMock()
    choice.message.content = content
    resp = MagicMock()
    resp.choices = [choice]
    return resp


# --------------------------------------------------------------------------- #
# _build_full_text
# --------------------------------------------------------------------------- #


class TestBuildFullText:
    def test_caller_labelled_correctly(self) -> None:
        rows = [_fake_row("CALLER", "Hello, I need help.")]
        assert _build_full_text(rows) == "Caller: Hello, I need help."

    def test_agent_labelled_correctly(self) -> None:
        rows = [_fake_row("AI", "Of course, how can I assist?")]
        assert _build_full_text(rows) == "Agent: Of course, how can I assist?"

    def test_alternating_speakers(self) -> None:
        rows = [
            _fake_row("CALLER", "Hi."),
            _fake_row("AI", "Hello!"),
            _fake_row("CALLER", "I need a refund."),
        ]
        result = _build_full_text(rows)
        assert result == "Caller: Hi.\nAgent: Hello!\nCaller: I need a refund."

    def test_empty_rows_returns_empty_string(self) -> None:
        assert _build_full_text([]) == ""

    def test_unknown_speaker_labelled_as_agent(self) -> None:
        rows = [_fake_row("HUMAN_AGENT", "Transferring you now.")]
        result = _build_full_text(rows)
        assert result == "Agent: Transferring you now."


# --------------------------------------------------------------------------- #
# generate_summary — skipping cases
# --------------------------------------------------------------------------- #


class TestGenerateSummarySkipped:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_segments(self) -> None:
        factory = _make_factory([])
        result = await generate_summary(
            context=_context(),
            transcript_row_id=uuid.uuid4(),
            factory=factory,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_fewer_than_min_segments(self) -> None:
        rows = [_fake_row("CALLER", "Hello?")] * (_MIN_SEGMENTS - 1)
        factory = _make_factory(rows)
        result = await generate_summary(
            context=_context(),
            transcript_row_id=uuid.uuid4(),
            factory=factory,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_proceeds_at_exactly_min_segments(self) -> None:
        """The boundary: MIN_SEGMENTS segments must trigger the LLM call."""
        rows = [
            _fake_row("CALLER", "I need help."),
            _fake_row("AI", "Sure."),
        ]
        assert len(rows) == _MIN_SEGMENTS
        factory = _make_factory(rows)

        with patch("worker.summariser._openai.AsyncOpenAI") as mock_cls:
            instance = AsyncMock()
            instance.chat.completions.create = AsyncMock(
                return_value=_make_openai_response("The caller needed help. The agent assisted.")
            )
            mock_cls.return_value = instance

            with patch("worker.summariser.make_resilient_client", return_value=MagicMock()):
                result = await generate_summary(
                    context=_context(),
                    transcript_row_id=uuid.uuid4(),
                    factory=factory,
                )

        assert result is not None
        summary, full_text = result
        assert summary is not None


# --------------------------------------------------------------------------- #
# generate_summary — normal path
# --------------------------------------------------------------------------- #


class TestGenerateSummaryNormalPath:
    @pytest.mark.asyncio
    async def test_returns_summary_and_full_text(self) -> None:
        rows = [
            _fake_row("CALLER", "My internet is down."),
            _fake_row("AI", "Let me check your account."),
            _fake_row("CALLER", "It has been down since yesterday."),
        ]
        factory = _make_factory(rows)

        with patch("worker.summariser._openai.AsyncOpenAI") as mock_cls:
            instance = AsyncMock()
            instance.chat.completions.create = AsyncMock(
                return_value=_make_openai_response(
                    "The caller reported a service outage since yesterday."
                )
            )
            mock_cls.return_value = instance
            with patch("worker.summariser.make_resilient_client", return_value=MagicMock()):
                result = await generate_summary(
                    context=_context(),
                    transcript_row_id=uuid.uuid4(),
                    factory=factory,
                )

        assert result is not None
        summary, full_text = result
        assert summary == "The caller reported a service outage since yesterday."
        assert "Caller: My internet is down." in full_text
        assert "Agent: Let me check your account." in full_text

    @pytest.mark.asyncio
    async def test_no_summary_sentinel_returns_none_summary(self) -> None:
        """When the LLM returns NO_SUMMARY, summary is None but full_text is
        still returned so the transcript is queryable."""
        rows = [_fake_row("CALLER", "ok"), _fake_row("AI", "bye")]
        factory = _make_factory(rows)

        with patch("worker.summariser._openai.AsyncOpenAI") as mock_cls:
            instance = AsyncMock()
            instance.chat.completions.create = AsyncMock(
                return_value=_make_openai_response("NO_SUMMARY")
            )
            mock_cls.return_value = instance
            with patch("worker.summariser.make_resilient_client", return_value=MagicMock()):
                result = await generate_summary(
                    context=_context(),
                    transcript_row_id=uuid.uuid4(),
                    factory=factory,
                )

        assert result is not None
        summary, full_text = result
        assert summary is None
        assert full_text  # still assembled

    @pytest.mark.asyncio
    async def test_empty_llm_response_returns_none_summary(self) -> None:
        rows = [_fake_row("CALLER", "x"), _fake_row("AI", "y")]
        factory = _make_factory(rows)

        with patch("worker.summariser._openai.AsyncOpenAI") as mock_cls:
            instance = AsyncMock()
            instance.chat.completions.create = AsyncMock(
                return_value=_make_openai_response("")
            )
            mock_cls.return_value = instance
            with patch("worker.summariser.make_resilient_client", return_value=MagicMock()):
                result = await generate_summary(
                    context=_context(),
                    transcript_row_id=uuid.uuid4(),
                    factory=factory,
                )

        assert result is not None
        summary, _ = result
        assert summary is None

    @pytest.mark.asyncio
    async def test_uses_provider_api_key(self) -> None:
        rows = [_fake_row("CALLER", "test"), _fake_row("AI", "ok")]
        factory = _make_factory(rows)
        ctx = _context()

        captured_kwargs: dict = {}

        with patch("worker.summariser._openai.AsyncOpenAI") as mock_cls:
            instance = AsyncMock()
            instance.chat.completions.create = AsyncMock(
                return_value=_make_openai_response("Summary here.")
            )
            mock_cls.return_value = instance

            def _capture(**kwargs: Any):
                captured_kwargs.update(kwargs)
                return instance

            mock_cls.side_effect = _capture

            with patch("worker.summariser.make_resilient_client", return_value=MagicMock()):
                await generate_summary(
                    context=ctx,
                    transcript_row_id=uuid.uuid4(),
                    factory=factory,
                )

        assert captured_kwargs.get("api_key") == ctx.llm.api_key

    @pytest.mark.asyncio
    async def test_spoken_template_does_not_override_post_call_prompt(self) -> None:
        """``summary_template`` is the spoken whisper string (TS-1), not an LLM prompt."""
        rows = [_fake_row("CALLER", "billing question"), _fake_row("AI", "noted")]
        factory = _make_factory(rows)
        ctx = _context(summary_template="Customer: {{customer}}. Reason: {{reason}}.")

        captured_messages: list = []

        with patch("worker.summariser._openai.AsyncOpenAI") as mock_cls:
            instance = AsyncMock()

            async def _create(**kwargs: Any):
                captured_messages.extend(kwargs.get("messages", []))
                return _make_openai_response("billing")

            instance.chat.completions.create = _create
            mock_cls.return_value = instance

            with patch("worker.summariser.make_resilient_client", return_value=MagicMock()):
                await generate_summary(
                    context=ctx,
                    transcript_row_id=uuid.uuid4(),
                    factory=factory,
                )

        system_msg = next(m for m in captured_messages if m["role"] == "system")
        assert _DEFAULT_SYSTEM_PROMPT in system_msg["content"]
        assert "{{customer}}" not in system_msg["content"]

    @pytest.mark.asyncio
    async def test_uses_default_prompt_when_template_is_none(self) -> None:
        rows = [_fake_row("CALLER", "a"), _fake_row("AI", "b")]
        factory = _make_factory(rows)
        ctx = _context(summary_template=None)

        captured_messages: list = []

        with patch("worker.summariser._openai.AsyncOpenAI") as mock_cls:
            instance = AsyncMock()

            async def _create(**kwargs: Any):
                captured_messages.extend(kwargs.get("messages", []))
                return _make_openai_response("ok")

            instance.chat.completions.create = _create
            mock_cls.return_value = instance

            with patch("worker.summariser.make_resilient_client", return_value=MagicMock()):
                await generate_summary(
                    context=ctx,
                    transcript_row_id=uuid.uuid4(),
                    factory=factory,
                )

        system_msg = next(m for m in captured_messages if m["role"] == "system")
        assert _DEFAULT_SYSTEM_PROMPT in system_msg["content"]


# --------------------------------------------------------------------------- #
# write_transcript_summary — SQL coverage
# --------------------------------------------------------------------------- #


class TestWriteTranscriptSummary:
    def test_sql_targets_call_transcripts(self) -> None:
        sql = str(_WRITE_SUMMARY_SQL)
        assert "call_transcripts" in sql

    def test_sql_sets_summary(self) -> None:
        assert "summary" in str(_WRITE_SUMMARY_SQL)

    def test_sql_sets_full_text(self) -> None:
        assert "full_text" in str(_WRITE_SUMMARY_SQL)

    def test_sql_updates_updated_at(self) -> None:
        assert "updated_at" in str(_WRITE_SUMMARY_SQL)

    def test_sql_filters_by_tenant_id(self) -> None:
        """Cross-tenant writes must be impossible (spec 7)."""
        assert "tenant_id" in str(_WRITE_SUMMARY_SQL)

    @pytest.mark.asyncio
    async def test_executes_update(self) -> None:
        session = AsyncMock()
        await write_transcript_summary(
            session,
            transcript_row_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            summary="The caller asked about billing.",
            full_text="Caller: billing?\nAgent: sure.",
        )
        session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_null_summary_is_allowed(self) -> None:
        """When the LLM returns NO_SUMMARY the summary column is NULL but the
        full_text is still stored."""
        session = AsyncMock()
        await write_transcript_summary(
            session,
            transcript_row_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            summary=None,
            full_text="Caller: hi.\nAgent: bye.",
        )
        session.execute.assert_awaited_once()
        _, call_params = session.execute.call_args[0]
        assert call_params["summary"] is None
        assert call_params["full_text"]


# --------------------------------------------------------------------------- #
# CallObserver properties
# --------------------------------------------------------------------------- #


class TestCallObserverProperties:
    def _make_observer(self) -> Any:
        from worker.pipeline.observer import CallObserver

        class _FakeMetrics:
            def __getattr__(self, name: str) -> MagicMock:
                return MagicMock()

        factory = MagicMock()
        return CallObserver(
            factory,
            call_row_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            call_id="obs-test",
            agent_id="test-agent",
            metrics=_FakeMetrics(),
            transcription_enabled=True,
        )

    def test_transcript_row_id_is_none_before_start(self) -> None:
        obs = self._make_observer()
        assert obs.transcript_row_id is None

    def test_segments_written_is_zero_before_start(self) -> None:
        obs = self._make_observer()
        assert obs.segments_written == 0

    @pytest.mark.asyncio
    async def test_transcript_row_id_set_after_start(self) -> None:
        obs = self._make_observer()
        session = AsyncMock()
        obs._factory.return_value.__aenter__ = AsyncMock(return_value=session)
        obs._factory.return_value.__aexit__ = AsyncMock(return_value=False)
        await obs.start()
        # After start(), the id must be a UUID.
        assert isinstance(obs.transcript_row_id, uuid.UUID)
        # Clean up background task.
        if obs._flusher:
            obs._flusher.cancel()

    def test_transcript_row_id_is_none_when_transcription_disabled(self) -> None:
        from worker.pipeline.observer import CallObserver

        class _FakeMetrics:
            def __getattr__(self, name: str) -> MagicMock:
                return MagicMock()

        factory = MagicMock()
        obs = CallObserver(
            factory,
            call_row_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            call_id="obs-test",
            agent_id="test-agent",
            metrics=_FakeMetrics(),
            transcription_enabled=False,
        )
        assert obs.transcript_row_id is None
