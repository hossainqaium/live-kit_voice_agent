"""Seven-field transfer summary and spoken rendering (spec 36, TS-1–TS-5)."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any

from shared.logging import get_logger
from worker.summariser import _request_summary

logger = get_logger(__name__)

SUMMARY_FIELDS: tuple[str, ...] = (
    "customer",
    "reason",
    "summary",
    "actions_taken",
    "order_information",
    "sentiment",
    "required_next_action",
)

DEFAULT_SPOKEN_TEMPLATE = (
    "Transfer from the AI agent. Customer: {{customer}}. Reason: {{reason}}. "
    "{{summary}} Actions taken: {{actions_taken}}. Order information: "
    "{{order_information}}. Sentiment: {{sentiment}}. Required next action: "
    "{{required_next_action}}."
)

FALLBACK_WHISPER = (
    "Transfer from the AI agent. Caller {caller}. Please accept the call."
)

_SUMMARY_TIMEOUT_S = 8.0

_FIELD_TOKEN = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")

_SUMMARY_PROMPT = (
    "You extract a warm-transfer briefing from a phone call. "
    "Reply with JSON only, no markdown, with exactly these keys: "
    "customer, reason, summary, actions_taken, order_information, sentiment, "
    "required_next_action. "
    "Write in {language}. Keep every value short enough to speak. "
    "If a field is unknown, use the string unknown."
)


@dataclass(frozen=True, slots=True)
class TransferSummary:
    """One generated briefing, with both renderings."""

    fields: dict[str, str]
    spoken: str
    generated: bool
    sip_headers: dict[str, str] = field(default_factory=dict)


def fallback_fields(
    *,
    caller_number: str | None,
    reason: str | None = None,
) -> dict[str, str]:
    """Minimal seven-field record used when generation fails (TS-4)."""
    return {
        "customer": caller_number or "unknown",
        "reason": (reason or "caller requested a human agent").strip() or "unknown",
        "summary": "The AI could not produce a full briefing. Please take the call.",
        "actions_taken": "none recorded",
        "order_information": "none",
        "sentiment": "unknown",
        "required_next_action": "assist the caller",
    }


def fallback_whisper(*, caller_number: str | None, reason: str | None = None) -> str:
    """Spoken line that must still play when generation fails (TS-4)."""
    caller = caller_number or "unknown"
    text = FALLBACK_WHISPER.format(caller=caller)
    if reason:
        text = f"{text} Reason: {reason.strip()}."
    return text


def render_spoken(template: str | None, fields: dict[str, str]) -> str:
    """Substitute ``{{field}}`` tokens from the seven-field record (TS-1)."""
    source = (template or "").strip() or DEFAULT_SPOKEN_TEMPLATE

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return str(fields.get(key) or "unknown")

    spoken = _FIELD_TOKEN.sub(_replace, source).strip()
    return spoken or fallback_whisper(caller_number=fields.get("customer"))


def structured_headers(fields: dict[str, str]) -> dict[str, str]:
    """SIP headers for PBXs that accept a screen-pop payload (TS-5)."""
    headers: dict[str, str] = {}
    for key in SUMMARY_FIELDS:
        value = str(fields.get(key) or "").strip()
        if not value:
            continue
        header = "X-Transfer-" + "-".join(part.title() for part in key.split("_"))
        headers[header] = value[:200]
    return headers


def parse_fields(raw: str | None, *, fallback: dict[str, str]) -> dict[str, str]:
    """Read a JSON object into the seven required keys."""
    if not raw:
        return dict(fallback)
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return dict(fallback)
    if not isinstance(payload, dict):
        return dict(fallback)
    parsed = dict(fallback)
    for key in SUMMARY_FIELDS:
        value = payload.get(key)
        if value is None:
            continue
        cleaned = str(value).strip()
        if cleaned:
            parsed[key] = cleaned
    return parsed


async def generate_transfer_summary(
    context: Any,
    dialogue: str,
    *,
    reason: str | None = None,
    timeout_seconds: float = _SUMMARY_TIMEOUT_S,
) -> TransferSummary:
    """Produce the seven-field record. Failure still returns a briefing (TS-4)."""
    fallback = fallback_fields(
        caller_number=getattr(context, "caller_number", None),
        reason=reason,
    )
    policy = getattr(context, "transfer_policy", None)
    template = getattr(policy, "summary_template", None) if policy is not None else None
    language = getattr(context, "language", None) or "en"

    generated = False
    fields = dict(fallback)
    if dialogue.strip():
        try:
            raw = await asyncio.wait_for(
                _request_summary(
                    context,
                    dialogue,
                    system_prompt=_SUMMARY_PROMPT.format(language=language),
                ),
                timeout=timeout_seconds,
            )
            fields = parse_fields(raw, fallback=fallback)
            generated = raw is not None
        except Exception:
            logger.warning("transfer_summary_generation_failed", exc_info=True)
            fields = dict(fallback)
            generated = False

    spoken = render_spoken(template, fields)
    if not generated:
        spoken = fallback_whisper(
            caller_number=getattr(context, "caller_number", None),
            reason=reason,
        )
    return TransferSummary(
        fields=fields,
        spoken=spoken,
        generated=generated,
        sip_headers=structured_headers(fields),
    )
