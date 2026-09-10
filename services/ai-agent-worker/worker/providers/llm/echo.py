"""Deterministic stand-in LLM for pipeline verification.

**Not a production provider.** It exists for two jobs an external model cannot
do well:

* proving the audio path — SIP, LiveKit, STT, TTS, barge-in — without an API
  key or network dependency, so a pipeline failure is unambiguous rather than
  entangled with provider latency
* load testing at 1,000 concurrent calls (spec 71) without paying for a
  million tokens, when the thing under test is worker and media capacity

It is registered under its own provider slug and is refused outside
development, because an agent that silently answers with canned text would be
worse than one that fails loudly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from worker.providers.base import (
    Completion,
    LLMProvider,
    Message,
    ProviderConfig,
    ProviderUnavailableError,
)


class EchoLLM(LLMProvider):
    """Replies with a fixed acknowledgement plus what it heard."""

    slug = "echo_dev"

    def build_livekit_component(self) -> Any:
        # Imported lazily so the module stays importable where the LiveKit
        # agents package is not installed, such as a unit-test-only image.
        from livekit.agents import llm as lk_llm
        from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

        class _EchoStream(lk_llm.LLMStream):
            async def _run(self) -> None:
                last_user = ""
                for item in reversed(self._chat_ctx.items):
                    if getattr(item, "role", None) == "user":
                        content = getattr(item, "content", None)
                        last_user = (
                            " ".join(str(c) for c in content)
                            if isinstance(content, list)
                            else str(content or "")
                        )
                        break

                reply = (
                    f"You said: {last_user}. This is the development echo model."
                    if last_user
                    else "Hello. This is the development echo model."
                )

                self._event_ch.send_nowait(
                    lk_llm.ChatChunk(
                        id="echo-0",
                        delta=lk_llm.ChoiceDelta(role="assistant", content=reply),
                    )
                )

        class _EchoLLM(lk_llm.LLM):
            def chat(self, *, chat_ctx, tools=None, conn_options=None, **kwargs):
                # conn_options must be a real APIConnectOptions: the base
                # LLMStream reads .max_retry from it, so None raises
                # AttributeError on the first turn. That failed quietly for a
                # while, because the greeting is spoken by session.say()
                # rather than by the model — the call connected and sounded
                # correct right up until someone actually spoke.
                return _EchoStream(
                    self,
                    chat_ctx=chat_ctx,
                    tools=tools or [],
                    conn_options=conn_options or DEFAULT_API_CONNECT_OPTIONS,
                )

        return _EchoLLM()

    async def generate(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[Completion]:
        last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        reply = (
            f"You said: {last_user}. This is the development echo model."
            if last_user
            else "Hello. This is the development echo model."
        )
        yield Completion(text=reply)
        yield Completion(finished=True)


def build(config: ProviderConfig) -> EchoLLM:
    """Construct the echo model, refusing to do so outside development.

    Checked here rather than only at configuration time, because a provider
    row could be seeded into a staging database by a copied fixture and this
    is the last point where it can be stopped.
    """
    import os

    if os.getenv("ENVIRONMENT", "development") != "development":
        raise ProviderUnavailableError(
            "the echo model is a development stand-in and must not serve real calls",
            provider="echo_dev",
        )
    return EchoLLM(config)
