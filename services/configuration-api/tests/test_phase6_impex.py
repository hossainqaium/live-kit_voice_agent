"""Plan 6.13: tenant configuration export/import never carries plaintext secrets."""

from __future__ import annotations

import inspect

import pytest

from app.api.v1 import admin as admin_api
from app.services import tenant_impex
from tests.conftest import frontend_file


class TestSecretStripping:
    def test_plaintext_api_key_is_detected(self) -> None:
        assert tenant_impex.contains_plaintext_secret({"api_key": "sk-live-secret"})

    def test_tool_auth_secret_is_detected(self) -> None:
        assert tenant_impex.contains_plaintext_secret(
            {"tools": [{"name": "x", "auth_secret": "super-secret"}]}
        )

    def test_boolean_configured_flag_is_not_a_secret(self) -> None:
        assert not tenant_impex.contains_plaintext_secret(
            {"credentials": [{"label": "primary", "configured": True}]}
        )

    def test_strip_drops_secret_strings_and_keeps_the_rest(self) -> None:
        cleaned = tenant_impex.strip_secrets(
            {
                "name": "check_order",
                "api_key": "sk-live-secret",
                "auth_secret_ciphertext": "cipher",
                "configured": True,
                "url_template": "https://example.com/{{order_id}}",
            }
        )
        dumped = str(cleaned)
        assert "sk-live-secret" not in dumped
        assert "cipher" not in dumped
        assert cleaned["name"] == "check_order"
        assert cleaned["configured"] is True
        assert cleaned["url_template"] == "https://example.com/{{order_id}}"

    async def test_import_refuses_a_bundle_with_a_stuffed_secret(self) -> None:
        import uuid

        with pytest.raises(ValueError, match="plaintext secret"):
            await tenant_impex.import_tenant(
                session=None,  # type: ignore[arg-type]
                tenant_id=uuid.uuid4(),
                bundle={
                    "format": tenant_impex.FORMAT,
                    "tools": [{"name": "x", "api_key": "sk-secret"}],
                },
            )


class TestExportContract:
    def test_export_never_reads_provider_ciphertext_into_the_bundle(self) -> None:
        src = inspect.getsource(tenant_impex.export_tenant)
        assert "api_key_ciphertext" not in src
        assert "sip_auth" not in src

    def test_export_records_tool_auth_as_a_flag_only(self) -> None:
        src = inspect.getsource(tenant_impex.export_tenant)
        assert "auth_configured" in src
        assert "auth_secret_ciphertext is not None" in src

    def test_import_never_assigns_a_secret_column(self) -> None:
        src = inspect.getsource(tenant_impex)
        assert "auth_secret_ciphertext =" not in src
        assert "api_key_ciphertext =" not in src

    def test_import_creates_draft_versions(self) -> None:
        src = inspect.getsource(tenant_impex._import_agents)
        assert "AgentVersionState.DRAFT" in src
        assert "PUBLISHED" not in src

    def test_format_constant(self) -> None:
        assert tenant_impex.FORMAT == "livekit-voice-agent.tenant.v1"

    async def test_unknown_format_is_refused(self) -> None:
        import uuid

        with pytest.raises(ValueError, match="unsupported bundle format"):
            await tenant_impex.import_tenant(
                session=None,  # type: ignore[arg-type]
                tenant_id=uuid.uuid4(),
                bundle={"format": "other.v1", "tools": []},
            )


class TestRoutes:
    def test_export_is_a_get_and_is_not_audited(self) -> None:
        src = inspect.getsource(admin_api.export_settings)
        assert "export_tenant" in src
        assert "audit.record" not in src

    def test_import_is_audited(self) -> None:
        src = inspect.getsource(admin_api.import_settings)
        assert "audit.record" in src
        assert "tenant.imported" in src
        assert "import_tenant" in src

    def test_import_uses_agents_write(self) -> None:
        src = inspect.getsource(admin_api)
        assert "settings/import" in src
        assert "AGENTS_WRITE" in src


class TestSeedCatalog:
    def test_section_25_adapters_are_seeded(self) -> None:
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "app" / "services" / "seed.py").read_text()
        assert '"elevenlabs_stt"' in src
        assert '"azure_stt"' in src
        assert '"google_tts"' in src
        assert '"azure_tts"' in src
        assert '"anthropic"' in src
        assert '"cartesia"' in src
        assert '"deepgram"' in src


class TestConsole:
    def test_settings_page_exports_and_imports(self) -> None:
        path = frontend_file("app", "settings", "page.tsx")
        if path is None:
            pytest.skip("services/frontend is not mounted in this container")
        src = path.read_text()
        assert "api.settings.export" in src
        assert "api.settings.importBundle" in src
        assert "secrets are never written" in src or "never written to the file" in src

    def test_client_has_export_and_import_methods(self) -> None:
        path = frontend_file("lib", "api.ts")
        if path is None:
            pytest.skip("services/frontend is not mounted in this container")
        src = path.read_text()
        assert "/settings/export" in src
        assert "/settings/import" in src
