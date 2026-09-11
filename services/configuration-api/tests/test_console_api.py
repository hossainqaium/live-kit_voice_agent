"""The console sections added for spec 60 and 61.

Two things are tested here that unit tests usually skip, both because a real
defect got through without them:

* **Every response schema is validated against a row-shaped object.** The audit
  schema declared ``ip_address: str`` while the column is PostgreSQL ``INET``,
  so asyncpg returned an ``IPv4Address`` and every read of the audit trail
  returned 500. Nothing caught it until the endpoint was called by hand,
  because no test ever built the response model.

* **Every new route is checked for authentication and a permission.** A route
  added without its ``require_permission`` dependency would otherwise be a
  silent, tenant-wide hole.
"""

from __future__ import annotations

import ipaddress
import uuid
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace

import pytest

from shared.models import (
    DayOfWeek,
    Permission,
    ProviderKind,
    ResourceStatus,
    TenantStatus,
)

NOW = datetime(2026, 9, 11, 7, 0, tzinfo=UTC)
ROW_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
TENANT_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")


def base_row(**extra: object) -> SimpleNamespace:
    """A stand-in for a persisted row, with the fields every response needs."""
    return SimpleNamespace(id=ROW_ID, created_at=NOW, updated_at=NOW, **extra)


# --------------------------------------------------------------------------- #
# Response schemas against realistic driver values
# --------------------------------------------------------------------------- #


class TestSchemasAcceptDatabaseValues:
    """Each schema is validated against what the driver actually returns.

    The values here are the driver's Python types, not JSON: ``INET`` becomes
    an ``IPv4Address``, ``TIME`` a ``time``, ``DATE`` a ``date``, and an
    enum column a member of the enum rather than its string.
    """

    def test_audit_log_accepts_an_inet_address(self) -> None:
        """The regression. ``ip_address`` is INET, so it is not a string."""
        from app.schemas.platform import AuditLogResponse

        row = SimpleNamespace(
            id=ROW_ID,
            occurred_at=NOW,
            tenant_id=TENANT_ID,
            user_id=ROW_ID,
            user_email="admin@example.com",
            action="tenant.updated",
            resource_type="tenant",
            resource_id=str(TENANT_ID),
            old_value={"name": "before"},
            new_value={"name": "after"},
            ip_address=ipaddress.ip_address("192.0.2.10"),
            request_id="abc",
        )
        parsed = AuditLogResponse.model_validate(row, from_attributes=True)
        # Serialised as a plain string, so the console does not have to know
        # it came from an INET column.
        assert parsed.model_dump(mode="json")["ip_address"] == "192.0.2.10"

    def test_audit_log_accepts_an_ipv6_address(self) -> None:
        from app.schemas.platform import AuditLogResponse

        row = SimpleNamespace(
            id=ROW_ID,
            occurred_at=NOW,
            tenant_id=None,
            user_id=None,
            user_email=None,
            action="user.signed_in",
            resource_type="user",
            resource_id=None,
            old_value=None,
            new_value=None,
            ip_address=ipaddress.ip_address("2001:db8::1"),
            request_id=None,
        )
        assert AuditLogResponse.model_validate(row, from_attributes=True).ip_address is not None

    def test_interval_response_accepts_time_columns(self) -> None:
        from app.schemas.routing import IntervalResponse

        row = SimpleNamespace(
            id=ROW_ID,
            day_of_week=DayOfWeek.MONDAY,
            opens_at=time(9, 0),
            closes_at=time(17, 30),
        )
        parsed = IntervalResponse.model_validate(row, from_attributes=True)
        assert parsed.model_dump(mode="json")["opens_at"] == "09:00:00"

    def test_usage_response_accepts_a_date_column(self) -> None:
        from app.schemas.admin import UsageResponse

        row = SimpleNamespace(
            usage_date=date(2026, 9, 11),
            call_count=3,
            answered_count=3,
            failed_count=0,
            transferred_count=0,
            total_seconds=120,
            ai_seconds=100,
        )
        assert UsageResponse.model_validate(row, from_attributes=True).call_count == 3

    def test_recording_response_accepts_a_bigint_and_nulls(self) -> None:
        from app.schemas.admin import RecordingResponse

        row = base_row(
            call_id=ROW_ID,
            bucket="recordings",
            object_key="2026/09/11/call.ogg",
            content_type=None,
            byte_size=4_294_967_296,
            duration_seconds=None,
            livekit_egress_id=None,
            started_at=NOW,
            ended_at=None,
            delete_after=None,
        )
        assert RecordingResponse.model_validate(row, from_attributes=True).byte_size > 0

    def test_tenant_summary_accepts_an_enum_column(self) -> None:
        from app.schemas.platform import TenantSummary

        row = base_row(
            name="Acme",
            slug="acme",
            status=TenantStatus.ACTIVE,
            timezone="UTC",
            default_language="en",
            max_concurrent_calls=None,
            max_daily_calls=None,
            max_monthly_minutes=None,
            notes=None,
        )
        parsed = TenantSummary.model_validate(row, from_attributes=True)
        assert parsed.model_dump(mode="json")["status"] == "ACTIVE"
        # Counts default rather than failing: the list view fills them in, and
        # the detail view of a brand-new tenant has none.
        assert parsed.user_count == 0

    def test_provider_response_accepts_enum_columns(self) -> None:
        from app.schemas.platform import ProviderResponse

        row = base_row(
            kind=ProviderKind.TTS,
            slug="cartesia",
            # A row's name and its adapter are separate columns, so a provider
            # can be registered twice for two endpoints (spec 25, 55).
            adapter="cartesia",
            display_name="Cartesia",
            status=ResourceStatus.ACTIVE,
            supports_streaming=True,
            default_base_url=None,
            requires_credential=True,
            notes=None,
        )
        assert ProviderResponse.model_validate(row, from_attributes=True).voice_count == 0

    def test_user_response_does_not_expose_the_password_hash(self) -> None:
        """A leaked hash is an offline cracking target (spec 70)."""
        from app.schemas.admin import UserResponse

        row = base_row(
            email="user@example.com",
            full_name="A User",
            is_active=True,
            last_login_at=None,
            password_hash="$2b$12$notarealhash",
            tokens_valid_from=None,
        )
        dumped = UserResponse.model_validate(row, from_attributes=True).model_dump()
        assert "password_hash" not in dumped
        assert "notarealhash" not in str(dumped)


