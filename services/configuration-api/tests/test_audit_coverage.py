"""Audit trail coverage across every mutating API endpoint (spec 69, 3b.5).

The audit log is only useful if it is complete.  A test that checks redaction
in isolation is not enough: an endpoint that never calls ``audit.record`` is
not visible there.  This suite provides the structural guarantee:

    Every POST / PUT / PATCH / DELETE handler either calls ``audit.record``
    (directly or via a module-private helper it delegates to), or is listed in
    ``_AUDIT_EXEMPT`` with a documented reason.

Approach
--------
``inspect.getsource`` on the endpoint function is read to check for a direct
``audit.record`` call.  When the handler delegates to a private function in
the same module (e.g. ``_set_status`` in ``pbxs.py``), the helper's source is
also checked — one level deep.  This covers the delegation pattern without
requiring a live database or dependency injection.

Adding a new mutating endpoint without ``audit.record`` makes this test fail
immediately, regardless of whether the endpoint is in an existing or a new
router file.
"""

from __future__ import annotations

import inspect
import re
from typing import Any

import pytest
from fastapi.routing import APIRoute

#: Methods that should write an audit row on success.
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Routes that intentionally do not write audit rows.
#: Removing an entry here without adding ``audit.record`` to the handler will
#: immediately cause the coverage test to fail — the exemption list is not a
#: way to skip the check permanently.
_AUDIT_EXEMPT: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/auth/refresh"): (
        "Token rotation only — no configuration object is mutated.  The "
        "resulting token is scoped to the same user and tenant.  Auditing "
        "every refresh would produce high-volume noise with no security value."
    ),
    ("POST", "/api/v1/catalog/credentials/verify-draft"): (
        "The submitted key is never persisted.  Writing an audit row would "
        "require capturing the key, which spec 54 explicitly prohibits.  The "
        "live check (credential.verified) IS audited."
    ),
}


def _handler_is_audited(endpoint: Any) -> bool:
    """Return True if ``endpoint`` or a private module helper it calls writes
    an audit row.

    Two-level check:
    1. Direct ``audit.record`` in the handler source.
    2. A private function (``_name``) called from the handler whose source
       also contains ``audit.record`` — covers the ``_set_status`` delegation
       pattern used in pbxs, sip_trunks, and phone_numbers.
    """
    try:
        fn_src = inspect.getsource(endpoint)
    except (OSError, TypeError):
        # Cannot read source (compiled / built-in).  Fail closed.
        return False

    if "audit.record" in fn_src:
        return True

    # Check one level of private-helper delegation.
    module = inspect.getmodule(endpoint)
    if module is None:
        return False

    for private_name in set(re.findall(r"\b(_\w+)\s*\(", fn_src)):
        helper = getattr(module, private_name, None)
        if helper is None or not callable(helper):
            continue
        try:
            helper_src = inspect.getsource(helper)
        except (OSError, TypeError):
            continue
        if "audit.record" in helper_src:
            return True

    return False


class TestAuditCoverage:
    """Every mutating route must write an audit row or be explicitly exempt."""

    def test_every_mutating_route_is_audited_or_explicitly_exempt(self, app) -> None:
        """Structural guard: a new POST/PUT/PATCH/DELETE without audit.record
        fails this test immediately, regardless of which router it lives in."""
        offenders: list[str] = []

        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in route.methods or set():
                if method not in _MUTATING_METHODS:
                    continue
                key = (method, route.path)
                if key in _AUDIT_EXEMPT:
                    continue
                if not _handler_is_audited(route.endpoint):
                    offenders.append(f"{method} {route.path}")

        assert not offenders, (
            f"Mutating routes with no audit.record call (direct or via "
            f"private helper):\n"
            + "\n".join(f"  - {o}" for o in sorted(offenders))
            + "\n\nEither add `await audit.record(...)` to the handler, or "
            "add the route to _AUDIT_EXEMPT in test_audit_coverage.py with a "
            "documented reason."
        )

    def test_exempt_list_contains_only_real_routes(self, app) -> None:
        """Routes in the exempt list must exist; stale entries are misleading."""
        existing = {
            (method, route.path)
            for route in app.routes
            if isinstance(route, APIRoute)
            for method in (route.methods or set())
        }
        stale = [f"{m} {p}" for (m, p) in _AUDIT_EXEMPT if (m, p) not in existing]
        assert not stale, (
            f"Routes in _AUDIT_EXEMPT that no longer exist: {stale}. "
            "Remove them from the exempt list."
        )

    def test_exempt_entries_have_documented_reasons(self) -> None:
        """Every exemption must explain why — silence is not an audit policy."""
        for key, reason in _AUDIT_EXEMPT.items():
            assert reason and len(reason.strip()) > 20, (
                f"_AUDIT_EXEMPT entry {key!r} has no meaningful reason string."
            )

    def test_verify_credential_is_audited(self, app) -> None:
        """Regression: POST /catalog/credentials/{id}/verify must audit the
        success path because it updates last_verified_at in the database."""
        route = next(
            (
                r
                for r in app.routes
                if isinstance(r, APIRoute)
                and r.path == "/api/v1/catalog/credentials/{credential_id}/verify"
            ),
            None,
        )
        assert route is not None, "verify_credential route not found"
        src = inspect.getsource(route.endpoint)
        assert "audit.record" in src, (
            "verify_credential must call audit.record on successful verification "
            "(it commits last_verified_at to the DB — that is a mutation)."
        )

    def test_auth_refresh_is_in_exempt_list(self) -> None:
        """The refresh exemption must be explicitly acknowledged."""
        assert ("POST", "/api/v1/auth/refresh") in _AUDIT_EXEMPT

    def test_verify_draft_is_in_exempt_list(self) -> None:
        """verify-draft exemption must be explicitly acknowledged (spec 54)."""
        assert ("POST", "/api/v1/catalog/credentials/verify-draft") in _AUDIT_EXEMPT
