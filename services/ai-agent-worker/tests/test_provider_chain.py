"""Fallback chain resolution (spec 25, 55).

The chain is configurable in the console, so these tests answer the question
that matters about any new configuration surface: does the worker read it?

They exercise the resolution helpers directly rather than through a call. The
alternative — asserting the SQL returns the right columns — needs a database,
and the failure worth catching here is a tier the console writes and the loader
silently drops, which is logic and not SQL.
"""

from __future__ import annotations

import uuid

from shared.models import ProviderKind
from worker.config_loader import CallConfigLoader


def version_row(**overrides: object) -> dict:
    """A ``_VERSION_SQL`` row with only a primary provider configured."""
    row = {
        "language": "en",
        "temperature": 0.7,
        "stt_slug": "openai_compatible",
        "stt_model": "whisper-1",
        "stt_base_url": "http://speech.internal/v1",
        "stt_provider_id": uuid.uuid4(),
        "llm_slug": "openai_compatible",
        "llm_model": "gpt-4o-mini",
        "llm_base_url": None,
        "llm_provider_id": uuid.uuid4(),
        "tts_slug": "openai_compatible",
        "tts_model": "tts-1",
        "tts_base_url": None,
        "tts_provider_id": uuid.uuid4(),
        "voice_external_id": "af_heart",
    }
    # Every optional tier absent, which is the state of every version that
    # existed before the tiers did.
    for prefix in ("sttf", "llmf", "ttsf", "sttl", "ttsl"):
        row[f"{prefix}_slug"] = None
        row[f"{prefix}_model"] = None
        row[f"{prefix}_base_url"] = None
        row[f"{prefix}_provider_id"] = None
    row["ttsf_voice"] = None
    row["ttsl_voice"] = None
    row.update(overrides)
    return row


class TestChainResolution:
    def test_a_version_with_no_extra_tiers_yields_no_fallbacks(self) -> None:
        """The backward-compatible case, and the common one."""
        loader = CallConfigLoader()
        row = version_row()

        assert (
            loader._optional_provider_config(ProviderKind.STT, row, "sttf", {}) is None
        )
        assert loader._chain((None, None)) == ()

    def test_a_configured_tier_is_resolved(self) -> None:
        loader = CallConfigLoader()
        provider_id = uuid.uuid4()
        row = version_row(
            sttf_slug="openai_compatible",
            sttf_model="gpt-4o-mini-transcribe",
            sttf_base_url="https://api.openai.com/v1",
            sttf_provider_id=provider_id,
        )

        config = loader._optional_provider_config(
            ProviderKind.STT, row, "sttf", {provider_id: ("sk-test", None)}, language="en"
        )

        assert config is not None
        assert config.kind is ProviderKind.STT
        assert config.model == "gpt-4o-mini-transcribe"
        assert config.api_key == "sk-test"
        assert config.base_url == "https://api.openai.com/v1"

    def test_a_tier_uses_its_own_credential(self) -> None:
        """Each tier's provider gets its own key.

        The failure this guards against is a chain whose fallback silently
        inherits the primary's credential: it would appear to work in testing
        against one vendor and 401 the first time the fallback was needed,
        which is the worst possible moment to find out.
        """
        loader = CallConfigLoader()
        primary_id, fallback_id = uuid.uuid4(), uuid.uuid4()
        row = version_row(
            stt_provider_id=primary_id,
            sttf_slug="openai_compatible",
            sttf_model="whisper-1",
            sttf_base_url=None,
            sttf_provider_id=fallback_id,
        )
        credentials = {primary_id: ("primary-key", None), fallback_id: ("fallback-key", None)}

        primary = loader._provider_config(ProviderKind.STT, row, "stt", credentials)
        fallback = loader._optional_provider_config(ProviderKind.STT, row, "sttf", credentials)

        assert primary.api_key == "primary-key"
        assert fallback is not None
        assert fallback.api_key == "fallback-key"

    def test_the_chain_keeps_its_order(self) -> None:
        """Fallback before local: the order is the policy."""
        loader = CallConfigLoader()
        fallback_id, local_id = uuid.uuid4(), uuid.uuid4()
        row = version_row(
            sttf_slug="openai_compatible",
            sttf_model="hosted-model",
            sttf_provider_id=fallback_id,
            sttl_slug="openai_compatible",
            sttl_model="local-model",
            sttl_base_url="http://speech.internal/v1",
            sttl_provider_id=local_id,
        )

        chain = loader._chain(
            (
                loader._optional_provider_config(ProviderKind.STT, row, "sttf", {}),
                loader._optional_provider_config(ProviderKind.STT, row, "sttl", {}),
            )
        )

        assert [c.model for c in chain] == ["hosted-model", "local-model"]

    def test_a_gap_does_not_shift_the_order(self) -> None:
        """Local configured with no fallback stays a one-entry chain."""
        loader = CallConfigLoader()
        row = version_row(
            sttl_slug="openai_compatible",
            sttl_model="local-model",
            sttl_provider_id=uuid.uuid4(),
        )

        chain = loader._chain(
            (
                loader._optional_provider_config(ProviderKind.STT, row, "sttf", {}),
                loader._optional_provider_config(ProviderKind.STT, row, "sttl", {}),
            )
        )

        assert [c.model for c in chain] == ["local-model"]

    def test_a_credentialless_tier_is_still_resolved(self) -> None:
        """A self-hosted endpoint authenticates by reachability (spec 25)."""
        loader = CallConfigLoader()
        row = version_row(
            ttsl_slug="openai_compatible",
            ttsl_model="Kokoro",
            ttsl_base_url="http://speech.internal/v1",
            ttsl_provider_id=uuid.uuid4(),
        )

        config = loader._optional_provider_config(
            ProviderKind.TTS, row, "ttsl", {}, voice_id="af_heart"
        )

        assert config is not None
        assert config.api_key is None
        assert config.base_url == "http://speech.internal/v1"
        assert config.voice_id == "af_heart"


class TestTheQueryReadsEveryTier:
    """The SQL is the other half: a column the loader wants and the query
    does not select fails with a ``KeyError`` at call setup, which is the
    single worst place for it."""

    def test_every_tier_prefix_appears_in_the_version_query(self) -> None:
        from worker.config_loader import _VERSION_SQL

        sql = str(_VERSION_SQL)
        for prefix in ("sttf", "llmf", "ttsf", "sttl", "ttsl"):
            assert f"{prefix}_slug" in sql, prefix
            assert f"{prefix}_model" in sql, prefix
            assert f"{prefix}_provider_id" in sql, prefix

    def test_the_query_resolves_the_adapter_not_the_row_name(self) -> None:
        """``providers.slug`` names the row; ``adapter`` selects the code.

        Asserted because building from the slug is what made a hosted and a
        self-hosted endpoint mutually exclusive in the catalog.
        """
        from worker.config_loader import _VERSION_SQL

        sql = str(_VERSION_SQL)
        assert "COALESCE(stt_p.adapter, stt_p.slug)" in sql
        assert "COALESCE(ttsl_p.adapter, ttsl_p.slug)" in sql

    def test_credentials_are_loaded_for_every_tier(self) -> None:
        """A fallback with no key is not a fallback."""
        import inspect

        source = inspect.getsource(CallConfigLoader._load_credentials)
        for key in (
            "sttf_provider_id",
            "llmf_provider_id",
            "ttsf_provider_id",
            "sttl_provider_id",
            "ttsl_provider_id",
        ):
            assert key in source, key