# --------------------------------------------------------------------------- #
# Route coverage
# --------------------------------------------------------------------------- #


def _console_routes(app) -> list:
    from fastapi.routing import APIRoute

    return [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/v1/")
    ]


class TestEveryConsoleSectionIsReachable:
    """Spec 60 and 61 list the console sections; each needs an endpoint."""

    #: (path, method) pairs the tenant console depends on.
    TENANT_PATHS = (
        "/api/v1/routing-rules",
        "/api/v1/business-hours",
        "/api/v1/transfer-destinations",
        "/api/v1/tools",
        "/api/v1/knowledge-bases",
        "/api/v1/users",
        "/api/v1/settings",
        "/api/v1/analytics",
        "/api/v1/usage",
        "/api/v1/recordings",
    )
    PLATFORM_PATHS = (
        "/api/v1/platform/tenants",
        "/api/v1/platform/providers",
        "/api/v1/platform/models",
        "/api/v1/platform/voices",
        "/api/v1/platform/audit-logs",
        "/api/v1/platform/capacity",
    )

    @pytest.mark.parametrize("path", TENANT_PATHS + PLATFORM_PATHS)
    def test_the_section_has_a_route(self, app, path: str) -> None:
        assert any(
            route.path == path for route in _console_routes(app)
        ), f"{path} is missing, so its console section cannot work"

    def test_every_versioned_route_requires_a_token(self, app) -> None:
        """Only login and refresh may be reached unauthenticated."""
        public = {"/api/v1/auth/login", "/api/v1/auth/refresh"}
        for route in _console_routes(app):
            if route.path in public:
                continue
            names = {
                getattr(dependency.call, "__name__", "")
                for dependency in route.dependant.dependencies
            }
            flat = " ".join(str(dependency.call) for dependency in route.dependant.dependencies)
            assert (
                "get_principal" in flat
                or "principal" in names
                or "require" in flat
                or "get_tenant_scope" in flat
            ), f"{route.methods} {route.path} does not resolve a principal"

    @pytest.mark.parametrize(
        ("path", "method"),
        [
            ("/api/v1/platform/tenants", "POST"),
            ("/api/v1/platform/providers", "POST"),
            ("/api/v1/platform/models", "POST"),
            ("/api/v1/platform/voices", "POST"),
        ],
    )
    def test_catalog_writes_require_super_admin(self, app, path: str, method: str) -> None:
        """Spec 8 puts platform-wide changes behind SUPER_ADMIN."""
        route = next(r for r in _console_routes(app) if r.path == path and method in r.methods)
        flat = " ".join(str(dependency.call) for dependency in route.dependant.dependencies)
        assert "super_admin" in flat, f"{method} {path} is not restricted to SUPER_ADMIN"

    def test_no_route_can_edit_the_audit_trail(self, app) -> None:
        """Spec 69 makes the trail append-only."""
        for route in _console_routes(app):
            if "audit" in route.path:
                assert route.methods == {"GET"}, f"{route.path} exposes {route.methods}"


