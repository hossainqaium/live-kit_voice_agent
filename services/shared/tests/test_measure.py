"""Repeated-measurement harness (Plan 2b.8).

Asserts the thing the harness exists for: realtime factor is audio duration
over p50 elapsed, and a factor below 1.0 is reported as unable to hold a
conversation. The numbers below are the Plan §12.1 isolated STT table.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared.measure import (
    Sample,
    format_table,
    main,
    parse_replay,
    percentile,
    realtime_factor,
    report_payload,
    run_probes,
    sine_wav_bytes,
    stt_placement,
    summarise,
    wav_duration_s,
)

# Plan §12.1: 3.6 s utterance against faster-whisper-tiny.
_AUDIO_S = 3.6
_SEQUENTIAL_P50 = 0.773
_CONCURRENT_2_P50 = 3.964


class TestPercentile:
    def test_single_value(self) -> None:
        assert percentile([4.0], 95) == 4.0

    def test_median_of_odd_set(self) -> None:
        assert percentile([1.0, 2.0, 3.0], 50) == 2.0

    def test_interpolates(self) -> None:
        assert percentile([0.0, 10.0], 50) == 5.0

    def test_empty_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            percentile([], 50)

    def test_rejects_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="0, 100"):
            percentile([1.0], 140)


class TestRealtimeFactor:
    def test_plan_sequential_is_about_4_6(self) -> None:
        rtf = realtime_factor(_AUDIO_S, _SEQUENTIAL_P50)
        assert 4.6 <= rtf <= 4.7

    def test_plan_two_concurrent_is_below_one(self) -> None:
        rtf = realtime_factor(_AUDIO_S, _CONCURRENT_2_P50)
        assert 0.90 <= rtf <= 0.91

    def test_rejects_non_positive_elapsed(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            realtime_factor(1.0, 0.0)


class TestSummarise:
    def test_plan_sequential_row(self) -> None:
        # 15 identical p50-shaped samples — enough to pin the factor.
        samples = [
            Sample(stage="stt", elapsed_s=_SEQUENTIAL_P50, ok=True, audio_s=_AUDIO_S)
            for _ in range(15)
        ]
        row = summarise(samples, concurrency=1, audio_s=_AUDIO_S)
        assert row.p50_s == pytest.approx(_SEQUENTIAL_P50)
        assert row.realtime_factor == pytest.approx(_AUDIO_S / _SEQUENTIAL_P50)
        assert row.holds_conversation is True
        assert row.failures == 0

    def test_plan_two_concurrent_cannot_hold(self) -> None:
        samples = [
            Sample(stage="stt", elapsed_s=_CONCURRENT_2_P50, ok=True, audio_s=_AUDIO_S)
            for _ in range(15)
        ]
        row = summarise(samples, concurrency=2, audio_s=_AUDIO_S)
        assert row.realtime_factor is not None
        assert row.realtime_factor < 1.0
        assert row.holds_conversation is False

    def test_llm_has_no_realtime_factor(self) -> None:
        samples = [Sample(stage="llm", elapsed_s=0.4, ok=True) for _ in range(5)]
        row = summarise(samples, concurrency=1)
        assert row.realtime_factor is None
        assert row.holds_conversation is None

    def test_all_failures_leave_percentiles_empty(self) -> None:
        samples = [Sample(stage="stt", elapsed_s=1.0, ok=False, error="down")]
        row = summarise(samples, concurrency=1, audio_s=_AUDIO_S)
        assert row.p50_s is None
        assert row.failures == 1
        assert row.holds_conversation is None


class TestRunProbes:
    def test_respects_repeat_count(self) -> None:
        samples = run_probes(
            lambda: Sample(stage="stt", elapsed_s=0.1, ok=True, audio_s=1.0),
            repeats=7,
            concurrency=3,
            stage="stt",
        )
        assert len(samples) == 7
        assert all(row.ok for row in samples)

    def test_probe_exception_becomes_a_failed_sample(self) -> None:
        def boom() -> Sample:
            raise RuntimeError("endpoint refused")

        samples = run_probes(boom, repeats=2, concurrency=1, stage="stt")
        assert len(samples) == 2
        assert all(not row.ok for row in samples)
        assert "refused" in (samples[0].error or "")


class TestWav:
    def test_generated_duration_matches_request(self) -> None:
        data = sine_wav_bytes(duration_s=1.25)
        assert wav_duration_s(data) == pytest.approx(1.25, abs=1 / 16_000)


class TestReplay:
    def test_parses_stage_audio_and_times(self) -> None:
        stage, audio_s, times = parse_replay("stt:3.6:0.773,2.655,3.024")
        assert stage == "stt"
        assert audio_s == 3.6
        assert times == [0.773, 2.655, 3.024]

    def test_allows_missing_audio_for_llm(self) -> None:
        stage, audio_s, times = parse_replay("llm::0.4,0.5")
        assert stage == "llm"
        assert audio_s is None
        assert times == [0.4, 0.5]

    def test_cli_replay_of_the_plan_table_exits_one(self, tmp_path: Path, capsys) -> None:
        """Two-concurrent RTF < 1 is the signal the harness exists to print."""
        report = tmp_path / "report.json"
        code = main(
            [
                "--replay",
                "stt:3.6:3.964,4.311,4.465",
                "--json",
                str(report),
            ]
        )
        assert code == 1
        payload = json.loads(report.read_text())
        run = payload["runs"][0]
        assert run["holds_conversation"] is False
        assert run["realtime_factor"] < 1.0
        assert "STT placement: hosted" in capsys.readouterr().out

    def test_cli_replay_sequential_exits_zero(self) -> None:
        code = main(["--replay", "stt:3.6:0.773,0.780,0.790"])
        assert code == 0


class TestSttPlacement:
    """Plan 2b.9: self-hosted STT is a hardware gate, not a preference."""

    def test_plan_two_concurrent_recommends_hosted(self) -> None:
        samples = [
            Sample(stage="stt", elapsed_s=_CONCURRENT_2_P50, ok=True, audio_s=_AUDIO_S)
            for _ in range(15)
        ]
        row = summarise(samples, concurrency=2, audio_s=_AUDIO_S)
        decision, reason = stt_placement([row])
        assert decision == "hosted"
        assert "realtime factor" in reason

    def test_sequential_p95_outside_the_window_recommends_hosted(self) -> None:
        # Plan §12.1 sequential p95 was 2655 ms — slower than the 2 s window.
        samples = [
            Sample(stage="stt", elapsed_s=2.655, ok=True, audio_s=_AUDIO_S) for _ in range(15)
        ]
        row = summarise(samples, concurrency=1, audio_s=_AUDIO_S)
        decision, reason = stt_placement([row])
        assert decision == "hosted"
        assert "p95" in reason

    def test_gpu_like_numbers_allow_self_hosted(self) -> None:
        sequential = summarise(
            [Sample(stage="stt", elapsed_s=0.35, ok=True, audio_s=_AUDIO_S) for _ in range(15)],
            concurrency=1,
            audio_s=_AUDIO_S,
        )
        contended = summarise(
            [Sample(stage="stt", elapsed_s=0.80, ok=True, audio_s=_AUDIO_S) for _ in range(15)],
            concurrency=2,
            audio_s=_AUDIO_S,
        )
        decision, _reason = stt_placement([sequential, contended])
        assert decision == "self_hosted"
        assert sequential.p95_s is not None and sequential.p95_s <= 2.0
        assert contended.realtime_factor is not None and contended.realtime_factor >= 1.0


class TestReport:
    def test_table_includes_realtime_factor_column(self) -> None:
        row = summarise(
            [Sample(stage="stt", elapsed_s=0.773, ok=True, audio_s=3.6)],
            concurrency=1,
            audio_s=3.6,
        )
        table = format_table([row])
        assert "rtf" in table
        assert "holds" in table
        assert f"{row.realtime_factor:.1f}" in table

    def test_json_payload_lists_runs(self) -> None:
        row = summarise(
            [Sample(stage="stt", elapsed_s=0.8, ok=True, audio_s=3.6)],
            concurrency=1,
            audio_s=3.6,
        )
        payload = report_payload([row], extra={"repeats": 1})
        assert payload["repeats"] == 1
        assert payload["runs"][0]["stage"] == "stt"
        assert "generated_at" in payload
