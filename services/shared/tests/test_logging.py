"""Tests for structured logging and correlation context (spec 43, 58)."""

from __future__ import annotations

import json
import logging

import pytest

from shared.logging import JsonFormatter, bind, clear, get_context, log_context, reset


def _format(record_kwargs: dict | None = None, *, message: str = "test_event") -> dict:
    """Render one log record through the formatter and parse it back."""
    formatter = JsonFormatter(service="test-service")
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in (record_kwargs or {}).items():
        setattr(record, key, value)
    return json.loads(formatter.format(record))


class TestJsonFormatter:
    def test_emits_required_fields(self) -> None:
        document = _format()
        # Spec 58 requires service, timestamp and event on every record.
        assert document["service"] == "test-service"
        assert document["event"] == "test_event"
        assert "timestamp" in document
        assert document["level"] == "info"

    def test_timestamp_is_utc_iso8601(self) -> None:
        document = _format()
        assert document["timestamp"].endswith("+00:00")

    def test_extra_fields_are_included(self) -> None:
        document = _format({"call_duration_ms": 1234})
        assert document["call_duration_ms"] == 1234

    def test_output_is_a_single_line(self) -> None:
        formatter = JsonFormatter(service="test-service")
        record = logging.LogRecord("n", logging.INFO, __file__, 1, "multi\nline", (), None)
        # A newline inside a log line would split one event into two for any
        # line-based collector.
        assert "\n" not in formatter.format(record)

    @pytest.mark.parametrize(
        "field",
        ["password", "api_key", "apiKey", "provider_secret", "auth_token", "Authorization"],
    )
    def test_secret_bearing_fields_are_redacted(self, field: str) -> None:
        """Credentials must never reach the logs (spec 54)."""
        document = _format({field: "super-secret-value"})
        assert document[field] == "***redacted***"
        assert "super-secret-value" not in json.dumps(document)

    def test_non_secret_fields_are_not_redacted(self) -> None:
        document = _format({"tenant_name": "Hotel ABC"})
        assert document["tenant_name"] == "Hotel ABC"

    def test_exception_is_captured_structurally(self) -> None:
        formatter = JsonFormatter(service="test-service")
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            record = logging.LogRecord(
                "n", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
            )
        document = json.loads(formatter.format(record))
        assert document["error"]["type"] == "ValueError"
        assert document["error"]["message"] == "boom"
        assert "stack" in document["error"]


class TestCorrelationContext:
    def setup_method(self) -> None:
        clear()

    def test_bound_fields_appear_on_records(self) -> None:
        with log_context(call_id="call_123", tenant_id="tenant_001"):
            document = _format(message="tts_started")
        assert document["call_id"] == "call_123"
        assert document["tenant_id"] == "tenant_001"

    def test_context_is_removed_after_the_block(self) -> None:
        with log_context(call_id="call_123"):
            pass
        assert "call_id" not in _format()

    def test_nested_context_merges_then_unwinds(self) -> None:
        with log_context(tenant_id="tenant_001"):
            with log_context(call_id="call_123"):
                inner = get_context()
            outer = get_context()
        assert inner == {"tenant_id": "tenant_001", "call_id": "call_123"}
        assert outer == {"tenant_id": "tenant_001"}

    def test_unknown_field_is_rejected(self) -> None:
        # A typo like `call_di` would otherwise silently produce a log line
        # that no correlation search would ever find.
        with pytest.raises(ValueError, match="unknown correlation field"):
            bind(cal_id="typo")

    def test_none_values_are_dropped(self) -> None:
        token = bind(call_id="call_123", room_id=None)
        try:
            assert get_context() == {"call_id": "call_123"}
        finally:
            reset(token)

    def test_context_survives_across_await_points(self) -> None:
        """contextvars must carry the call's identity through async code."""
        import asyncio

        async def nested() -> dict:
            await asyncio.sleep(0)
            return get_context()

        async def main() -> dict:
            with log_context(call_id="call_async"):
                return await nested()

        assert asyncio.run(main())["call_id"] == "call_async"