class TestTenantSettingsCannotRaiseItsOwnLimits:
    """Spec 47 makes the call limits the platform's lever, not the tenant's."""

    def test_update_schema_rejects_the_limit_fields(self) -> None:
        from pydantic import ValidationError

        from app.schemas.admin import TenantSettingsUpdate

        for field in ("max_concurrent_calls", "max_daily_calls", "max_monthly_minutes"):
            with pytest.raises(ValidationError):
                TenantSettingsUpdate.model_validate({field: 9999})

    def test_the_platform_schema_does_accept_them(self) -> None:
        from app.schemas.platform import TenantUpdate

        assert TenantUpdate(max_concurrent_calls=25).max_concurrent_calls == 25


# --------------------------------------------------------------------------- #
# Pure logic
# --------------------------------------------------------------------------- #


class TestBusinessHours:
    """Spec 37. Evaluated in the schedule's own timezone, not the server's."""

    @staticmethod
    def interval(day: DayOfWeek, opens: time, closes: time) -> SimpleNamespace:
        return SimpleNamespace(day_of_week=day, opens_at=opens, closes_at=closes)

    def test_unknown_timezone_returns_none_rather_than_closed(self) -> None:
        from app.api.v1.routing import _is_open_now

        assert _is_open_now([], "Not/AZone", []) is None

    def test_closed_when_no_interval_covers_now(self) -> None:
        from app.api.v1.routing import _is_open_now

        assert _is_open_now([], "UTC", []) is False

    def test_a_holiday_closes_the_day(self) -> None:
        from app.api.v1.routing import _is_open_now

        today = datetime.now(UTC).date().isoformat()
        every_day = [self.interval(day, time(0, 0), time(23, 59)) for day in DayOfWeek]
        assert _is_open_now(every_day, "UTC", [{"date": today, "closed": True}]) is False

    def test_a_holiday_can_declare_a_day_open(self) -> None:
        """A trading Sunday: the exception list works in both directions."""
        from app.api.v1.routing import _is_open_now

        today = datetime.now(UTC).date().isoformat()
        assert _is_open_now([], "UTC", [{"date": today, "closed": False}]) is True

    def test_a_holiday_on_another_date_is_ignored(self) -> None:
        from app.api.v1.routing import _is_open_now

        other = (datetime.now(UTC).date() + timedelta(days=3)).isoformat()
        every_day = [self.interval(day, time(0, 0), time(23, 59)) for day in DayOfWeek]
        assert _is_open_now(every_day, "UTC", [{"date": other, "closed": True}]) is True

    def test_the_closing_time_is_exclusive(self) -> None:
        """A schedule from 17:00 to 17:00 is not a minute of opening."""
        from pydantic import ValidationError

        from app.schemas.routing import IntervalInput

        with pytest.raises(ValidationError):
            IntervalInput(day_of_week=DayOfWeek.MONDAY, opens_at=time(17), closes_at=time(17))


