"""Tool placeholders and request-schema checks (spec 31).

Shared so the Configuration API and the worker agree on what ``{{name}}``
means. A placeholder the API accepts must be one the worker can fill, or a
tool that validated cleanly would still fail mid-call.
"""

from __future__ import annotations

import re
from typing import Any

#: Doubled braces so a literal ``{id}`` in a tenant URL is not treated as a
#: substitution. Tenant APIs use single braces; treating those as variables
#: would silently blank part of the path.
VARIABLE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

#: Platform handlers, not HTTP. The worker dispatches on this prefix.
BUILTIN_PREFIX = "builtin://"

#: Filled from the live call, not from the model. A URL may reference these
#: without declaring them in the request schema (spec 31).
CONTEXT_VARIABLES: frozenset[str] = frozenset(
    {"caller_number", "call_id", "did", "tenant_id", "agent_name"}
)


class UnresolvedVariableError(ValueError):
    """A placeholder had no value when the tool was invoked."""

    def __init__(self, name: str) -> None:
        super().__init__(f"no value for {{{{{name}}}}}")
        self.name = name


def extract_variables(*texts: str) -> list[str]:
    """Unique placeholder names, sorted, from URL and header values."""
    found: set[str] = set()
    for text in texts:
        found.update(VARIABLE.findall(text or ""))
    return sorted(found)


def substitute(template: str, values: dict[str, Any]) -> str:
    """Replace ``{{name}}`` with ``values[name]``.

    Missing or ``None`` is an error rather than an empty string: blanking a
    path segment produces a request the tenant API will mis-route, and the
    model should hear that the argument was missing.
    """

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values or values[key] is None:
            raise UnresolvedVariableError(key)
        return str(values[key])

    return VARIABLE.sub(repl, template)


def is_builtin(url_template: str) -> bool:
    return (url_template or "").startswith(BUILTIN_PREFIX)


def builtin_name(url_template: str) -> str:
    return (url_template or "")[len(BUILTIN_PREFIX) :].strip()


def validate_request_schema(
    schema: dict[str, Any] | None, referenced: list[str]
) -> tuple[bool, str | None]:
    """Whether a request schema can be sent to a provider as a function definition.

    Shallow on purpose: it confirms the shape a provider needs, not the whole
    of JSON Schema. A tool that passes here can still describe arguments the
    tenant's API rejects — that is the tenant's contract.
    """
    schema = schema or {}
    if not schema:
        unsatisfied = set(referenced) - CONTEXT_VARIABLES
        if unsatisfied:
            return False, (
                "the URL or headers reference variable(s) the schema does not declare: "
                f"{', '.join(sorted(unsatisfied))}"
            )
        return True, None

    if not isinstance(schema, dict):
        return False, "the request schema must be an object"
    if schema.get("type") not in (None, "object"):
        return False, "the top-level schema type must be 'object'"

    properties = schema.get("properties")
    if properties is not None and not isinstance(properties, dict):
        return False, "'properties' must be an object"

    required = schema.get("required")
    if required is not None:
        if not isinstance(required, list):
            return False, "'required' must be a list"
        missing = [name for name in required if name not in (properties or {})]
        if missing:
            return False, f"'required' names undeclared propert(ies): {', '.join(missing)}"

    declared = set(properties or {})
    unsatisfied = set(referenced) - declared - CONTEXT_VARIABLES
    if unsatisfied:
        return False, (
            "the URL or headers reference variable(s) the schema does not declare: "
            f"{', '.join(sorted(unsatisfied))}"
        )
    return True, None


def missing_required_arguments(
    schema: dict[str, Any] | None, arguments: dict[str, Any]
) -> list[str]:
    """Required property names the model omitted or sent empty."""
    required = (schema or {}).get("required") or []
    if not isinstance(required, list):
        return []
    missing: list[str] = []
    for name in required:
        if not isinstance(name, str):
            continue
        value = arguments.get(name)
        if value is None or value == "":
            missing.append(name)
    return missing
