"""Anthropic Claude language-model adapter (spec 24, 25)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from worker.providers._livekit import optional_kwargs, stream_llm
from worker.providers.base import Completion, LLMProvider, Message, ProviderConfig


class AnthropicLLM(LLMProvider):
    slug = "anthropic"

    def build_livekit_component(self) -> Any:
        from livekit.plugins import anthropic as lk_anthropic

        return lk_anthropic.LLM(
            **optional_kwargs(
                model=self.config.model or "claude-sonnet-4-6",
                api_key=self.config.api_key,
                base_url=self.config.base_url,
                temperature=self.config.temperature,
            )
        )

    async def generate(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[Completion]:
        async for item in stream_llm(self.build_livekit_component(), messages):
            yield item


def build(config: ProviderConfig) -> AnthropicLLM:
    return AnthropicLLM(config)