class TestToolValidation:
    """Spec 33. A tool definition the provider would reject is worth catching
    at configuration time, not on a live call."""

    @staticmethod
    def tool(url: str, headers: dict | None = None) -> SimpleNamespace:
        return SimpleNamespace(url_template=url, headers=headers or {}, request_schema={})

    def test_url_variables_are_extracted(self) -> None:
        from app.api.v1.tools import _variables

        assert _variables(self.tool("https://api.example.com/guests/{{guest_id}}/stay")) == [
            "guest_id"
        ]

    def test_a_single_brace_is_not_a_variable(self) -> None:
        """The placeholder syntax is doubled so a literal brace stays literal.

        Tenant APIs do use single braces in paths and in JSON, and treating
        those as substitutions would silently blank part of a URL.
        """
        from app.api.v1.tools import _variables

        assert _variables(self.tool("https://api.example.com/guests/{guest_id}")) == []

    def test_header_variables_are_extracted_too(self) -> None:
        """A placeholder in a header is as much an input as one in the path."""
        from app.api.v1.tools import _variables

        found = _variables(self.tool("https://api.example.com/x", {"X-Room": "{{room_number}}"}))
        assert found == ["room_number"]

    def test_a_url_with_no_variables_yields_none(self) -> None:
        from app.api.v1.tools import _variables

        assert _variables(self.tool("https://api.example.com/ping")) == []

    def test_a_tool_name_must_be_an_identifier(self) -> None:
        """Providers reject anything else in a function name."""
        from pydantic import ValidationError

        from app.schemas.tool import ToolCreate

        with pytest.raises(ValidationError):
            ToolCreate.model_validate(
                {
                    "name": "look up guest",
                    "description": "x",
                    "http_method": "GET",
                    "url": "https://example.com",
                }
            )


class TestPlatformSlugs:
    def test_a_slug_is_lowercased_and_trimmed(self) -> None:
        from app.schemas.platform import TenantCreate

        assert TenantCreate(name="Acme", slug="  Acme-Hotels  ").slug == "acme-hotels"

    @pytest.mark.parametrize("bad", ["acme hotels", "acme_hotels", "-acme", "acme-", "acme/x"])
    def test_a_slug_rejects_what_would_break_a_url(self, bad: str) -> None:
        from pydantic import ValidationError

        from app.schemas.platform import TenantCreate

        with pytest.raises(ValidationError):
            TenantCreate(name="Acme", slug=bad)


class TestPermissionsAreDeclaredForEverySection:
    """Spec 8 lists twelve permissions; the console must use them all.

    A permission nothing checks is a permission that does not exist, and the
    roles built on it silently grant nothing.
    """

    @staticmethod
    def required_permissions(route) -> set[Permission]:
        """The permissions a route's dependencies actually check.

        ``require_permission`` closes over its arguments, so the requirement is
        read out of the closure cells. Matching on the repr instead would pass
        for a route that merely mentions a permission in a docstring.
        """
        found: set[Permission] = set()
        for dependency in route.dependant.dependencies:
            for cell in dependency.call.__closure__ or ():
                try:
                    contents = cell.cell_contents
                except ValueError:  # pragma: no cover - empty cell
                    continue
                if isinstance(contents, tuple):
                    found.update(c for c in contents if isinstance(c, Permission))
                elif isinstance(contents, Permission):
                    found.add(contents)
        return found

    def test_every_permission_guards_at_least_one_route(self, app) -> None:
        checked: set[Permission] = set()
        for route in _console_routes(app):
            checked |= self.required_permissions(route)
        unused = sorted(p.value for p in Permission if p not in checked)
        assert unused == [], f"no route checks: {unused}"

    @pytest.mark.parametrize(
        ("path", "permission"),
        [
            ("/api/v1/users", Permission.USERS_MANAGE),
            ("/api/v1/usage", Permission.BILLING_READ),
            ("/api/v1/analytics", Permission.ANALYTICS_READ),
            ("/api/v1/recordings", Permission.RECORDINGS_READ),
            ("/api/v1/routing-rules", Permission.AGENTS_READ),
            ("/api/v1/tools", Permission.AGENTS_READ),
        ],
    )
    def test_the_section_checks_the_permission_its_menu_entry_claims(
        self, app, path: str, permission: Permission
    ) -> None:
        """The sidebar greys an entry out by permission; the API must agree.

        If they disagree the console either hides a section the user may use,
        or shows one every request will refuse.
        """
        route = next(r for r in _console_routes(app) if r.path == path and "GET" in r.methods)
        assert permission in self.required_permissions(route)


