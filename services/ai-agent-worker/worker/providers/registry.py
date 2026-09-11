"""Provider adapter registry (spec 24, 25).

Maps a ``(kind, slug)`` pair from the database to the adapter that implements
it. This is the only place that knows which vendors exist: everything else
depends on the interfaces in :mod:`worker.providers.base`, so adding a
provider is a new module plus one entry here — no changes to the pipeline
(spec 25: "additional providers later without redesigning the platform").
"""

from __future__ import annotations

from collections.abc import Callable

from shared.models import ProviderKind
from worker.providers.base import (
    BaseProvider,
    LLMProvider,
    ProviderConfig,
    ProviderUnavailableError,
    STTProvider,
    TTSProvider,
)

Builder = Callable[[ProviderConfig], BaseProvider]


def _registry() -> dict[tuple[ProviderKind, str], Builder]:
    """Build the registry.

    Imports happen inside the function so a broken or missing optional
    dependency in one adapter cannot stop the worker from starting and serving
    calls with the others (spec 55: a provider failure must not take down the
    pool).
    """
    from worker.providers.llm import echo as llm_echo
    from worker.providers.llm import openai_compatible as llm_openai
    from worker.providers.stt import openai_compatible as stt_openai
    from worker.providers.tts import elevenlabs as tts_elevenlabs
    from worker.providers.tts import openai_compatible as tts_openai

    return {
        (ProviderKind.STT, stt_openai.OpenAICompatibleSTT.slug): stt_openai.build,
        (ProviderKind.LLM, llm_openai.OpenAICompatibleLLM.slug): llm_openai.build,
        (ProviderKind.LLM, llm_echo.EchoLLM.slug): llm_echo.build,
        (ProviderKind.TTS, tts_openai.OpenAICompatibleTTS.slug): tts_openai.build,
        (ProviderKind.TTS, tts_elevenlabs.ElevenLabsTTS.slug): tts_elevenlabs.build,
    }


def available_slugs(kind: ProviderKind | None = None) -> list[str]:
    """Adapter slugs this build supports, for validation before publish (spec 63)."""
    return sorted({slug for (k, slug) in _registry() if kind is None or k is kind})


def build_provider(config: ProviderConfig) -> BaseProvider:
    """Instantiate the adapter for ``config``.

    Raises :class:`ProviderUnavailableError` when the database names a provider
    this build has no adapter for — which happens after a rollback, or when a
    catalog row is added before the adapter ships. Failing loudly beats
    silently substituting a different vendor.
    """
    registry = _registry()
    key = (config.kind, config.provider)

    builder = registry.get(key)
    if builder is None:
        supported = available_slugs(config.kind)
        raise ProviderUnavailableError(
            f"no {config.kind} adapter for provider {config.provider!r}; "
            f"this build supports: {supported}",
            provider=config.provider,
        )

    return builder(config)


def build_stt(config: ProviderConfig) -> STTProvider:
    provider = build_provider(config)
    assert isinstance(provider, STTProvider)  # noqa: S101 - registry key guarantees kind
    return provider


def build_llm(config: ProviderConfig) -> LLMProvider:
    provider = build_provider(config)
    assert isinstance(provider, LLMProvider)  # noqa: S101
    return provider


def build_tts(config: ProviderConfig) -> TTSProvider:
    provider = build_provider(config)
    assert isinstance(provider, TTSProvider)  # noqa: S101
    return provider
