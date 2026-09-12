"""Post-call conversation summarisation (spec 34, 2b.5).

After a call ends and every transcript segment has been flushed to the
database, this module reads them back, assembles the full dialogue, and asks
the same LLM that powered the call to produce a concise summary.  The
(summary, full_text) pair is returned to the caller, which stores it in
``call_transcripts``.

Principles
----------
* **Non-realtime.** Runs in the ``finally`` block after the session closes,
  never during the call.  Latency does not matter here.
* **Non-fatal.** A failure here never masks a call error or blocks the usage
  write.  The caller wraps this in ``try/except``.
* **Conditional.** Skipped when fewer than :data:`_MIN_SEGMENTS` turns were
  captured (caller hung up immediately) or when transcription was disabled.
* **Bounded.** Capped at :data:`_SUMMARY_MAX_TOKENS` so a 60-minute call does
  not produce an unusably long summary.
* **Customisable.** When ``context.transfer_policy.summary_template`` is set
  the tenant's own prompt is used, so a company can tailor the format and
  focus for warm-transfer whispers (Phase 6).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import openai as _openai
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.logging import get_logger
from worker.resilience import make_resilient_client

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from worker.config_loader import CallContext

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Minimum number of captured segments before summary is attempted.
#: Fewer than this means the caller hung up after the greeting and there is
#: nothing worth summarising.
_MIN_SEGMENTS = 2

#: Token budget for the summary.  Generous enough for 5 dense sentences but
#: short enough to keep cost negligible relative to the call's own LLM usage.
_SUMMARY_MAX_TOKENS = 350

#: Low temperature: summaries should be factual and reproducible.
_SUMMARY_TEMPERATURE = 0.2

#: Default system prompt used when the tenant has not configured their own.
#: The sentinel ``NO_SUMMARY`` lets the model signal "not enough content"
#: without returning a hallucinated summary.
_DEFAULT_SYSTEM_PROMPT = (
    "You are a concise call-summary assistant. "
    "The user will supply the transcript of an inbound phone call between a "
    "caller and an AI voice agent. "
    "Produce a brief, factual summary (3-5 sentences maximum) covering: "
    "(1) the caller's main reason for calling, "
    "(2) key information exchanged, "
    "(3) the outcome or next step agreed. "
    "Do not include greetings, small talk, or meta-commentary. "
    "Write in the third person (e.g. 'The caller asked …'). "
    "If the transcript is too short or contains no meaningful content, "
    "respond with exactly the string: NO_SUMMARY"
)

_READ_SEGMENTS_SQL = text(
    """
    SELECT speaker, text
    FROM   call_transcript_segments
    WHERE  call_transcript_id = :transcript_id
    ORDER  BY sequence
    """
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_full_text(rows: list) -> str:
    """Convert ordered segment rows into a readable dialogue string."""
    lines: list[str] = []
    for row in rows:
        speaker = "Caller" if str(row.speaker) == "CALLER" else "Agent"
        lines.append(f"{speaker}: {row.text}")
    return "\n".join(lines)


async def _fetch_segments(session: AsyncSession, transcript_id: uuid.UUID) -> list:
    result = await session.execute(_READ_SEGMENTS_SQL, {"transcript_id": transcript_id})
    return result.fetchall()


async def _request_summary(
    context: "CallContext",
    dialogue: str,
    *,
    system_prompt: str | None = None,
) -> str | None:
    """Make one chat.completions call and return the summary text, or None.

    Builds a fresh ``AsyncOpenAI`` client each time.  The LiveKit LLM
    component is not available post-call, and a one-shot completion does not
    need the session machinery.
    """
    cfg = context.llm
    provider_key = cfg.base_url or cfg.provider or "default_llm"
    http_client = make_resilient_client(provider_key=provider_key, kind="llm")
    client = _openai.AsyncOpenAI(
        api_key=cfg.api_key or "not-required",
        base_url=cfg.base_url,
        http_client=http_client,
        max_retries=0,
    )
    model = cfg.model or "gpt-4o-mini"

    # An explicit prompt is used for mid-call rolling memory and the
    # warm-transfer JSON briefing. ``transfer_summary_template`` is a spoken
    # {{field}} string (TS-1), not an LLM prompt, so post-call summaries
    # always use the built-in instruction unless a caller overrides it.
    if system_prompt is None:
        system_prompt = _DEFAULT_SYSTEM_PROMPT

    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Transcript:\n{dialogue}"},
        ],
        max_tokens=_SUMMARY_MAX_TOKENS,
        temperature=_SUMMARY_TEMPERATURE,
    )
    raw = (resp.choices[0].message.content or "").strip()
    return raw if raw and raw != "NO_SUMMARY" else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def generate_summary(
    *,
    context: "CallContext",
    transcript_row_id: uuid.UUID,
    factory: "async_sessionmaker",
) -> tuple[str | None, str] | None:
    """Generate a post-call summary from the persisted transcript segments.

    Parameters
    ----------
    context:
        The completed call's context.  Supplies the LLM provider config and
        the tenant's optional ``summary_template``.
    transcript_row_id:
        UUID of the ``call_transcripts`` header row whose segments to read.
    factory:
        Process-wide ``async_sessionmaker`` for reading segments from the DB.

    Returns
    -------
    ``(summary, full_text)``
        When a summary was generated.  ``summary`` may be ``None`` if the LLM
        returned ``NO_SUMMARY`` (too little content); ``full_text`` is always
        the assembled dialogue string.
    ``None``
        When there are fewer than :data:`_MIN_SEGMENTS` segments — not worth
        an LLM call.

    Raises
    ------
    Any exception from the database read or the LLM call.  The caller is
    expected to wrap this in ``try/except`` and log non-fatally.
    """
    async with factory() as session:
        rows = await _fetch_segments(session, transcript_row_id)

    if len(rows) < _MIN_SEGMENTS:
        logger.debug(
            "transcript_summary_skipped",
            extra={"reason": "too_few_segments", "count": len(rows), "min": _MIN_SEGMENTS},
        )
        return None

    full_text = _build_full_text(rows)
    summary = await _request_summary(context, full_text)

    logger.info(
        "transcript_summary_generated" if summary else "transcript_summary_empty",
        extra={"chars": len(summary) if summary else 0, "segments": len(rows)},
    )

    return summary, full_text
