"""Audit trail redaction (spec 69, 54).

The trail exists so an authorization model can be reviewed after an incident.
That makes its redaction a security boundary in itself: an audit row that
records a credential turns ``analytics.read`` into a way to read another
tenant's API keys.
"""

from __future__ import annotations

import pytest

from app.services.audit import redact, snapshot


class FakeRow:
    def __init__(self, **fields: object) -> None:
        for key, value in fields.items():
            setattr(self, key, value)


class TestRedaction:
    @pytest.mark.parametrize(
        "field",
        [
            "password",
            "api_key",
            "apiKey",
            "provider_secret",
            "auth_token",
            "authorization",
            "credential",
            "private_key",
            "api_key_ciphertext",
        ],
    )
    def test_a_secret_bearing_string_is_replaced(self, field: str) -> None:
        assert redact({field: "the-real-value"})[field] == "***redacted***"

    def test_a_nested_secret_is_replaced(self) -> None:
        """Unlike the log redactor, this walks the structure.

        An audit payload is a nested representation of a resource, so a
        credential can sit several levels down — inside a provider's config, or
        in a list of trunk definitions.
        """
        payload = {
            "provider": {"slug": "openai", "config": {"api_key": "sk-real-key"}},
        }
        result = redact(payload)
        assert result["provider"]["config"]["api_key"] == "***redacted***"
        assert result["provider"]["slug"] == "openai"

    def test_a_secret_inside_a_list_is_replaced(self) -> None:
        payload = {"trunks": [{"name": "a", "password": "hunter2"}, {"name": "b"}]}
        result = redact(payload)
        assert result["trunks"][0]["password"] == "***redacted***"
        assert result["trunks"][0]["name"] == "a"

    def test_the_secret_value_appears_nowhere_in_the_output(self) -> None:
        import json

        result = redact({"nested": {"deep": {"api_key": "sk-should-not-survive"}}})
        assert "sk-should-not-survive" not in json.dumps(result)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("prompt_tokens", 1280),
            ("token_count", 42),
            ("api_key_rotation_days", 90),
            ("secret_count", 3),
            ("password_changed_at", 1789000000),
        ],
    )
    def test_a_numeric_field_is_not_redacted(self, field: str, value: object) -> None:
        """Only strings and bytes can carry a credential.

        The name heuristic alone destroys real information: a token *count* and
        a rotation *interval* are both useful and neither is a secret. This is
        the same over-redaction that once replaced a latency measurement.
        """
        assert redact({field: value})[field] == value

    def test_bytes_are_summarised_not_stored(self) -> None:
        """Ciphertext should leave a trace that something was there, not what."""
        assert redact({"api_key_ciphertext": b"0" * 48}) == {"api_key_ciphertext": "***redacted***"}
        assert redact({"blob": b"0" * 48}) == {"blob": "<48 bytes>"}

    def test_an_ordinary_field_passes_through(self) -> None:
        assert redact({"name": "Head Office PBX"})["name"] == "Head Office PBX"

    def test_none_and_booleans_pass_through(self) -> None:
        result = redact({"description": None, "is_active": True})
        assert result["description"] is None
        assert result["is_active"] is True


class TestSnapshot:
    def test_only_named_fields_are_captured(self) -> None:
        """An explicit field list rather than the whole row.

        A full dump would grow silently as columns are added, and would pull
        ciphertext columns into the trail by default.
        """
        row = FakeRow(name="PBX", host="10.0.0.1", api_key_ciphertext=b"secret")
        assert snapshot(row, "name", "host") == {"name": "PBX", "host": "10.0.0.1"}

    def test_a_missing_field_is_recorded_as_none(self) -> None:
        """A field removed from the model should not break auditing."""
        assert snapshot(FakeRow(name="PBX"), "name", "gone") == {
            "name": "PBX",
            "gone": None,
        }

    def test_a_captured_secret_is_still_redacted(self) -> None:
        """Belt and braces: even if a secret field is named explicitly."""
        row = FakeRow(name="PBX", password="hunter2")
        assert snapshot(row, "name", "password")["password"] == "***redacted***"
