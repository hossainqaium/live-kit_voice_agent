"""OpenAI-compatible language model adapter (spec 24, 25).

Covers OpenAI, plus any server exposing ``/v1/chat/completions`` — ollama,
vLLM, LM Studio, llama.cpp. Spec 25 requires local/self-hosted models, and one
code path for all of them means switching is a configuration change.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from livekit.plugins import openai as lk_openai

from worker.providers.base import Completion, LLMProvider, Message, ProviderConfig, ToolCall

_NO_CREDENTIAL_PLACEHOLDER = "not-required"


class OpenAICompatibleLLM(LLMProvider):
    """Chat completions over the OpenAI API shape."""

    slug = "openai_compatible"

    def build_livekit_component(self) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.config.model or "gpt-4o-mini",
            "base_url": self.config.base_url,
            "api_key": self.config.api_key or _NO_CREDENTIAL_PLACEHOLDER,
        }
        # Only pass temperature when configured. Some self-hosted servers
        # reject an explicit null, and others have a better default than ours.
        if self.config.temperature is not None:
            kwargs["temperature"] = self.config.temperature
        return lk_openai.LLM(**kwargs)

    async def generate(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[Completion]:
        """Stream a completion.

        Used by tests and non-realtime callers; the voice pipeline consumes the
        LiveKit component directly so token streaming is not double-buffered
        (spec 28, 56).
        """
        from livekit.agents import llm as lk_llm

        component = self.build_livekit_component()

        chat_ctx = lk_llm.ChatContext()
        for message in messages:
            chat_ctx.add_message(role=message.role, content=message.content)

        accumulated_tools: list[ToolCall] = []
        async with component.chat(chat_ctx=chat_ctx) as stream:
            async for chunk in stream:
                delta = getattr(chunk, "delta", None)
                if delta is None:
                    continue

                if getattr(delta, "tool_calls", None):
                    for call in delta.tool_calls:
                        accumulated_tools.append(
                            ToolCall(
                                id=getattr(call, "call_id", "") or "",
                                name=getattr(call, "name", "") or "",
                                arguments=getattr(call, "arguments", None) or {},
                            )
                        )

                if getattr(delta, "content", None):
                    yield Completion(text=delta.content)

        yield Completion(tool_calls=tuple(accumulated_tools), finished=True)


def build(config: ProviderConfig) -> OpenAICompatibleLLM:
    return OpenAICompatibleLLM(config)
