"""Shared helpers for LiveKit-plugin adapters (spec 24, 25).

Each vendor module still owns how it constructs the plugin. Streaming a
component's output into the platform types is the same every time, and
duplicating it is how a later adapter forgets to yield ``AudioChunk``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from worker.providers.base import AudioChunk, Completion, ToolCall, Transcript


def optional_kwargs(**values: Any) -> dict[str, Any]:
    """Drop Nones so a plugin can apply its own defaults."""
    return {key: value for key, value in values.items() if value is not None}


async def stream_tts(
    component: Any, text: str | AsyncIterator[str]
) -> AsyncIterator[AudioChunk]:
    if isinstance(text, str):
        stream = component.synthesize(text)
    else:
        stream = component.stream()

        async def _feed() -> None:
            async for piece in text:
                stream.push_text(piece)
            stream.end_input()

        asyncio.create_task(_feed())  # noqa: RUF006 — lifetime bound to stream

    async for event in stream:
        frame = getattr(event, "frame", None)
        if frame is None:
            continue
        yield AudioChunk(
            data=bytes(frame.data),
            sample_rate=frame.sample_rate,
            num_channels=frame.num_channels,
        )


async def recognize_stt(
    component: Any,
    audio: AsyncIterator[bytes],
    *,
    sample_rate: int,
    language: str | None,
) -> AsyncIterator[Transcript]:
    chunks = [chunk async for chunk in audio]
    if not chunks:
        return
    from livekit import rtc

    payload = b"".join(chunks)
    frame = rtc.AudioFrame(
        data=payload,
        sample_rate=sample_rate,
        num_channels=1,
        samples_per_channel=max(1, len(payload) // 2),
    )
    event = await component.recognize(buffer=frame, language=language)
    for alternative in getattr(event, "alternatives", ()) or ():
        yield Transcript(
            text=getattr(alternative, "text", "") or "",
            is_final=True,
            confidence=getattr(alternative, "confidence", None),
            language=getattr(alternative, "language", None) or language,
        )


async def stream_llm(component: Any, messages: list[Any]) -> AsyncIterator[Completion]:
    from livekit.agents import llm as lk_llm

    chat_ctx = lk_llm.ChatContext()
    for message in messages:
        chat_ctx.add_message(role=message.role, content=message.content)

    accumulated: list[ToolCall] = []
    async with component.chat(chat_ctx=chat_ctx) as stream:
        async for chunk in stream:
            delta = getattr(chunk, "delta", None)
            if delta is None:
                continue
            if getattr(delta, "tool_calls", None):
                for call in delta.tool_calls:
                    accumulated.append(
                        ToolCall(
                            id=getattr(call, "call_id", "") or "",
                            name=getattr(call, "name", "") or "",
                            arguments=dict(getattr(call, "arguments", None) or {}),
                        )
                    )
            text = getattr(delta, "content", None) or getattr(delta, "text", "") or ""
            if text:
                yield Completion(text=str(text))
    yield Completion(text="", tool_calls=tuple(accumulated), finished=True)
