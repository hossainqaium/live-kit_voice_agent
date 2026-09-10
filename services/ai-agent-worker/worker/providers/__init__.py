"""Provider adapters (spec 24, 25).

Provider-specific code lives only in the ``stt``, ``llm`` and ``tts``
subpackages. Everything else depends on the interfaces in :mod:`.base`.
"""

from .base import (
    AudioChunk,
    BaseProvider,
    Completion,
    LLMProvider,
    Message,
    ProviderAuthError,
    ProviderConfig,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    STTProvider,
    ToolCall,
    Transcript,
    TTSProvider,
)

__all__ = [
    "AudioChunk",
    "BaseProvider",
    "Completion",
    "LLMProvider",
    "Message",
    "ProviderAuthError",
    "ProviderConfig",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "STTProvider",
    "TTSProvider",
    "ToolCall",
    "Transcript",
]
