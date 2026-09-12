"""HTTP tool execution with timeout, retry, and auth (spec 31)."""

from __future__ import annotations

import base64
from typing import Any

import httpx

from shared.logging import get_logger
from shared.models import HttpMethod, ToolAuthType
from shared.tools import UnresolvedVariableError, substitute
from worker.tools.definitions import ToolDefinition, call_variables

logger = get_logger(__name__)


class ToolHttpError(RuntimeError):
    """The tenant API did not return a usable result."""


def _auth_headers(definition: ToolDefinition) -> dict[str, str]:
    secret = definition.auth_secret
    if definition.auth_type is ToolAuthType.NONE or not secret:
        return {}
    if definition.auth_type is ToolAuthType.BEARER_TOKEN:
        return {"Authorization": f"Bearer {secret}"}
    if definition.auth_type is ToolAuthType.API_KEY_HEADER:
        name = definition.auth_header_name or "X-API-Key"
        return {name: secret}
    if definition.auth_type is ToolAuthType.BASIC:
        token = base64.b64encode(secret.encode()).decode()
        return {"Authorization": f"Basic {token}"}
    # OAuth client-credentials is configured in the builder but not fetched
    # per call yet — fail clearly rather than sending an empty token.
    raise ToolHttpError(
        f"{definition.auth_type.value} authentication is not available on this worker"
    )


def _trim_response(payload: Any, schema: dict[str, Any]) -> Any:
    """Keep only keys the response schema names, so a noisy API cannot flood the prompt."""
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not properties or not isinstance(payload, dict):
        return payload
    return {key: payload[key] for key in properties if key in payload}


async def execute_http(
    definition: ToolDefinition,
    arguments: dict[str, Any],
    context: Any,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    values = {**call_variables(context), **arguments}
    try:
        url = substitute(definition.url_template, values)
        headers = {
            key: substitute(str(value), values) for key, value in (definition.headers or {}).items()
        }
    except UnresolvedVariableError as exc:
        return {"ok": False, "error": str(exc)}

    headers.update(_auth_headers(definition))
    method = (
        definition.http_method.value
        if isinstance(definition.http_method, HttpMethod)
        else str(definition.http_method)
    )
    timeout = httpx.Timeout(definition.timeout_seconds)
    attempts = 1 + max(0, definition.max_retries)
    last_error = "the request failed"

    own_client = client is None
    http = client or httpx.AsyncClient(timeout=timeout)
    try:
        for attempt in range(attempts):
            try:
                request_kwargs: dict[str, Any] = {"headers": headers}
                if method in {"POST", "PUT", "PATCH"}:
                    request_kwargs["json"] = arguments
                response = await http.request(method, url, **request_kwargs)
                if response.status_code >= 500 and attempt + 1 < attempts:
                    last_error = f"HTTP {response.status_code}"
                    continue
                if response.status_code >= 400:
                    return {
                        "ok": False,
                        "error": f"the API returned HTTP {response.status_code}",
                    }
                try:
                    payload: Any = response.json()
                except ValueError:
                    payload = {"text": response.text[:2000]}
                return {
                    "ok": True,
                    "result": _trim_response(payload, definition.response_schema),
                }
            except httpx.TimeoutException:
                last_error = f"timed out after {definition.timeout_seconds}s"
            except httpx.HTTPError as exc:
                last_error = str(exc) or exc.__class__.__name__
        return {"ok": False, "error": last_error}
    finally:
        if own_client:
            await http.aclose()
