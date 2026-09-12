"""Repeated-measurement latency harness (Plan 2b.8, spec 76).

Single-call latency on a contended host is noise — transcription of the same
utterance has already been seen at 1661 ms and 11832 ms minutes apart. This
module repeats a stage probe, then reports p50 / p95 / min / max **and** the
realtime factor:

    realtime x = audio_duration / p50(elapsed)

Below 1.0 the stage is slower than a caller can speak, so a conversation
falls progressively further behind. That is the figure that showed
``faster-whisper-tiny`` on this host cannot hold two concurrent calls.

The 10-concurrent-call run is the same harness with ``concurrency=10``. It is
not the Phase 8 SIP load test.

Stdlib only — importing this module must not pull Prometheus.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import struct
import sys
import time
import uuid
import wave
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

Probe = Callable[[], "Sample"]

# Matches the isolated STT measurement in Plan §12.1.
DEFAULT_AUDIO_SECONDS = 3.6
DEFAULT_REPEATS = 15
DEFAULT_CONCURRENCY = (1, 2, 10)
DEFAULT_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class Sample:
    """One probe attempt."""

    stage: str
    elapsed_s: float
    ok: bool
    audio_s: float | None = None
    error: str | None = None


@dataclass(frozen=True)
class StageSummary:
    """Aggregated repeats at one concurrency level."""

    stage: str
    concurrency: int
    n: int
    failures: int
    p50_s: float | None
    p95_s: float | None
    min_s: float | None
    max_s: float | None
    mean_s: float | None
    realtime_factor: float | None
    holds_conversation: bool | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def percentile(values: Sequence[float], p: float) -> float:
    """Linear-interpolated percentile.

    ``p`` is in ``[0, 100]``. One sample returns that sample; an empty
    sequence is an error rather than a silent NaN.
    """
    if not values:
        raise ValueError("percentile of an empty sample")
    if not 0.0 <= p <= 100.0:
        raise ValueError("percentile must be in [0, 100]")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (p / 100.0) * (len(ordered) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(ordered[lo])
    frac = rank - lo
    return float(ordered[lo]) * (1.0 - frac) + float(ordered[hi]) * frac


def realtime_factor(audio_s: float, elapsed_s: float) -> float:
    """Audio duration over wall time. Below 1.0 the stage cannot keep up."""
    if audio_s < 0:
        raise ValueError("audio duration cannot be negative")
    if elapsed_s <= 0:
        raise ValueError("elapsed time must be positive")
    return audio_s / elapsed_s


def summarise(
    samples: Sequence[Sample],
    *,
    concurrency: int,
    audio_s: float | None = None,
) -> StageSummary:
    """Fold samples into the table row Plan §12.1 used."""
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    ok = [row for row in samples if row.ok]
    times = [row.elapsed_s for row in ok]
    stage = samples[0].stage if samples else "unknown"
    if not times:
        return StageSummary(
            stage=stage,
            concurrency=concurrency,
            n=len(samples),
            failures=len(samples),
            p50_s=None,
            p95_s=None,
            min_s=None,
            max_s=None,
            mean_s=None,
            realtime_factor=None,
            holds_conversation=None,
        )

    p50 = percentile(times, 50)
    duration = audio_s
    if duration is None:
        known = [row.audio_s for row in ok if row.audio_s is not None]
        duration = known[0] if known else None
    rtf = realtime_factor(duration, p50) if duration is not None else None
    return StageSummary(
        stage=stage,
        concurrency=concurrency,
        n=len(samples),
        failures=len(samples) - len(ok),
        p50_s=p50,
        p95_s=percentile(times, 95),
        min_s=min(times),
        max_s=max(times),
        mean_s=sum(times) / len(times),
        realtime_factor=rtf,
        holds_conversation=None if rtf is None else rtf >= 1.0,
    )


def run_probes(probe: Probe, *, repeats: int, concurrency: int, stage: str) -> list[Sample]:
    """Fire ``repeats`` probes with at most ``concurrency`` in flight."""
    if repeats < 1:
        raise ValueError("repeats must be >= 1")
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")

    def _wrapped() -> Sample:
        started = time.perf_counter()
        try:
            return probe()
        except Exception as exc:
            return Sample(
                stage=stage,
                elapsed_s=time.perf_counter() - started,
                ok=False,
                error=str(exc),
            )

    samples: list[Sample] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_wrapped) for _ in range(repeats)]
        for future in as_completed(futures):
            samples.append(future.result())
    return samples


def sine_wav_bytes(
    *,
    duration_s: float,
    sample_rate: int = 16_000,
    freq: float = 440.0,
) -> bytes:
    """A mono 16-bit PCM WAV of known duration, for probes that need a file."""
    if duration_s <= 0:
        raise ValueError("duration must be positive")
    if sample_rate <= 0:
        raise ValueError("sample rate must be positive")
    n_frames = int(duration_s * sample_rate)
    frames = bytearray()
    two_pi_f = 2.0 * math.pi * freq
    for i in range(n_frames):
        sample = int(32767 * 0.2 * math.sin(two_pi_f * i / sample_rate))
        frames += struct.pack("<h", sample)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(frames)
    return buf.getvalue()


def wav_duration_s(data: bytes) -> float:
    with wave.open(io.BytesIO(data), "rb") as wav:
        rate = wav.getframerate()
        if rate <= 0:
            raise ValueError("WAV has a zero sample rate")
        return wav.getnframes() / float(rate)


def parse_replay(spec: str) -> tuple[str, float | None, list[float]]:
    """Parse ``stage:audio_s:t1,t2,...`` or ``stage::t1,t2`` (no audio)."""
    parts = spec.split(":", 2)
    if len(parts) != 3 or not parts[0] or not parts[2]:
        raise ValueError(
            f"replay spec must be stage:audio_s:t1,t2,... or stage::t1,t2 — got {spec!r}"
        )
    stage, audio_raw, times_raw = parts
    audio_s = float(audio_raw) if audio_raw else None
    times = [float(item.strip()) for item in times_raw.split(",") if item.strip()]
    if not times:
        raise ValueError("replay spec has no times")
    if any(t <= 0 for t in times):
        raise ValueError("replay times must be positive")
    return stage, audio_s, times


def samples_from_replay(spec: str) -> tuple[list[Sample], float | None]:
    stage, audio_s, times = parse_replay(spec)
    return (
        [Sample(stage=stage, elapsed_s=t, ok=True, audio_s=audio_s) for t in times],
        audio_s,
    )


def format_table(summaries: Sequence[StageSummary]) -> str:
    """Human-readable table matching the Plan §12.1 layout."""
    if not summaries:
        return "(no measurements)"
    lines = [
        f"{'stage':<8} {'x':>3} {'n':>4} {'fail':>4} "
        f"{'p50':>10} {'p95':>10} {'min':>10} {'max':>10} "
        f"{'rtf':>6} {'holds':>6}"
    ]
    lines.append("-" * len(lines[0]))
    for row in summaries:
        rtf = "—" if row.realtime_factor is None else f"{row.realtime_factor:.1f}"
        if row.holds_conversation is None:
            holds = "n/a"
        else:
            holds = "yes" if row.holds_conversation else "NO"
        lines.append(
            f"{row.stage:<8} {row.concurrency:>3} {row.n:>4} {row.failures:>4} "
            f"{_ms(row.p50_s):>10} {_ms(row.p95_s):>10} "
            f"{_ms(row.min_s):>10} {_ms(row.max_s):>10} "
            f"{rtf:>6} {holds:>6}"
        )
    return "\n".join(lines)


def report_payload(
    summaries: Sequence[StageSummary], *, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runs": [row.as_dict() for row in summaries],
    }
    if extra:
        payload.update(extra)
    return payload


def _ms(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 1000:.0f}ms"


# --------------------------------------------------------------------------- #
# Live probes (OpenAI-compatible HTTP). URLs are operator-supplied.
# --------------------------------------------------------------------------- #


def _auth_headers(api_key: str | None) -> dict[str, str]:
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


def _urlopen(request: Request, *, timeout: float) -> Any:
    return urlopen(request, timeout=timeout)


def _join(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


def _multipart(
    fields: dict[str, str],
    files: dict[str, tuple[str, bytes, str]],
) -> tuple[bytes, str]:
    boundary = "----VoiceMeasure" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()
        )
    for name, (filename, data, content_type) in files.items():
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode()
            + data
            + b"\r\n"
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def probe_stt(
    *,
    base_url: str,
    model: str,
    wav_bytes: bytes,
    audio_s: float,
    api_key: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Sample:
    body, content_type = _multipart(
        {"model": model},
        {"file": ("utterance.wav", wav_bytes, "audio/wav")},
    )
    request = Request(
        _join(base_url, "/audio/transcriptions"),
        data=body,
        method="POST",
        headers={**_auth_headers(api_key), "Content-Type": content_type},
    )
    started = time.perf_counter()
    try:
        with _urlopen(request, timeout=timeout) as response:
            response.read()
            status = getattr(response, "status", 200)
    except HTTPError as exc:
        return Sample(
            stage="stt",
            elapsed_s=time.perf_counter() - started,
            ok=False,
            audio_s=audio_s,
            error=f"HTTP {exc.code}",
        )
    except (URLError, TimeoutError, OSError) as exc:
        return Sample(
            stage="stt",
            elapsed_s=time.perf_counter() - started,
            ok=False,
            audio_s=audio_s,
            error=str(exc),
        )
    elapsed = time.perf_counter() - started
    return Sample(stage="stt", elapsed_s=elapsed, ok=status < 400, audio_s=audio_s)


def probe_llm(
    *,
    base_url: str,
    model: str,
    prompt: str = "Reply with the single word ok.",
    api_key: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Sample:
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 8,
            "stream": True,
        }
    ).encode()
    request = Request(
        _join(base_url, "/chat/completions"),
        data=payload,
        method="POST",
        headers={
            **_auth_headers(api_key),
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )
    started = time.perf_counter()
    first_byte_at: float | None = None
    try:
        with _urlopen(request, timeout=timeout) as response:
            while True:
                chunk = response.read(64)
                if not chunk:
                    break
                if first_byte_at is None:
                    first_byte_at = time.perf_counter()
                    break
    except HTTPError as exc:
        return Sample(
            stage="llm",
            elapsed_s=time.perf_counter() - started,
            ok=False,
            error=f"HTTP {exc.code}",
        )
    except (URLError, TimeoutError, OSError) as exc:
        return Sample(
            stage="llm",
            elapsed_s=time.perf_counter() - started,
            ok=False,
            error=str(exc),
        )
    elapsed = (first_byte_at or time.perf_counter()) - started
    return Sample(stage="llm", elapsed_s=elapsed, ok=first_byte_at is not None)


def probe_tts(
    *,
    base_url: str,
    model: str,
    voice: str = "alloy",
    text: str = "Hello, this is a latency probe.",
    api_key: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Sample:
    payload = json.dumps(
        {"model": model, "input": text, "voice": voice, "response_format": "wav"}
    ).encode()
    request = Request(
        _join(base_url, "/audio/speech"),
        data=payload,
        method="POST",
        headers={**_auth_headers(api_key), "Content-Type": "application/json"},
    )
    started = time.perf_counter()
    first_byte_at: float | None = None
    body = bytearray()
    try:
        with _urlopen(request, timeout=timeout) as response:
            while True:
                chunk = response.read(4096)
                if not chunk:
                    break
                if first_byte_at is None:
                    first_byte_at = time.perf_counter()
                body.extend(chunk)
    except HTTPError as exc:
        return Sample(
            stage="tts",
            elapsed_s=time.perf_counter() - started,
            ok=False,
            error=f"HTTP {exc.code}",
        )
    except (URLError, TimeoutError, OSError) as exc:
        return Sample(
            stage="tts",
            elapsed_s=time.perf_counter() - started,
            ok=False,
            error=str(exc),
        )
    elapsed = (first_byte_at or time.perf_counter()) - started
    audio_s: float | None = None
    if body[:4] == b"RIFF":
        try:
            audio_s = wav_duration_s(bytes(body))
        except (wave.Error, ValueError):
            audio_s = None
    return Sample(
        stage="tts",
        elapsed_s=elapsed,
        ok=first_byte_at is not None,
        audio_s=audio_s,
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_concurrency(raw: str) -> list[int]:
    values = [int(part.strip()) for part in raw.split(",") if part.strip()]
    if not values or any(v < 1 for v in values):
        raise argparse.ArgumentTypeError("concurrency must be positive integers")
    return values


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Repeat STT / LLM / TTS probes and report p50/p95 plus realtime factor. "
            "Plan 2b.8 — not the Phase 8 SIP load test."
        )
    )
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument(
        "--concurrency",
        type=_parse_concurrency,
        default=list(DEFAULT_CONCURRENCY),
        help="Comma-separated in-flight counts, default 1,2,10",
    )
    parser.add_argument("--audio-seconds", type=float, default=DEFAULT_AUDIO_SECONDS)
    parser.add_argument("--audio", help="WAV file to upload instead of a generated tone")
    parser.add_argument("--stt-url", help="OpenAI-compatible base URL (…/v1)")
    parser.add_argument("--stt-model", default="Systran/faster-whisper-tiny")
    parser.add_argument("--llm-url")
    parser.add_argument("--llm-model", default="gpt-4o-mini")
    parser.add_argument("--tts-url")
    parser.add_argument("--tts-model", default="tts-1")
    parser.add_argument("--tts-voice", default="alloy")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument(
        "--replay",
        action="append",
        default=[],
        help="stage:audio_s:t1,t2,...  (repeatable; no network)",
    )
    parser.add_argument("--json", dest="json_path", help="Write the report JSON to this path")
    return parser


def _load_wav(path: str | None, duration_s: float) -> tuple[bytes, float]:
    if not path:
        data = sine_wav_bytes(duration_s=duration_s)
        return data, wav_duration_s(data)
    with open(path, "rb") as handle:
        data = handle.read()
    return data, wav_duration_s(data)


def collect_summaries(args: argparse.Namespace) -> list[StageSummary]:
    summaries: list[StageSummary] = []

    for spec in args.replay:
        samples, audio_s = samples_from_replay(spec)
        summaries.append(summarise(samples, concurrency=1, audio_s=audio_s))

    wav: bytes | None = None
    audio_s = args.audio_seconds
    if args.stt_url or args.tts_url:
        wav, audio_s = _load_wav(args.audio, args.audio_seconds)

    for concurrency in args.concurrency:
        if args.stt_url:
            if wav is None:
                raise RuntimeError("STT probe requested but no WAV was loaded")
            summaries.append(
                summarise(
                    run_probes(
                        lambda: probe_stt(
                            base_url=args.stt_url,
                            model=args.stt_model,
                            wav_bytes=wav,
                            audio_s=audio_s,
                            api_key=args.api_key,
                            timeout=args.timeout,
                        ),
                        repeats=args.repeats,
                        concurrency=concurrency,
                        stage="stt",
                    ),
                    concurrency=concurrency,
                    audio_s=audio_s,
                )
            )
        if args.llm_url:
            summaries.append(
                summarise(
                    run_probes(
                        lambda: probe_llm(
                            base_url=args.llm_url,
                            model=args.llm_model,
                            api_key=args.api_key,
                            timeout=args.timeout,
                        ),
                        repeats=args.repeats,
                        concurrency=concurrency,
                        stage="llm",
                    ),
                    concurrency=concurrency,
                )
            )
        if args.tts_url:
            summaries.append(
                summarise(
                    run_probes(
                        lambda: probe_tts(
                            base_url=args.tts_url,
                            model=args.tts_model,
                            voice=args.tts_voice,
                            api_key=args.api_key,
                            timeout=args.timeout,
                        ),
                        repeats=args.repeats,
                        concurrency=concurrency,
                        stage="tts",
                    ),
                    concurrency=concurrency,
                )
            )
    return summaries


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.repeats < 1:
        parser.error("--repeats must be >= 1")
    if not args.replay and not (args.stt_url or args.llm_url or args.tts_url):
        parser.error("provide --replay and/or at least one of --stt-url / --llm-url / --tts-url")

    summaries = collect_summaries(args)
    table = format_table(summaries)
    print(table)

    payload = report_payload(
        summaries,
        extra={
            "repeats": args.repeats,
            "concurrency": args.concurrency,
            "audio_seconds": args.audio_seconds,
        },
    )
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")

    if not summaries:
        return 2
    if all(row.failures == row.n for row in summaries):
        return 2
    if any(row.holds_conversation is False for row in summaries):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
