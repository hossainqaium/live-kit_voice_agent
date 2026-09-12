"""Mid-call summarisation for long conversations (spec 34, Plan 6.8).

LiveKit holds the raw turn list. After a configurable number of user turns
this module compresses the older ones into a rolling summary that is injected
into the agent's instructions, so the model stays coherent without sending
the entire transcript every turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from worker.summariser import _request_summary

#: User turns between rolls. 8 spoken exchanges is long enough that the
#: context window starts to matter on a telephony model, short enough that
#: a 3-minute call still gets one compression.
ROLLING_TRIGGER_TURNS = 8

_ROLLING_PROMPT = (
    "You are compressing an in-progress phone call so the voice agent can "
    "keep answering. Write 4-8 short factual sentences covering: what the "
    "caller wants, facts already collected, and any promise or next step. "
    "Do not greet. Third person. If there is nothing worth keeping, respond "
    "with exactly: NO_SUMMARY"
)


@dataclass
class RollingMemory:
    """Per-call rolling summary state. Not frozen — it updates mid-call."""

    base_instructions: str
    summary: str = ""
    user_turns: int = 0
    last_rolled_at: int = 0
    recent_turns: list[str] = field(default_factory=list)

    def record_user_turn(self, text: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        self.user_turns += 1
        self.recent_turns.append(f"Caller: {cleaned}")

    def record_agent_turn(self, text: str) -> None:
        cleaned = text.strip()
        if cleaned:
            self.recent_turns.append(f"Agent: {cleaned}")

    def should_roll(self) -> bool:
        return self.user_turns >= self.last_rolled_at + ROLLING_TRIGGER_TURNS

    def compose_instructions(self, *, retrieved: str = "") -> str:
        parts = [self.base_instructions]
        if retrieved:
            parts.append(retrieved)
        if self.summary:
            parts.append(f"Earlier in this call:\n{self.summary}")
        return "\n\n".join(parts)


async def roll_summary(context: object, memory: RollingMemory) -> str | None:
    """Compress ``recent_turns`` into ``memory.summary`` using the call LLM."""
    if not memory.recent_turns:
        return memory.summary or None
    dialogue = "\n".join(memory.recent_turns)
    if memory.summary:
        dialogue = f"Previous summary:\n{memory.summary}\n\nNew turns:\n{dialogue}"

    summary = await _request_summary(
        context,  # type: ignore[arg-type]
        dialogue,
        system_prompt=_ROLLING_PROMPT,
    )

    if summary:
        memory.summary = summary
        memory.last_rolled_at = memory.user_turns
        memory.recent_turns = memory.recent_turns[-4:]
    return memory.summary or None
