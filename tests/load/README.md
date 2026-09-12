# Latency and load harnesses

## Repeated measurement (Plan 2b.8) — built

A single call's latency on a contended host is noise. This harness repeats a
stage probe and reports p50 / p95 / min / max **and** the realtime factor
(`audio_duration / p50`). Below 1.0 the stage cannot keep up with speech.

```bash
# Isolated STT, then 2- and 10-wide concurrent (the 10-call run is here, not Phase 8)
make measure-latency STT_URL=http://127.0.0.1:8000/v1 REPEATS=15 CONCURRENCY=1,2,10

# Recompute a row from already-collected times (no network)
python3 scripts/measure_latency.py --replay stt:3.6:0.773,2.655,3.024
```

Implementation: `services/shared/shared/measure.py`.
Tests: `services/shared/tests/test_measure.py`.

## Progressive SIP load (Phase 8) — not built

`make load-test` is still the placeholder. That harness drives real SIP calls
through the production path at 10 → 1000 concurrent. Do not treat 2b.8's
`--concurrency 10` as a capacity claim.