class TestProviderSelectionAndCredentials:
    """The agent builder's provider controls (spec 24, 25, 26, 55, 62).

    These exist because the fields were reachable through the CLI long before
    they were reachable through the API, and the two drifted: the version table
    carried columns the API would not accept and the builder could not show.
    """

    def test_the_catalog_has_a_tenant_route(self, app) -> None:
        """Without it, the builder has nothing to populate a dropdown from."""
        assert any(route.path == "/api/v1/catalog" for route in _console_routes(app))

    def test_credentials_can_be_set_through_the_api(self, app) -> None:
        """Spec 77: no CLI step may be required to configure a tenant."""
        routes = {
            (route.path, method)
            for route in _console_routes(app)
            for method in route.methods
        }
        assert ("/api/v1/catalog/credentials", "PUT") in routes
        assert ("/api/v1/catalog/credentials/{credential_id}/verify", "POST") in routes
        assert ("/api/v1/catalog/credentials/{credential_id}", "DELETE") in routes

    def test_no_response_model_can_carry_an_api_key(self, app) -> None:
        """Spec 26 makes a stored key write-only.

        Asserted against every response model on every route rather than
        against the credential endpoints alone: the rule is that no endpoint
        anywhere returns a key, and a future endpoint that did would be exactly
        the kind of mistake nobody reviews for twice.
        """
        from fastapi.routing import APIRoute

        forbidden = {"api_key", "api_key_ciphertext", "password", "secret"}
        offenders: list[str] = []
        for route in app.routes:
            if not isinstance(route, APIRoute) or route.response_model is None:
                continue
            fields = getattr(route.response_model, "model_fields", {})
            leaked = forbidden & set(fields)
            if leaked:
                offenders.append(f"{route.path} returns {sorted(leaked)}")
        assert not offenders, "; ".join(offenders)

    def test_the_request_model_accepts_a_key(self) -> None:
        """The other half: write-only means writable, not absent."""
        from app.schemas.catalog import CredentialUpsert

        assert "api_key" in CredentialUpsert.model_fields

    def test_a_short_key_is_refused(self) -> None:
        """A blank or truncated paste is the most common way this goes wrong."""
        import pydantic

        from app.schemas.catalog import CredentialUpsert

        with pytest.raises(pydantic.ValidationError):
            CredentialUpsert(provider_id=uuid.uuid4(), api_key="abc")

    @pytest.mark.parametrize(
        "field",
        [
            "stt_fallback_provider_id",
            "stt_fallback_model_id",
            "llm_fallback_provider_id",
            "llm_fallback_model_id",
            "tts_fallback_provider_id",
            "tts_fallback_model_id",
            "tts_fallback_voice_id",
            "stt_local_provider_id",
            "stt_local_model_id",
            "tts_local_provider_id",
            "tts_local_model_id",
            "tts_local_voice_id",
        ],
    )
    def test_every_tier_is_accepted_by_the_api(self, field: str) -> None:
        """``extra="forbid"`` means an unlisted field is a 422, not a no-op.

        The failure this catches is a column added to the table and the
        migration, rendered in the builder, and silently rejected by the
        request model — which reads to an operator as a form that does not save.
        """
        from app.schemas.agent import AgentVersionConfig

        assert field in AgentVersionConfig.model_fields

    @pytest.mark.parametrize(
        "field",
        [
            "stt_fallback_provider_id",
            "llm_fallback_provider_id",
            "tts_fallback_provider_id",
            "stt_local_provider_id",
            "tts_local_provider_id",
        ],
    )
    def test_every_tier_is_validated_against_the_catalog(self, field: str) -> None:
        """A tier rendered but not validated accepts a dangling foreign key.

        Both the label resolution and the reference check are derived from
        ``_PROVIDER_TIERS``, so this asserts the table covers the columns rather
        than that two hand-written lists happen to agree.
        """
        from app.api.v1.agents import _PROVIDER_TIERS

        columns = {provider for _label, provider, _model, _voice in _PROVIDER_TIERS}
        columns |= {model for _label, _provider, model, _voice in _PROVIDER_TIERS}
        assert field in columns

    def test_the_version_response_labels_every_tier(self) -> None:
        """The builder shows a chain; it cannot resolve twelve keys itself."""
        from app.schemas.agent import AgentVersionResponse

        for label in (
            "stt_label",
            "llm_label",
            "tts_label",
            "voice_label",
            "stt_fallback_label",
            "llm_fallback_label",
            "tts_fallback_label",
            "stt_local_label",
            "tts_local_label",
        ):
            assert label in AgentVersionResponse.model_fields, label

    def test_the_llm_stage_has_no_local_tier(self) -> None:
        """Deliberate, and asserted so it is not "fixed" by accident.

        A self-hosted language model is a deployment decision with its own
        hardware, not a switch a tenant can flip. Offering the control without
        that would be a promise the platform cannot keep.
        """
        from app.schemas.agent import AgentVersionConfig

        assert "llm_local_provider_id" not in AgentVersionConfig.model_fields
