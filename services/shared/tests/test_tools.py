"""Variable substitution and request-schema checks (spec 31)."""

from __future__ import annotations

import pytest

from shared.tools import (
    CONTEXT_VARIABLES,
    UnresolvedVariableError,
    extract_variables,
    is_builtin,
    missing_required_arguments,
    substitute,
    validate_request_schema,
)


class TestExtract:
    def test_url_and_headers(self) -> None:
        assert extract_variables(
            "https://api.example.com/orders/{{order_id}}",
            "{{caller_number}}",
        ) == ["caller_number", "order_id"]

    def test_single_braces_are_literal(self) -> None:
        assert extract_variables("https://api.example.com/orders/{order_id}") == []


class TestSubstitute:
    def test_fills_placeholders(self) -> None:
        assert (
            substitute("/orders/{{order_id}}", {"order_id": "ORD-100"}) == "/orders/ORD-100"
        )

    def test_missing_is_an_error(self) -> None:
        with pytest.raises(UnresolvedVariableError) as exc:
            substitute("/orders/{{order_id}}", {})
        assert exc.value.name == "order_id"

    def test_none_is_an_error(self) -> None:
        with pytest.raises(UnresolvedVariableError):
            substitute("/c/{{caller_number}}", {"caller_number": None})


class TestSchema:
    def test_empty_schema_is_valid(self) -> None:
        assert validate_request_schema({}, []) == (True, None)

    def test_caller_number_need_not_be_declared(self) -> None:
        valid, error = validate_request_schema({}, ["caller_number", "did"])
        assert valid is True
        assert error is None
        assert "caller_number" in CONTEXT_VARIABLES

    def test_model_variables_must_be_declared(self) -> None:
        valid, error = validate_request_schema({}, ["order_id"])
        assert valid is False
        assert error is not None
        assert "order_id" in error

    def test_required_must_exist_in_properties(self) -> None:
        valid, error = validate_request_schema(
            {"type": "object", "properties": {}, "required": ["title"]},
            [],
        )
        assert valid is False
        assert error is not None
        assert "title" in error

    def test_missing_required_arguments(self) -> None:
        schema = {"required": ["title", "description"]}
        assert missing_required_arguments(schema, {"title": "x"}) == ["description"]
        assert missing_required_arguments(schema, {"title": "x", "description": "y"}) == []


class TestBuiltin:
    def test_prefix(self) -> None:
        assert is_builtin("builtin://create_ticket")
        assert not is_builtin("https://api.example.com/tickets")
