# Implementation Plan
## Multi-Tenant AI Voice Agent Platform (LiveKit)

| | |
|---|---|
| **Document** | LiveKitVoiceAgentPlan.md |
| **Version** | 1.0 (Draft) |
| **Date** | 2026-09-11 |
| **Derived from** | `LiveKitVoiceAgentPRD.md` · Master Requirements Specification §1–§80 |
| **Phase structure** | Fixed by spec §76 (Phases 1–8) |

**Companion documents:** [`LiveKitVoiceAgentPRD.md`](./LiveKitVoiceAgentPRD.md) ·
[`LiveKitVoiceAgentREADME.md`](./LiveKitVoiceAgentREADME.md) ·
[`AIProviders.md`](./AIProviders.md)

---

## 1. How This Plan Is Structured

The eight phases below are **prescribed by the specification (§76)**, not invented here. Their
order is deliberate: prove one real call works before building a platform around it, and prove
tenant isolation before building tenant-facing features.

Each phase states:

- **Goal** — the single outcome that defines the phase
- **Scope** — work items with spec references
- **Deliverables** — artifacts that must exist when the phase closes
- **Exit criteria** — objectively checkable conditions
- **Explicitly out of scope** — what must *not* be built yet

### The troubleshooting log is part of the deliverable

§12 records, per phase, every problem actually hit while building — symptom
first, then cause, then fix. It exists so the next environment costs hours
instead of days.

**Closing a phase includes adding its entries.** Write them as the problems are
solved, not afterwards: the symptom is the searchable part, and it is the first
thing forgotten once the cause is understood. An entry is worth writing
whenever the symptom did not point at the cause — which was true of nearly
every problem in Phases 0 and 1.

### Standing rule for every phase [§80]

Before implementing any major component, write down its **Architecture, Database Model,
API Contract, Security Model, Error Handling, Tests, and Scaling Considerations**. A component
without those seven is not ready to build.

### Cross-cutting work that starts in Phase 1 and never stops [§80]

| Concern | Why it cannot be deferred |
|---|---|
| Structured JSON logging + `call_id` correlation | Retrofitting correlation IDs across six services is far more expensive than starting with them. [§43, §58] |
| `/health` and `/ready` on every service | Kubernetes readiness semantics shape service startup design. [§59] |
| Containerization | Every service is Docker-first from its first commit. [§4] |
| Automated tests per module | Test coverage is per-module, not a phase. [§70, §80] |
| No hard-coded tenant configuration | An architectural invariant, checked in review. [§9] |
| Secrets never in code/logs/prompts/frontend | Checked in review and CI. [§54] |

---

## 2. Estimation Basis

Durations are **engineering-effort ranges for a small team** (roughly 2 backend, 1 frontend,
1 DevOps/SRE), not commitments. They assume LiveKit self-hosting, provider API keys available,
and a reachable test PBX.

| Phase | Focus | Effort |
|---|---|---|
| 0 | Foundations (pre-work inside Phase 1) | 1 week |
| 1 | Basic Call | 2–3 weeks |
| 2 | Realtime Quality | 2–3 weeks |
| 3 | Multi-Tenancy | 2–3 weeks |
| 4 | Configuration Platform | 4–6 weeks |
| 5 | LiveKit Control Plane | 3–4 weeks |
| 6 | Advanced AI | 4–6 weeks |
| 7 | Production Infrastructure | 3–5 weeks |
| 8 | Capacity Testing | 2–4 weeks |
| | **Total** | **~23–35 weeks** |

Phase 8 duration depends heavily on how much remediation the load tests trigger. Treat the
upper bound as realistic, not pessimistic.

---

## 3. Phase 0 — Foundations

Not a numbered spec phase; it is the first week of Phase 1. It exists so Phase 1 does not
stall on scaffolding.

### Scope

1. Repository skeleton per README §3 — `services/`, `deploy/`, `tests/`, `docs/`.
2. `deploy/docker-compose.yml` with the ten minimum services: `postgresql`, `redis`, `livekit`,
   `livekit-sip`, `configuration-api`, `ai-agent-worker`, `frontend`, `minio`, `prometheus`,
   `grafana`. [§4]
3. `shared/logging` — JSON logger emitting `service`, `timestamp`, `event`, and, where
   available, `tenant_id`, `call_id`, `room_id`, `agent_id`, `agent_version_id`. [§58]
4. `/health` + `/ready` scaffolding for API and worker. [§59]
5. Alembic wired up; empty initial migration. [§68]
6. CI: lint, type-check, unit tests, container build, secret scan. [§54, §70]
7. `.env.example` covering every variable in README §6.

### Exit criteria

- `docker compose up` brings all ten services to healthy.
- `curl /health` and `curl /ready` succeed on API and worker.
- CI is green on an empty test suite; the secret scanner fails a deliberately planted fake key.
- Every problem hit during setup is recorded in §12.0, symptom first.

---

## 4. Phase 1 — Basic Call [§76]

### Goal

> **One complete successful call.** `PBX → LiveKit SIP → LiveKit → AI Agent → PBX`

Everything else in this plan is worthless until this works end to end.

### Scope

| # | Work item | Spec |
|---|---|---|
| 1.1 | Full database schema design — all tables from §68, with `tenant_id` on every tenant-owned table from the first migration | §6, §68, §80 |
| 1.2 | Configuration API skeleton at `/api/v1`, OpenAPI generation on | §67 |
| 1.3 | LiveKit integration module — admin client, SIP trunk create/read, dispatch rule create/read | §11, §80 |
| 1.4 | One SIP trunk and one **long-lived** dispatch rule, created via LiveKit API (not CLI, not per-call) | §11, §21 |
| 1.5 | AI Agent Worker skeleton registered for agent dispatch; accepts a job, joins the room | §22, §23 |
| 1.6 | Minimum viable pipeline: one STT + one LLM + one TTS provider behind the adapter interfaces | §24, §28 |
| 1.7 | Provider abstraction defined properly now — `STTProvider.transcribe`, `LLMProvider.generate`, `TTSProvider.synthesize` | §24 |
| 1.8 | Worker loads agent configuration from PostgreSQL at call start and caches it for the call | §23, §45 |
| 1.9 | `call_id` generated at call start; propagated through SIP, LiveKit, agent, STT, LLM, TTS, DB, logs | §43 |
| 1.10 | `calls` table write path with the full minimum record from §41 | §41 |
| 1.11 | Call state machine: `NEW → RINGING → ANSWERED → AI_CONNECTED → IN_PROGRESS → COMPLETED`, plus failure states | §42 |
| 1.12 | Test PBX wired to LiveKit SIP — satisfied by an existing FreeSWITCH/FusionPBX rather than a container, with a dedicated additive dialplan and no change to existing routes | §14 |
| 1.13 | Provider credential storage: encrypted at rest with key versioning, read from stdin so a key never enters a process listing or shell history | §53, §54 |
| 1.14 | CLI to store credentials and repoint an agent's STT/LLM/TTS — provider choice is configuration, not code | §9, §25 |

### Deliverables

- Component design docs (the seven headings) for: Configuration API, LiveKit module, AI Agent Worker.
- Troubleshooting entries in §12 for every problem hit (done — see §12.1).
- Initial Alembic migration containing the complete §68 schema.
- A recorded walkthrough of one successful call, with the log trace filtered by `call_id`.

### Exit criteria

- A real phone call into the test PBX is answered by the AI agent, holds a short spoken
  exchange, and hangs up cleanly.
- A single `grep` on `call_id` returns the full lifecycle across every service.
- The `calls` row is complete and correct: timestamps, duration, status, hangup reason.
- The dispatch rule is reused across at least ten consecutive calls — none created per call.
- No tenant-specific value appears anywhere in worker source code.
- Each configured provider is verified by exercising the adapter and seeing real
  output — a connected call and an absent error are not evidence that the model
  replied.

### Out of scope

Multi-tenancy enforcement, RBAC, the web UI, tools, RAG, transfer, recording, Kubernetes.
Provider adapters exist but only one implementation per kind.

### Risks

| Risk | Mitigation |
|---|---|
| SIP/NAT/RTP problems consume the whole phase | Budget a dedicated media-path spike in week 1; test on a host with a public IP before debugging code |
| Codec mismatch between PBX and LiveKit SIP | Pin a known-good codec (e.g. PCMU/Opus) explicitly on the trunk before broad testing |
| Temptation to hard-code the single test tenant | The `tenant_id`-everywhere schema (1.1) lands before any worker logic |

---

## 5. Phase 2 — Realtime Quality [§76]

### Goal

Ten concurrent calls that actually sound good.

### Scope

| # | Work item | Spec |
|---|---|---|
| 2.1 | VAD and turn detection in the pipeline | §28, §29 |
| 2.2 | Barge-in: `caller speech → detect interruption → cancel TTS → STT → LLM` | §29 |
| 2.3 | Streaming everywhere it is supported — STT input, LLM tokens, TTS audio | §28 |
| 2.4 | Silence timeout and maximum call duration enforcement | §18, §29 |
| 2.5 | Voice latency instrumentation: STT latency, LLM first-token, TTS first-audio, time to first response, time to first audio, end-to-end | §56 |
| 2.6 | Call recording to object storage (S3/MinIO); PostgreSQL stores metadata only | §39 |
| 2.7 | Transcription storage — speaker (`Caller`/`AI`/`Human Agent`), timestamp, text, confidence | §40 |
| 2.8 | Conversation memory per call: `conversation_id`, `call_id`, `tenant_id`, `messages`, `tool_calls`, `tool_results`, `summary` — isolated per call | §34 |
| 2.9 | Grafana dashboard for the voice-latency metrics | §56, §57 |
| 2.10 | 10-concurrent-call test harness | §76 |

### Deliverables

- Troubleshooting entries in §12.
- Latency dashboard with per-stage breakdown.
- Recording + transcript artifacts for the 10-call test.
- Barge-in test cases in the integration suite.

### Exit criteria

- 10 concurrent calls sustained with acceptable audio, no dropped calls.
- Interruption reliably cancels in-flight TTS; the caller is not talked over.
- Recording and transcript exist for every call in the test run; no audio blob is in PostgreSQL.
- Every latency metric in §56 is queryable in Prometheus and charted in Grafana.
- Conversation state from one call never appears in another.

### Out of scope

Multi-tenancy, tools, RAG, transfer, autoscaling.

### Risks

| Risk | Mitigation |
|---|---|
| Barge-in feels laggy or fires on background noise | Tune VAD thresholds against recorded samples; make them agent-configurable per §18 |
| Latency budget blown by a non-streaming provider | Measure per stage from day one (2.5); reject providers that cannot stream for the default path |

---

---

## 5b. Carry-Over Phases

Work left behind when a phase moved on. Collected here as real phases with
their own scope and exit criteria, rather than as notes inside the phase that
dropped them, so that nothing depends on someone remembering a paragraph.

Two of these items are **not missing features**. They are settings that exist
in the schema, are loaded at runtime, and then do nothing. That is worse than
not offering them, because the agent builder will present them and a tenant
will believe them. They are marked **defect** below and should be treated as
correctness work.

### Phase 2b — Realtime Quality Completion

**Goal:** the call policies the data model already advertises actually take
effect, and a call's audio is retrievable afterwards.

| # | Item | Kind | Spec |
|---|---|---|---|
| ~~2b.1~~ | **Enforce `silence_timeout_seconds`.** ~~Loaded from the agent version today and ignored.~~ **Done (2026-09-12):** Mapped to `AgentSession(user_away_timeout=...)`. A value of None or 0 keeps the session alive indefinitely (the default). Tests: `test_call_policy.py::TestBuildSessionPassesPolicy`. | ~~defect~~ | §18, §29 |
| ~~2b.2~~ | **Enforce `max_call_duration_seconds`.** ~~Loaded and ignored.~~ **Done (2026-09-12):** `_max_duration_watchdog` races against `_wait_for_disconnect`; when it fires, it calls `ctx.delete_room()` and the call ends with `HangupReason.MAX_DURATION`. Tests: `test_call_policy.py::TestMaxDurationWatchdog`. | ~~defect~~ | §18 |
| ~~2b.3~~ | **Honour `recording_enabled`.** ~~Start LiveKit egress to object storage, write `call_recordings` metadata, leave the audio out of PostgreSQL. Loaded and ignored today.~~ **Done (2026-09-12):** `_start_recording` / `_stop_recording` in `entrypoint.py` use `livekit.api.LiveKitAPI` + S3 settings to start a room-composite egress before the greeting and stop it in the `finally` block. Non-fatal: errors are logged but never propagate to kill the call. **`call_recordings` DB row complete (2b.3b, 2026-09-12):** `write_recording_row()` in `config_loader.py` INSERTs the metadata row after the egress stops and updates `calls.recording_id`. `_start_recording` now returns a `_RecordingInfo` dataclass with `egress_id`, `bucket`, `object_key`, `started_at`. No migration needed — table shipped in the baseline schema. | ~~defect~~ | §39 |
| ~~2b.4~~ | **Map `interruption_enabled` and `interruption_min_words`** onto the session's own options. **Done (2026-09-12):** Forwarded as `AgentSession(allow_interruptions=..., min_interruption_words=...)`. Tests: `test_call_policy.py`. | ~~mapping~~ | §29 |
| ~~2b.5~~ | ~~Conversation summarisation into `call_transcripts.summary`, which is also what the warm-transfer whisper reads.~~ **Done (2026-09-12):** `worker/summariser.py` — `generate_summary()` reads completed segments from the DB after `observer.aclose()`, assembles the full dialogue, and calls the call's own LLM (OpenAI-compatible) with a configurable prompt. Result is `(summary, full_text)` written to `call_transcripts` via `write_transcript_summary()` in `config_loader.py`. Skipped when fewer than 2 segments; handles `NO_SUMMARY` sentinel; uses `transfer_policy.summary_template` when set (Phase 6 warm-transfer whisper reads the same column). Non-fatal in the `finally` block — failure never suppresses the usage write. 25 new tests in `tests/test_summariser.py`. `call_transcripts.summary` and `full_text` already existed — no migration needed. | ~~feature~~ | §34, §36 |
| ~~2b.6~~ | ~~Grafana dashboard for the six voice-latency metrics. They are exposed and scraped; nothing charts them.~~ **Done (2026-09-12):** `deploy/grafana/dashboards/voice-latency.json` — 18-panel Grafana 10 dashboard auto-provisioned by the existing file provider. Panels: Time-to-First-Audio p50/p95, End-to-End p50/p95, STT/LLM/TTS per-stage histograms by provider, Time-to-First-Response, Active Calls (Gauge), Worker Utilization (Gauge), Call Rate (completed/failed), Circuit Breaker state, Provider fallbacks, All-6-metrics overlay. Template variables: `datasource`, `tenant_id`, `agent_id`. All latency panels use `unit="s"`. Structural validation test in `tests/test_grafana_dashboard.py` (skips inside Docker; 19 tests locally). | ~~feature~~ | §56, §57 |
| ~~2b.7~~ | ~~Barge-in and endpointing verified against real speech.~~ **Done (2026-09-12):** The failing call (`call_20260911T063710`) committed the turn at LiveKit's default `min_delay=0.5` s before a 1103 ms transcript landed. `worker/endpointing.py` now sets a fixed window of **2.0 / 6.0 s** on `AgentSession(turn_handling=...)` — min exceeds hosted STT, max exceeds the 2581 ms EOU. Interruption policy moves into the same `turn_handling` dict (top-level kwargs are ignored once it is set). Session start logs `endpointing_window`. Fitted to hosted STT, not the 11832 ms local-contention sample. Tests: `tests/test_endpointing.py`, `test_call_policy.py`. | ~~defect~~ | §29, §56 |
| ~~2b.9~~ | ~~STT latency. Reopened on evidence.~~ **Done (2026-09-12):** Self-hosted STT is a **hardware gate**, not a code change. `shared.measure.stt_placement` recommends `self_hosted` only when (1) realtime factor at ×2 is ≥ 1.0 and (2) sequential p95 is ≤ 2.0 s (the 2b.7 window). The Plan §12.1 table fails both (`×2` rtf 0.9, sequential p95 2655 ms). **This development host uses hosted `gpt-4o-mini-transcribe`** until STT has a GPU or a dedicated box; a larger Whisper model on this CPU would be worse. Cold start (2.6–3.6 s) needs a keep-warm ping if you do self-host. `tiny` is still too inaccurate on 8 kHz. CLI prints `STT placement: hosted|self_hosted`. Tests: `test_measure.py::TestSttPlacement`. | ~~defect~~ | §25, §56 |
| ~~2b.10~~ | **Browser test call from the console.** **Done**: a green-handset **Call Test** button on Phone Numbers, for any active number with an agent. `POST /browser-test/session` mints a scoped join token — the only endpoint in the platform that issues a credential, so its four guards are asserted rather than reviewed. Getting audio working took a stale `NODE_IP`, concurrent participant waits, no STUN, and a server upgrade; see §12.1. | ~~tooling~~ | §70 |
| ~~2b.11~~ | ~~Synchronous work on the agent event loop.~~ **Done (2026-09-12):** Silero VAD was already prewarmed (1020 → 628 ms). The remainder was LiveKit's default `TurnDetector` loading its local EOT model inside `AgentSession.start` — timed at **537 ms** cold, 0 ms warm. That model cannot commit a turn earlier than our 2 s `min_delay` (2b.7), so `turn_handling` now sets `turn_detection="vad"` and the EOT path is never entered. `_run_call` logs `session_built` / `session_started` elapsed_ms so the next measurement is not a guess. Tests: `test_endpointing.py::test_uses_vad_not_the_local_eot_model`. | ~~defect~~ | §28, §56 |
| ~~2b.8~~ | ~~Repeated-measurement harness, then the 10-concurrent-call one. Must report **realtime factor** per stage and not only latency.~~ **Done (2026-09-12):** `shared.measure` repeats an STT / LLM / TTS probe and reports p50 / p95 / min / max **and** `realtime× = audio_duration / p50`. Below 1.0 the stage cannot keep up with speech — the same definition as Plan §12.1. Default concurrency ladder is `1,2,10` (the 10-wide run is here; it is not the Phase 8 SIP load test). CLI: `make measure-latency STT_URL=…` or `--replay stt:3.6:0.773,2.655`. Tests: `services/shared/tests/test_measure.py` assert the Plan §12.1 sequential row holds a conversation and the ×2 row does not. | ~~feature~~ | §76 |

**Most of 2b.1, 2b.2 and 2b.4 is a mapping exercise.** `AgentSession` already
accepts `user_away_timeout`, `min_interruption_duration`,
`min_interruption_words` and `allow_interruptions`; the work is passing
configuration into them. Only maximum call duration needs new logic.

**Why 2b.10 comes before 2b.7 rather than after it.** The endpointing defect can
only be reproduced today by placing a real PBX call, so every measurement carries
SIP, NAT and codec behaviour along with the pipeline's own — and the two are not
separable from the outside. A browser client removes all of it and makes one
conversational turn reproducible in seconds. The reason it does not already work
is sound and must be preserved: SIP attributes carry the DID, the DID is the only
route to a tenant, and a call row with a null tenant breaks §6. So the branch has
to resolve a DID it is handed rather than skip tenant resolution, and it has to
default to off. It is listed as tooling, not a feature — nothing ships to a
tenant — but it is the cheapest thing in this phase and it shortens everything
after it.

**Exit criteria**

- A call with a 10-second silence timeout ends by itself after 10 seconds of
  silence, with `hangup_reason = SILENCE_TIMEOUT`.
- A call exceeding its maximum duration ends with `hangup_reason = MAX_DURATION`.
- An agent with recording enabled produces an object in storage and a
  `call_recordings` row; PostgreSQL holds no audio.
- No setting in `agent_versions` is loaded at runtime and then ignored — the
  property worth asserting in a test, so this class of defect cannot recur.
- The latency dashboard renders p50 and p95 per stage.
- One conversational turn can be exercised from a browser against a real
  agent version's configuration, with no PBX in the path, and the
  development-only branch that allows it is off by default.

### Phase 4b — Configuration Completion

**Goal:** the §77 criterion holds — a tenant administrator configures
everything through the UI, with no CLI step left in the path.

Both consoles are built and every §60/§61 section has a working screen.
**Phase 4b is complete (2026-09-12)** — every carry-over item in the table
below is done, including the SIP Configuration Wizard (4b.1).

| # | Item | Kind | Spec |
|---|---|---|---|
| ~~4b.4~~ | **Tenant provider credentials through the API and console.** **Done**: `GET /catalog`, `PUT /catalog/credentials`, `POST /catalog/credentials/{id}/verify`, `DELETE /catalog/credentials/{id}`, with the key entry and a live *Test connection* inside the agent builder. Write-only field, Fernet at rest, never returned by any response model — asserted across every route, not just the credential ones. | ~~feature~~ | §26, §62, §77 |
| ~~4b.7~~ | **Model selection in the agent builder.** **Done**: STT, LLM, TTS and voice are selectable per tier, cascading provider → model → voice, with the catalog's default pre-selected. The columns existed from the start and the builder showed them read-only, pointing the operator at the CLI. | ~~gap~~ | §18, §62 |
| ~~4b.8~~ | **Fallback and local tiers (§55 fallback provider).** **Done**: primary → fallback → local per stage, resolved by the worker into LiveKit's own `FallbackAdapter`. Required separating `providers.adapter` from `providers.slug` — see 4b.9. | ~~feature~~ | §55, §25 |
| ~~4b.9~~ | **A provider's adapter is no longer its name.** **Done**: `providers.slug` is unique per kind and was also the worker's registry key, so a catalog could hold only one row per protocol per kind — "OpenAI-compatible hosted" and "OpenAI-compatible self-hosted" could not coexist, which made the local tier unconfigurable. `adapter` is now a separate nullable column falling back to the slug. | ~~defect~~ | §24, §25 |
| ~~4b.10~~ | **The rest of §55: timeout, retry, exponential backoff, circuit breaker.** ~~`worker/resilience/__init__.py` is still a docstring and nothing else. Fallback alone covers the case where a provider fails outright; it does not cover one that is slow, nor stop a dying provider being retried on every call.~~ **Done (2026-09-12):** `worker/resilience/__init__.py` now implements all three layers: (1) per-stage `httpx.Timeout` (STT read=30s, LLM read=120s, TTS read=30s) injected into all openai-compatible adapters via `openai.AsyncOpenAI(http_client=make_resilient_client(…))`; (2) per-provider-endpoint circuit breaker — 5 consecutive transport failures open the circuit (instant `ConnectError` to FallbackAdapter, no timeout wait), 60s recovery then HALF_OPEN probe; (3) `async_retry()` with jittered exponential backoff (base=1s, ×2, cap=30s) for non-realtime callers. `FallbackAdapter` on all three stages now sets `attempt_timeout` (STT 12s, LLM/TTS 10s) so slow providers fail over on latency, not only hard errors. | ~~gap~~ | §55 |
| ~~4b.3~~ | **Routing engine evaluation at call setup.** ~~Rules, business hours and the fallback chain are all configurable and none is consulted when a call arrives; the DID's directly assigned agent answers.~~ **Done (2026-09-12):** `worker/routing.py` evaluates all ACTIVE rules in priority / specificity order, checks business hours in the schedule's timezone including holiday overrides, routes to the secondary agent when closed with `SECONDARY_AGENT` closed_action, and raises `RoutingClosedError` (→ `NoRouteError`) for HANGUP/PBX_QUEUE/VOICEMAIL. `CallConfigLoader.load()` calls the engine before loading the version; backward-compatible — tenants with no routing rules still use `inbound_agent_id`. Fallback chain: if the primary agent has no published version and the rule has a secondary agent, the secondary is used automatically. | ~~defect~~ | §20, §37, §38 |
| ~~4b.2~~ | ~~DID form fields for routing rule, business hours and fallback. The columns exist and the API accepts them; the form does not offer them.~~ **Done (2026-09-12):** Tenant Phone Numbers form (`services/frontend/app/phone-numbers/page.tsx`) now offers `routing_rule_id` (pin one rule, or evaluate all matches) and `business_hours_id`. Fallback is not a DID column — it lives on the routing rule; the form shows the pinned rule's fallback/closed action as a read-only hint. List table gained Routing and Hours columns. API: `_validate_references` now rejects cross-tenant rule/hours UUIDs; `_decorate` resolves `routing_rule_name` and `business_hours_name`. Tests: `tests/test_phone_numbers.py`. **Follow-up (2026-09-12):** Adding a DID with an agent assigned 500'd — `audit.snapshot` put raw UUIDs into JSONB. Snapshots are now JSON-safe. Create/update/delete/enable also enqueue a trunk sync so LiveKit accepts the new number (the wizard already did this). Files: `app/services/audit.py`, `app/api/v1/phone_numbers.py`. | ~~gap~~ | §17 |
| ~~4b.1~~ | ~~SIP Configuration Wizard — the 10 steps in §16 as a guided sequence. Every field is already reachable through the trunk and DID forms, so this is onboarding ergonomics, not capability.~~ **Done (2026-09-12):** Tenant page `/sip-wizard` walks the PRD §8.3 ten steps (Select PBX → SIP Configuration → Authentication → Codec → Security → Connection Test → Phone Number → AI Agent → Routing → Complete). Composes existing APIs only. Trunk is created at Security; DID create is followed by `POST /sip-trunks/{id}/sync` so LiveKit accepts the number. Wizard also exposes direction, codecs, DTMF and SRTP (API-ready but missing from the standalone trunk form). Nav: Telephony → SIP Setup Wizard. Tests: `tests/test_sip_wizard.py`. | ~~feature~~ | §16 |
| ~~4b.5~~ | ~~Voice preview. §27 lists Test among the voice actions; needs a synthesis endpoint and a stored sample.~~ **Done (2026-09-12):** `POST /platform/voices/{id}/test` synthesises via OpenAI-compatible `/audio/speech` or ElevenLabs REST, uploads MP3 to `voice-samples/{uuid}.mp3` in the recordings bucket, and sets `voices.sample_object_key` (column already existed — no migration). `GET .../sample` streams the file through the API so the browser never needs a MinIO URL. API key is request-only, never stored or audited (`voice.previewed`). Voice Library **Test** dialog: phrase, optional key, play stored sample. Tests: `tests/test_voice_preview.py`. | ~~feature~~ | §27 |
| ~~4b.6~~ | **ElevenLabs TTS adapter with streaming.** ~~Only the OpenAI-compatible adapter exists, and the catalog will happily offer a provider nothing can drive.~~ **Done (2026-09-12):** `worker/providers/tts/elevenlabs.py` wraps `livekit.plugins.elevenlabs.TTS` with slug `elevenlabs`, registered in the provider registry. Forwards `model`, `voice_id`, `api_key`, `base_url`, `language` (conditional kwargs); sets `inactivity_timeout=30` to close stuck WebSocket connections. Streaming text input supported via `stream()` + `push_text`. | ~~feature~~ | §26 |

**4b.3 was the most consequential open defect and is now closed (2026-09-12).**
The routing engine in `worker/routing.py` is the runtime half of the routing
UI that existed since Phase 4. Every routing rule, business-hours schedule, and
fallback chain configured in the console is now evaluated on every inbound call.
The DID's `routing_rule_id` pins a specific rule; otherwise all ACTIVE rules are
evaluated in priority / specificity order. PBX_QUEUE and VOICEMAIL closed
actions log a warning and reject the call until Phase 6 transfer is available.

**4b.7–4b.9 were built the other way round, deliberately.** The provider
controls went in with the worker change that reads them in the same commit: the
chain the builder writes is resolved by `_VERSION_SQL`, credentials are
decrypted per tier, and the tiers become a `FallbackAdapter`. A worker test
asserts each prefix appears in the query, because a tier the console writes and
the loader drops would be invisible until the primary failed — the worst
possible moment to discover it.

**4b.10 is now complete (2026-09-12).** §55 lists five things — the console
configures the fallback tier, and the worker now implements the remaining four:
per-request timeouts, circuit breaker, retry with exponential backoff, and
`FallbackAdapter` latency trip-wires. A provider that is slow or repeatedly
failing is handled without stalling the caller's turn.

**Exit criteria**

- A tenant administrator creates PBX → trunk → DID → agent → routing rule and
  receives a call **using only the console**, including entering the provider
  API key.
- A call arriving on a DID with a routing rule is answered by the agent the
  rule selects, not the DID's default, and the `calls` row records which rule
  matched.
- A call arriving outside a schedule's hours follows the closed action.
- A provider credential is never returned by any endpoint, and the audit trail
  records that one was set without recording its value.
- A tenant administrator selects STT, LLM, TTS and voice for an agent, stores
  the key for each provider, and confirms each one answers — without leaving
  the console. **Met.**
- An agent with a fallback tier keeps talking when its primary provider returns
  errors or becomes slow. Fallover on error and latency both met (4b.10 done).

### Phase 4c — AI Setup Section (CR-2) ✓ **COMPLETE**

**Goal:** a dedicated, first-class home for AI provider credentials, decoupled
from the agent builder, with all acceptance criteria in
[`AIProviders.md`](./AIProviders.md) met.

| # | Item | Kind | Spec |
|---|---|---|---|
| ~~4c.1~~ | ~~Add `EMBEDDING` to `ProviderKind` enum and run the Alembic migration.~~ **Done.** `shared/models/enums.py`; migration `a3f8c2e91d47` (no-op: `providers.kind` is VARCHAR, no native enum exists). | feature | §33, CR-2 |
| ~~4c.2~~ | ~~Seed `providers` rows for the `EMBEDDING` kind.~~ **Done.** Expanded to 29 providers across all 4 kinds (10 LLM · 7 STT · 7 TTS · 5 Embedding), all popular cloud and self-hosted providers included. | feature | §25, CR-2 |
| ~~4c.3~~ | ~~Implement `POST /catalog/credentials/verify-draft`.~~ **Done.** Key used for probe only, discarded immediately, not logged or audited. | feature | §54, CR-2 |
| ~~4c.4~~ | ~~Build the `/ai-setup` page with four tabs.~~ **Done.** Grouped dropdown (Cloud / Self-hosted), per-provider key hints and docs links, verify-before-save enforced. | feature | §62, CR-2 |
| ~~4c.5~~ | ~~Update the agent builder — filter to credentialed providers.~~ **Done.** Inline key-entry removed from `ProviderChain.tsx`; link to AI Setup shown when list is empty. | feature | §18, §62, CR-2 |
| ~~4c.6~~ | ~~Add `embedding_provider_id` / `embedding_model_id` to `agent_versions`.~~ **Done.** Migration `b7d4f1a02c58`; `EmbeddingPicker` in agent builder Capability tab (visible only when KB is selected). | feature | §33, CR-2 |
| ~~4c.7~~ | ~~Extend pre-publish validation for embedding fields.~~ **Done.** `_validate_version()` emits `severity=warning` when `knowledge_base_id` is set but `embedding_provider_id` is null. | feature | §63, CR-2 |
| ~~4c.8~~ | ~~Pre-populate the Endpoint URL field from the provider's canonical base URL.~~ **Done (2026-09-13).** `CatalogProvider` now exposes `default_base_url` for non-self-hosted providers; `AddProviderDialog` initialises `baseUrl` state from `provider.default_base_url` (cloud) or `PROVIDER_META.endpointPlaceholder` (self-hosted) on first render and on every provider change; the value is saved to `provider_credentials.base_url` on Add. Tests: `test_catalog_provider_exposes_default_base_url_for_cloud`, `test_catalog_provider_hides_default_base_url_for_self_hosted`. Files: `app/schemas/catalog.py`, `app/api/v1/catalog.py`, `lib/api.ts`, `app/ai-setup/page.tsx`. | fix | CR-2 |
| ~~4c.9~~ | ~~Fix agent publish blocked by missing voice when provider has no catalog voices.~~ **Done (2026-09-13).** Validation now queries available voices for the TTS provider before deciding severity: `voice_id = null` is only a hard error when voices exist in the catalog; otherwise it's a warning (agent publishes, worker uses provider default). Seeded 43 standard voices across OpenAI TTS, ElevenLabs, Cartesia, Deepgram Aura, Google TTS, LMNT, PlayHT so selections are available out of the box. Voice picker hint updated to explain missing voices. Tests: `test_voice_validation_checks_catalog_before_blocking`, `test_publishable_flag_is_driven_by_error_severity_only`. Files: `app/api/v1/agents.py`, `app/services/seed.py`, `components/ProviderChain.tsx`. | fix | §63, CR-2 |

**Implementation order:** 4c.1 → 4c.2 → 4c.3 (in parallel with 4c.1–4c.2) →
4c.4 → 4c.5 (depends on 4c.4) → 4c.6 → 4c.7 (depends on 4c.6) → 4c.8 → 4c.9.

**Exit criteria — all met**

- The AI Setup sidebar entry appears for roles with `agents.read`; all four
  tabs render with correct tables and empty states. **Met.**
- Adding a credential via the modal requires a successful Test Connection
  before the Add button becomes active. The credential appears in the table
  with a green verified tick immediately after saving. **Met.**
- Rotating a key clears `last_verified_at`; the row shows "Unverified" until
  tested again. **Met.**
- Deleting a credential succeeds immediately; the audit trail records the
  deletion with the key hint but not the key itself. **Met.**
- The agent builder's provider dropdowns show only credentialed or keyless
  providers; a link to AI Setup is shown when the list is empty. **Met.**
- A full API key is never returned by any endpoint, present in any log, or
  visible in the browser's network tab. **Met.**
- The Embedding tab is visible and states its Phase 6 dependency honestly. **Met.**

### Phase 3b — Isolation and Account Hardening

**Goal:** the isolation guarantee holds even for a query written outside the
repository, and accounts are manageable through the API rather than the CLI.

| # | Item | Kind | Spec |
|---|---|---|---|
| ~~3b.1~~ | ~~PostgreSQL Row Level Security on every tenant-owned table, as the second layer beneath `TenantRepository`.~~ **Done (2026-09-12):** Alembic migration `20260912_1200_row_level_security.py` enables `ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY` + a `tenant_isolation` policy on all 27 `TENANT_OWNED_TABLES`. Policy: row passes when `app.tenant_id` GUC is absent (platform routes see all) or when it matches the row's `tenant_id`. GUC set transaction-locally via `SELECT set_config('app.tenant_id', :tid, true)` in `get_tenant_scope` (wired once; covers all 27 tables automatically). Tests: `tests/test_rls.py` (18 tests — table-list parity, upgrade/downgrade SQL, policy semantics) + updated `TestTenantScope` in `tests/test_dependencies.py`. 404 passing. | ~~feature~~ | §7 |
| ~~3b.2~~ | User management endpoints — create, list, disable, assign roles. **Done**: `/users` for tenant users, `/platform/users` for platform staff, both with a console screen. | ~~feature~~ | §8, §61 |
| ~~3b.3~~ | Password reset. **Done**: `POST /users/{id}/password` and the platform equivalent, both revoking sessions in the same change — a reset that leaves old tokens working is not a reset. Self-service change by the signed-in user is still open (**3b.3a**). | ~~feature~~ | §53 |
| ~~3b.3a~~ | ~~Self-service password change, for a user rotating their own password without an administrator.~~ **Done (2026-09-12):** `POST /auth/me/password` accepts `{current_password, new_password}` (new must differ; 12–72 chars). `auth_service.change_own_password` verifies the current hash, writes the new bcrypt hash, and sets `tokens_valid_from` so every session including this one dies immediately. Audit action `user.password_changed` (no secret in the payload). Console: **Change password** in the Shell topbar (every role); after success the user is sent to login with a banner. Tests: `tests/test_password_change.py`. | ~~feature~~ | §53 |
| ~~3b.4~~ | Audit read endpoint. **Done**: `GET /platform/audit-logs`, filterable, with no write or delete route anywhere in the API. | ~~feature~~ | §69 |
| ~~3b.5~~ | ~~Audit coverage asserted across every mutating endpoint, rather than trusting that each one remembered.~~ **Done (2026-09-12):** Fixed real gap: `POST /catalog/credentials/{id}/verify` updated `last_verified_at` with no audit row — added `credential.verified` action (catalog.py). Structural coverage test in `tests/test_audit_coverage.py`: walks every `POST/PUT/PATCH/DELETE` route, checks `audit.record` in handler source or private helper (handles the `_set_status` delegation pattern), or verifies route is in `_AUDIT_EXEMPT` with a documented reason. Exempt: `POST /auth/refresh` (token rotation, no config mutation) + `POST .../verify-draft` (key never stored, spec 54). Stale-entry check and exemption-reason check included. 6 new tests; 410 passing. | ~~verification~~ | §69 |

**Why RLS is worth the work even with the repository in place:** the repository
is the first layer and covers every path written through it. RLS covers the
paths that are not — an ad-hoc query, a future report, a migration script. The
two together mean a mistake has to be made twice to become a leak.

**Exit criteria**

- With RLS enabled, a raw `SELECT * FROM agents` under a tenant role returns
  only that tenant's rows.
- A test asserts every mutating endpoint writes an audit row.
- A tenant administrator can create and disable a user through the API, and a
  disabled user's existing token stops working immediately.

### Phase 1b — Call Policy Enforcement Debt

One item, recorded separately because it belongs to Phase 1's own goal rather
than to realtime quality:

| # | Item | Kind | Spec |
|---|---|---|---|
| ~~1b.1~~ | **Tenant call limits enforced at call acceptance.** ~~`max_daily_calls` and `max_monthly_minutes` relied on the `usage` rollup, which nothing wrote.~~ **Done (2026-09-12):** All three limits now enforced in `_enforce_limits` in priority order: (1) `max_concurrent_calls` — COUNT of active calls; (2) `max_daily_calls` — COUNT of today's calls in the tenant's timezone from `calls` table; (3) `max_monthly_minutes` — SUM of `duration_seconds` + in-progress estimates from `calls` for the current month. `update_usage()` upserts one row per tenant-day into `usage` at call end so the rollup stays accurate. Tests: `tests/test_call_limits.py`. | ~~defect~~ | §47 |

**Exit criteria (met):** a tenant at its daily call limit has the next call rejected
before a LiveKit room is created, and the `usage` table is populated as calls
complete.

### Sequencing

**The ordering constraint was not met, and that is now the position to work
from rather than a plan to make.** 2b.1–2b.4 were meant to land before the
agent builder exposed those fields; the builder shipped first. The same had
happened again with routing: the console presented rules, schedules and a
fallback chain that nothing evaluated at call setup (4b.3). **4b.3 is now
closed (2026-09-12)** — `worker/routing.py` is the runtime half of the
routing UI.

So the remaining priority is the enforcement gap, not more screens:

1. ~~**4b.3** — routing evaluation.~~ **Done 2026-09-12.**
2. ~~**2b.1–2b.4** — call-policy enforcement.~~ **Done 2026-09-12.** `silence_timeout_seconds` → `user_away_timeout`, `interruption_enabled/min_words` → `allow_interruptions/min_interruption_words`, `max_call_duration_seconds` → `_max_duration_watchdog`, `recording_enabled` → LiveKit egress start/stop.
3. **2b.11, then 2b.7.** 2b.9 and 2b.10 are done, and together they changed
   the picture: the agent now holds a conversation over the browser path, and
   time to first audio is 5854 ms — the first complete measurement rather than
   an improvement on 4744 ms, which was taken from a turn that never replied.
   ~~**2b.8 now comes before 2b.7**~~ — **2b.8 done 2026-09-12**
   (`make measure-latency`). Transcription time across three identical calls
   was 2314, 1661 and 11832 ms, so a turn-taking window tuned against any one
   of them would be fitted to noise. ~~The harness is what makes the next 2b.7
   window defensible.~~ **2b.7 done 2026-09-12** — window is 2.0 / 6.0 s,
   fitted to hosted STT (1103 ms), not the 11832 ms local sample.
   ~~2b.11's per-call block is half fixed~~ — **2b.11 done 2026-09-12**: the
   leftover 537 ms was the local EOT model; turn detection is now VAD.
   A real PBX call is still needed to confirm the 2b.7 symptom is gone off
   the browser path.
3. ~~**2b.1–2b.4**~~ — **Done 2026-09-12.**
4. **4b.4** — provider credentials in the console, which is what §77 turns on.
5. ~~**1b.1**~~ — **Done 2026-09-12.** All three limits enforced; usage rollup written at call end.

3b.1 (Row Level Security) has no ordering constraint and can run in parallel.

**The lesson worth keeping:** a form is not a feature. Each of 2b.1–2b.3 was
a screen that writes a row nothing reads, built first because it was the
visible half. 4b.3 fell into the same pattern and has now been corrected. The test named in Phase 2b's
exit criteria — *no setting is loaded at runtime and then ignored* — should be
extended to cover configuration the console writes, not only fields the worker
loads.

**4b.7–4b.9 are the counter-example, and they cost more up front.** Making the
provider controls real meant a migration, a column split in the catalog, a
widened resolution query, per-tier credential decryption, and a change to how
the session is assembled — before any of it could be rendered. It also surfaced
two faults that had nothing to do with the feature: a dataclass that could not
be constructed, and a 500 on every draft save. Both would have been shipped by
a UI-first approach and found by whoever next placed a call.

## 6. Phase 3 — Multi-Tenancy [§76]

### 6.1 Phase 2 carry-over

Moved to [§5b Carry-Over Phases](#5b-carry-over-phases), where the outstanding
work is defined as Phase 2b with its own exit criteria.

## 7. Phase 4 — Configuration Platform [§76]

### Goal

A non-developer tenant administrator can configure the platform entirely through the UI. [§62]

### Scope

| # | Work item | Spec |
|---|---|---|
| 4.1 | PBX UI + API — Name, Type, Host, Port, Transport, Status; Create/Edit/Delete/Enable/Disable/Test Connection; supports Asterisk, FreePBX, FreeSWITCH, FusionPBX, Kamailio, 3CX, Cisco, Mitel, other SIP | §14 |
| 4.2 | SIP Trunk UI + API — full field set including auth, allowed IPs, codecs, DTMF, media encryption; Create/Edit/Delete/Enable/Disable/Test | §15 |
| 4.3 | SIP Configuration Wizard — the 10 steps | §16 |
| 4.4 | Phone Number / DID UI — Number, Tenant, PBX, SIP Trunk, Inbound Agent, Routing Rule, Business Hours, Fallback, Status | §17 |
| 4.5 | Agent Builder UI — every field in §18/§62; Save Draft, Test, Publish, Rollback | §18, §62 |
| 4.6 | Agent versioning — `Draft`/`Testing`/`Published`; publish must not disturb in-flight calls; new calls take the published version | §19, §45 |
| 4.7 | Routing UI + routing engine — conditions on tenant, PBX, SIP trunk, DID, caller number, destination number, business hours, campaign, priority | §20 | **Done** (UI was done; engine evaluation at call setup done 2026-09-12) |
| 4.8 | Business hours per tenant, usable by routing | §37 |
| 4.9 | Fallback routing chain: primary agent → secondary agent → PBX queue → voicemail, configurable | §38 |
| 4.10 | Provider UI — providers, models, credentials (encrypted); all providers in §25 registered | §25 |
| 4.11 | Voice Library UI — Provider, Voice ID, Voice Name, Language, Accent, Description, Status; Add/Edit/Delete/Enable/Disable/Test/Set Default | §27 |
| 4.12 | ElevenLabs TTS adapter with streaming | §26 |
| 4.13 | Configuration validation before publish — STT, LLM, TTS, voice, prompt, tools, KB, transfer destination, routing, PBX, SIP trunk, DID | §63 |
| 4.14 | Dependency validation tree, showing disabled providers/voices/tools clearly | §64 |
| 4.15 | Tenant call limits — max concurrent, max daily, max monthly minutes; enforced **before** call acceptance | §47 |
| 4.16 | Tenant console shell — all 17 sections from §61 | §61 |
| 4.17 | Platform console shell — all 11 sections from §60 | §60 |

### Deliverables

- Troubleshooting entries in §12.
- API contracts for every resource in §67 that this phase touches, with OpenAPI published.
- Both console UIs, responsive and desktop-oriented.
- Validation rule catalogue for §63.

### Exit criteria

- A tenant administrator creates PBX → SIP trunk → DID → agent → routing rule and receives a
  call, **using only the UI**. No SQL, no SSH, no config files, no LiveKit CLI. [§77]
- Publishing `v2` while a call is running on `v1`: the running call finishes on `v1`, the next
  call answers on `v2`, and the `calls` rows record the correct `agent_version_id` for each.
- An agent with a disabled TTS voice cannot be published, and the dependency tree names the
  exact problem.
- A tenant at its concurrent-call limit has the next call rejected before a LiveKit room is
  created.
- Every configuration change appears in the audit log.

### Status

Both consoles are built and every section in §60 and §61 has a working screen.
What remains is listed as Phase 4b in [§5b](#5b-carry-over-phases) rather than
left as an unmarked gap.

| # | Item | State |
|---|---|---|
| 4.1 | PBX UI + API | **done** |
| 4.2 | SIP Trunk UI + API | **done** |
| 4.3 | SIP Configuration Wizard (10 steps) | **done** (4b.1, 2026-09-12) — `/sip-wizard` |
| 4.4 | Phone Number / DID UI | **done** — routing rule and business hours are on the form; fallback is shown from the pinned rule (4b.2, 2026-09-12) |
| 4.5 | Agent Builder UI | **done** |
| 4.6 | Agent versioning | **done** |
| 4.7 | Routing UI + engine | **done** — UI + runtime engine (`worker/routing.py`) both complete as of 2026-09-12 |
| 4.8 | Business hours | **done**, including dated exceptions, holiday overrides, and `open_now`; evaluated at call setup |
| 4.9 | Fallback routing chain | **done** — primary → secondary-agent fallback evaluated at call setup; PBX_QUEUE/VOICEMAIL deferred to Phase 6 |
| 4.10 | Provider UI — providers, models, credentials | **providers and models done**; tenant credential entry is CLI-only (**4b.4**) |
| 4.11 | Voice Library UI | **done** — Test/preview complete (4b.5, 2026-09-12) |
| 4.12 | ElevenLabs TTS adapter | **Done (2026-09-12) — 4b.6.** `worker/providers/tts/elevenlabs.py` |
| 4.13 | Validation before publish | **done** |
| 4.14 | Dependency validation tree | **done** — `GET /agents/{id}/versions/{n}/validate` returns issues and dependencies separately |
| 4.15 | Tenant call limits | **done** — all three enforced (concurrent, daily, monthly); usage rollup written at call end (Plan 1b.1 complete 2026-09-12) |
| 4.16 | Tenant console — all §61 sections | **done** |
| 4.17 | Platform console — all §60 sections | **done** |

The §77 exit criterion — configure everything **using only the UI** — is not
yet met, and 4b.4 is the reason. A tenant cannot enter its own provider API key
through the console, so a new tenant still needs `python -m app.cli
set-credential` before its agent can reach a cloud provider.

### Out of scope

LiveKit resource synchronization and drift detection (Phase 5); tools, RAG, transfer (Phase 6).

### Risks

| Risk | Mitigation |
|---|---|
| Largest phase; scope creep in UI polish | Field-complete before pixel-perfect; §18/§62 field lists are the definition of done |
| Version-pinning bug lets a publish disrupt live calls | Explicit test in exit criteria; the worker caches the version at call start (Phase 1, item 1.8) |
| Routing precedence ambiguity with overlapping rules | Define and document deterministic precedence (priority, then specificity) before building the engine |

---

## 8. Phase 5 — LiveKit Control Plane [§76]

### Goal

LiveKit is managed **through the platform**, with resource IDs mirrored in PostgreSQL and drift
detected automatically.

### Scope

| # | Work item | Spec |
|---|---|---|
| ~~5.1~~ | ~~LiveKit administration section — Clusters, SIP Configuration, SIP Trunks, Dispatch Rules, Agent Dispatch, Rooms, Participants, Media, Codecs, Recording/Egress, TURN/ICE, Health, Metrics~~ **Done (2026-09-12):** `/platform/livekit` lists all 13 spec-11 topics. Tenant items link to `/sip-trunks`, `/dispatch-rules`, `/sip-wizard`. Cluster / media / TURN are `scope=infra` and link to Infrastructure — not editable. Rooms and participant counts are a live `list_rooms` read (never written from the UI). Tests: `tests/test_livekit_admin.py`. | §11 |
| ~~5.2~~ | ~~LiveKit SIP trunk management fully API-driven from the UI~~ **Done (2026-09-12):** The standalone SIP Trunks dialog now offers direction, codecs, DTMF and SRTP — the same spec-15 fields the wizard already wrote. API accepted them; the form did not. Tests: `tests/test_sip_trunk_form.py`. | §11, §15 |
| ~~5.3~~ | ~~Dispatch rule management as long-lived objects; per-call creation explicitly prevented~~ **Done (2026-09-12):** Tenant `/dispatch-rules` CRUD. SHARED rooms are refused (spec 22). Worker tree is asserted to contain no `create_sip_dispatch_rule`. Tests: `tests/test_dispatch_rules.py`. | §21 |
| ~~5.4~~ | ~~Agent dispatch configuration per agent/DID~~ **Done (2026-09-12):** Dispatch-rule form sets `agent_dispatch_name` (must match `WORKER_AGENT_NAME`) and shows the trunk's DIDs. Phone Numbers form shows which worker LiveKit will invite for the selected trunk, and that the inbound agent is who speaks. Caller allow-list is labelled as the *caller*, not the DID (486 flood). | §21 |
| ~~5.5~~ | ~~Resource synchronization flow: `UI → API → validate → PostgreSQL → LiveKit API → store LiveKit resource ID`~~ **Done (2026-09-12):** Dispatch-rule create/update/sync writes the row first (`PENDING`), then create-or-recreate in LiveKit, then stores `livekit_resource_id` (`SYNCED` / `FAILED`). Same order as trunks. Delete removes LiveKit first so an orphan cannot keep matching calls. | §12 |
| ~~5.6~~ | ~~Sync status `SYNCED`/`PENDING`/`FAILED`/`DRIFTED` with Synchronize, Retry, Repair actions~~ **Done (2026-09-12):** Tenant `/sip-trunks` and `/dispatch-rules` plus platform `/platform/livekit` offer **Synchronize** (update in place), **Retry** (same after FAILED/PENDING), and **Repair** (delete leftover LiveKit resource, clear ID, recreate from PostgreSQL). `POST .../sync`, `/retry`, `/repair` and `POST /platform/livekit/{kind}/{id}/{action}`. Tests: `tests/test_livekit_jobs.py`, `tests/test_sync_actions.py`. | §12 |
| ~~5.7~~ | ~~Drift detection job comparing PostgreSQL against LiveKit; surfaces "Configuration Drift Detected"~~ **Done (2026-09-12):** `app/livekit/drift.py` list-and-compares inbound trunks and dispatch rules in both directions. A LiveKit ID that is gone or differs is marked `DRIFTED`; a LiveKit object no row names is an **orphan** on the last report (no auto-delete). `app/livekit/scheduler.py` runs on the API lifespan at 300 s + jitter (`LIVEKIT_DRIFT_CHECK_*`). A LiveKit outage is not drift — statuses stay put. On-demand: `POST /platform/livekit/drift-check`, `python -m app.cli detect-drift`, `make detect-drift`. Does not Repair — tenant Synchronize or `sync-livekit` recreates from PostgreSQL. Console: **"Configuration Drift Detected"** on `/platform` and `/platform/livekit`. Tests: `tests/test_drift.py`. | §46 |
| ~~5.8~~ | ~~Infrastructure/tenant configuration split — Redis, ports, RTP ranges, external IP, TLS, LBs, Kubernetes, networking, firewall, topology hidden from tenant admins~~ **Done (2026-09-12):** `TenantSettingsResponse` / `TenantSettingsUpdate` stay identity-only. Tenant Settings says cluster infrastructure is platform-controlled. Isolation tests: `tests/test_tenant_infra_isolation.py`. | §13 |
| ~~5.9~~ | ~~Asynchronous processing for all LiveKit operations~~ **Done (2026-09-12):** Create/update/sync/retry/repair write PostgreSQL, commit `PENDING`, return, then `app/livekit/jobs.py` runs LiveKit in a new session. Delete stays synchronous so a 204 cannot leave LiveKit still accepting calls. Tests: `tests/test_livekit_jobs.py`. | §80 |
| ~~5.10~~ | ~~Platform capacity dashboard — Total/Available Capacity, LiveKit Nodes, SIP Nodes, AI Workers, Worker Utilization, CPU, Memory, Network, Provider Health~~ **Done (2026-09-12):** `GET /platform/capacity` probes worker `/ready` + Prometheus metrics (`WORKER_HEALTH_URLS`, `LIVEKIT_METRICS_URL`). Null when a probe does not answer. Provider Health overlays catalog credentials and `voice_provider_circuit_state`. Page: `/platform/capacity`. Tests: `tests/test_capacity_fleet.py`. | §48 |

**Phase 5 is complete (2026-09-12)** — administration, SIP/dispatch CRUD, PostgreSQL-first
sync, drift detection, Synchronize/Retry/Repair, async LiveKit jobs, tenant/infra split,
and the spec-48 capacity dashboard.

### Deliverables

- Troubleshooting entries in §12.
- LiveKit integration design doc: resource lifecycle, ID mapping, reconciliation algorithm.
- Drift-detection runbook (README §16).
- Capacity dashboard.

### Exit criteria

- Every LiveKit resource the platform creates has its ID stored in PostgreSQL with a sync status.
- Deleting a SIP trunk directly in LiveKit is detected as `DRIFTED` within the detection
  interval, and **Repair** restores it from PostgreSQL.
- A `TENANT_ADMIN` cannot see or edit any infrastructure-level LiveKit setting.
- No normal tenant-configuration path requires a LiveKit CLI command.
- Load-bearing LiveKit calls run asynchronously; the UI never blocks on them.

### Risks

| Risk | Mitigation |
|---|---|
| Partial failure mid-sync leaves PostgreSQL and LiveKit inconsistent | `PENDING`/`FAILED` states plus idempotent Retry; PostgreSQL is authoritative and Repair reconciles toward it |
| Drift job storms LiveKit at scale | Interval + jitter, batched reads, and backoff on the admin API |

---

## 9. Phase 6 — Advanced AI [§76]

### Goal

Agents that can *do* things: call APIs, consult tenant knowledge, remember, escalate to humans,
and survive provider failure.

### Scope

| # | Work item | Spec |
|---|---|---|
| ~~6.0~~ | ~~Simple ticketing — tenant `tickets` table, CRUD API, `/tickets` UI; seed one ticket and a **Service Agent** whose job is to file tickets~~ **Done (2026-09-12):** `tickets` is tenant-owned with RLS. `GET/POST /tickets`, `PUT/DELETE /tickets/{id}`. Console **Tickets**. `seed-dev-tenant` creates **Service Agent** (published) and `TCK-0001` ("Lobby access card reader offline"). Agent-filed tickets are Plan 6.1. Tests: `tests/test_tickets.py`. | new |
| ~~6.1~~ | ~~Tools / function calling in the pipeline — `create_ticket()` (Service Agent only) plus `get_customer()`, `check_order()`, `create_order()`, `cancel_order()`, `check_inventory()`, `send_sms()`, `send_email()`, `transfer_call()`; assignable per agent~~ **Done (2026-09-12):** Worker loads granted tools onto `CallContext`, registers them with LiveKit `function_tool(raw_schema=…)`, and executes builtins (`builtin://name`) or HTTP. `create_ticket()` inserts a tenant ticket with `source=AGENT`. Seed creates the PRD example tools plus `refund_order`; Service Agent is granted `create_ticket` only. Tests: `tests/test_tools.py` (worker), `tests/test_phase6_tools.py`. | §30 |
| ~~6.2~~ | ~~API Tool Builder UI — name, description, method, URL, auth, headers, request/response schema, timeout, retry policy~~ **Done (2026-09-12):** `/tools` form now includes response schema, timeout (≤30s), and retry count. Builtin tools lock name and URL. | §31 |
| ~~6.3~~ | ~~Variable substitution — `{{customer_id}}`, `{{order_id}}`, `{{caller_number}}`~~ **Done (2026-09-12):** `shared/tools.py` substitutes `{{name}}`. Call-context values (`caller_number`, `call_id`, `did`, `tenant_id`, `agent_name`) do not need to be in the request schema. Model-supplied names still must be declared. | §31 |
| ~~6.4~~ | ~~Tool schema validation before agent publish~~ **Done (2026-09-12):** `_validate_version` blocks publish when a granted tool is inactive or `schema_valid` is false. | §31 |
| ~~6.5~~ | ~~Tool permissions — explicit per-agent allow-list; everything else denied~~ **Done (2026-09-12):** Agent builder checkboxes write `tool_ids` onto the draft (`agent_tools`). Drafts copy grants. The worker registers only granted tools and `ToolRuntime.invoke` still denies any other name. Seeded `refund_order` is in the library and granted to no agent. **Follow-up (2026-09-12):** Saving a live agent that already had tools 500'd (`uq_agent_tools_version_tool`) because `_replace_tool_grants` deleted then re-inserted the same pairs. It now syncs. Changing any field dirties the builder and enables **Publish**, which persists first (creates a draft when the version is live) then publishes. Files: `app/api/v1/agents.py`, `app/agents/page.tsx`. Tests: `test_phase6_tools.py`. | §32 |
| ~~6.6~~ | ~~Knowledge bases from PDF, DOCX, TXT, CSV, web content; ingestion pipeline~~ **Done (2026-09-13):** `POST /knowledge-bases/{id}/documents` (file) and `/documents/web` (URL). Extract → chunk (800/120) → embed via OpenAI-compatible `/embeddings`. Originals in MinIO (`knowledge/…`). Failed ingest keeps a FAILED row. Seed: **Hotel policies** / House rules (`harbour-1842`). Console Documents dialog uploads and reindexes. Files: `shared/knowledge.py`, `app/services/knowledge_ingest.py`, `app/api/v1/tools.py`, `app/knowledge-bases/page.tsx`. Tests: `test_knowledge.py`, `test_phase6_knowledge.py`. | §33 |
| ~~6.7~~ | ~~RAG retrieval on PostgreSQL + pgvector; knowledge bases assignable to agents~~ **Done (2026-09-13):** Worker `search_chunks` does cosine scan filtered by `tenant_id` + `knowledge_base_id`, keyword ILIKE if there is no key or no vectors. `ConfigurableAgent.on_user_turn_completed` injects the passages into instructions. Agent builder **Assigned base** writes `knowledge_base_id`. CallContext carries `KnowledgeRetrieval`. Tests: `tests/test_rag.py`. | §33 |
| ~~6.8~~ | ~~Conversation summarization for long conversations~~ **Done (2026-09-13):** Mid-call `RollingMemory` compresses older turns every 8 user turns via the call LLM and prepends “Earlier in this call” to instructions. Post-call 2b.5 summary is unchanged. Files: `worker/memory.py`, `worker/entrypoint.py`. Tests: `tests/test_memory.py`. | §34 |
| ~~6.9~~ | ~~Warm transfer back to the PBX — two simultaneous audio paths: caller hears the configurable announcement plus hold media, while the human agent leg is dialled through the PBX~~ **Done (2026-09-13):** `transfer_call` starts `WarmTransfer`. Caller announcement/hold on `caller-hold`; `CreateSIPParticipant` dials the destination (SIP/PBX extension, queue, external number, SIP URI). Seed: **Reception** (`1000`) and Development Agent is granted `transfer_call`. Files: `worker/transfer/`. Tests: `tests/test_transfer.py`. | §35, CR-1 |
| ~~6.10~~ | ~~Summary whisper to the human agent — TTS on the agent leg only, DTMF skip, duration cap, bridge only after accept~~ **Done (2026-09-13):** Whisper publishes `agent-whisper` with human-only subscription permissions; caller and human are unsubscribed from each other until bridge. DTMF `transfer_skip_dtmf` (default `1`) skips. Cap: `transfer_summary_max_seconds`. | §36, CR-1 |
| ~~6.10a~~ | ~~Transfer summary content — seven fields, spoken template, optional structured PBX delivery~~ **Done (2026-09-13):** JSON briefing (`customer`, `reason`, `summary`, `actions_taken`, `order_information`, `sentiment`, `required_next_action`) rendered with `{{field}}` template. SIP `X-Transfer-*` headers + participant metadata are best-effort (TS-5). Generation is a background task so the announcement is not delayed (TS-3); failure uses the fallback whisper (TS-4). Builder fields on `/agents`. | §36, CR-1 |
| ~~6.10b~~ | ~~`transfer_status` lifecycle + fallbacks + timings~~ **Done (2026-09-13):** `TRANSFER_STATUS_TRANSITIONS` enforced on write. Timings and `transfer_summary` JSON persist on `calls`. No-answer / busy / reject / fail take the Phase 4 chain (`transfer_fallback_taken`); a unused queue/voicemail destination is retried while still `DIALING_AGENT`. Caller always hears a spoken line — never silence. | §38, §41, CR-1 |
| ~~6.10c~~ | ~~AI leaves on bridge; human-to-human continues with recording and `HUMAN_AGENT` speech~~ **Done (2026-09-13):** On bridge the session is closed, state becomes `HUMAN_AGENT`, room-composite egress stays up, and later transcription from `human-agent-*` is stored as `SpeakerType.HUMAN_AGENT`. Whisper segments are `is_private_to_agent`. `_wait_for_disconnect` ignores the human leaving before bridge so fallback can run. | §39, §40, CR-1 |
| ~~6.11~~ | ~~Remaining provider adapters — all STT, LLM, TTS providers in §25~~ **Done (2026-09-13):** Registry now drives Deepgram / ElevenLabs / Google / Azure STT, Anthropic LLM, and Cartesia / Deepgram / Google / Azure TTS. OpenAI Whisper, Gemini, and local/self-hosted stay on `openai_compatible`. Seed added `elevenlabs_stt`, `azure_stt`, `google_tts`, `azure_tts` (catalog 33). Plugins are lazy-imported. Files: `worker/providers/stt/{deepgram,elevenlabs,google,azure}.py`, `worker/providers/llm/anthropic.py`, `worker/providers/tts/{cartesia,deepgram,google,azure}.py`, `worker/providers/_livekit.py`, `worker/providers/registry.py`, `app/services/seed.py`. Tests: `tests/test_phase6_adapters.py`. | §25 |
| ~~6.12~~ | ~~Provider resilience — timeout, retry, exponential backoff, circuit breaker, fallback provider~~ **Done (2026-09-12 as Plan 4b.10; documented 2026-09-13):** `worker/resilience/` already has per-stage timeouts, circuit breaker, `async_retry()`, and LiveKit `FallbackAdapter`. Phase 6 deliverable is the failover matrix in README §9c.8. Tests: `tests/test_resilience.py`, `tests/test_provider_chain.py`. | §55 |
| ~~6.13~~ | ~~Tenant configuration import/export, with secrets never exported in plaintext~~ **Done (2026-09-13):** `GET /api/v1/settings/export` (`agents.read`) and `POST /api/v1/settings/import` (`agents.write`). Bundle format `livekit-voice-agent.tenant.v1` covers agents, versions (imported as drafts), tools, routing, business hours, knowledge-base config, transfer destinations. Secrets are stripped on the way out and refused on the way in. Credentials export as metadata only. Settings UI download + file picker. Files: `app/services/tenant_impex.py`, `app/api/v1/admin.py`, `app/settings/page.tsx`. Tests: `tests/test_phase6_impex.py`. | §65 |

### Deliverables

- Troubleshooting entries in §12.
- Tool execution design doc: sandboxing, timeouts, retry semantics, variable resolution.
- RAG design doc: chunking, embedding model, retrieval strategy, per-tenant partitioning.
- ~~Provider failover matrix — which provider falls back to which, per kind.~~ **Done (2026-09-13):** README §9c.8. Tiers are per agent version (primary → fallback → local), not a hardcoded vendor chain.

### Exit criteria

- A tenant administrator can list, create, and close tickets in `/tickets`. Seed data includes one ticket and a **Service Agent**.
- On a call to Service Agent, the model files a ticket via `create_ticket()` and the row appears in `/tickets` with `source=AGENT`.
- An agent calls a tenant-configured HTTP tool mid-call and uses the result in its reply.
- An agent denied the Refund API cannot invoke it, even when the LLM tries. [§32]
- A tenant knowledge base answers a question that is not in the system prompt, retrieved from
  that tenant's documents only.
- A warm transfer is verified **by listening to both legs**: the caller hears the announcement
  and never hears the summary; the human agent hears the summary before the bridge; the legs
  connect only afterwards.
- A transfer whose spoken summary contains all seven §36 fields is delivered to a PBX queue,
  with the structured rendering attached where the PBX supports it.
- A human agent who does not answer, is busy, or rejects the call sends the caller down the
  configured fallback chain — never to silence or a dropped call.
- Summary generation failing or timing out still produces a transfer, using the minimal
  fallback whisper.
- Killing the primary TTS provider mid-call fails over to the fallback; the worker pool stays up
  and other calls are unaffected. [§55]
- Exported tenant configuration contains no plaintext secret.

### Risks

| Risk | Mitigation |
|---|---|
| A slow tenant API stalls the voice pipeline | Hard per-tool timeouts (§31) with a spoken holding response; tools run off the audio path |
| RAG retrieval adds unacceptable latency | Measure retrieval inside the §56 latency budget; cap top-k and pre-warm embeddings |
| Cross-tenant leakage through the vector store | Partition by `tenant_id` at the query level and test it in the Phase 3 isolation suite |
| LLM invents tool arguments | Schema validation on every tool call; reject and re-prompt rather than forwarding bad calls |
| Summary whisper leaks to the caller — the one failure mode that is unacceptable in front of a customer | Isolation is structural: the whisper is published only into the agent leg's audio path, never the caller's. Tested by recording both legs separately and asserting the caller leg is free of it |
| Caller abandons during a long summary whisper | Bound the whisper duration (TR-12), start the caller announcement before summary generation finishes (TS-3), and offer DTMF skip to the agent (TR-9) |
| PBX cannot signal agent answer/reject distinctly | Establish which SIP responses the target PBXs actually emit during Phase 4 trunk testing, before the transfer state machine depends on them |

---

## 10. Phase 7 — Production Infrastructure [§76]

### Goal

The architecture that Phase 8 will measure. Kubernetes, HA, autoscaling, observability, backup,
DR.

### Scope

| # | Work item | Spec |
|---|---|---|
| 7.1 | Helm charts with independent scaling for Frontend, API, LiveKit, LiveKit SIP, AI Workers | §5, §4 |
| 7.2 | High availability — multiple LiveKit nodes, SIP nodes, AI workers, API replicas; PostgreSQL HA; Redis HA; redundant load balancers | §52 |
| 7.3 | HPA (or equivalent) driven by active AI jobs, worker utilization, CPU, memory; calls treated as jobs/sessions | §50 |
| 7.4 | Graceful shutdown on `SIGTERM` — stop accepting, finish existing, disconnect cleanly, exit; grace period exceeds max call duration | §51 |
| 7.5 | Full Prometheus metric coverage: infrastructure, LiveKit, AI, business | §57 |
| 7.6 | Grafana dashboard suite | §57 |
| 7.7 | Alerting rules — capacity, provider health, drift, failure rate, latency regression | §57 |
| 7.8 | Log aggregation (optional Loki) and tracing (optional Tempo / OpenTelemetry) | §3 |
| 7.9 | Secret management via Kubernetes Secrets or Vault / AWS Secrets Manager / Google Secret Manager / Azure Key Vault | §54 |
| 7.10 | Backup — PostgreSQL, configuration, recording metadata, object storage; Redis classified disposable vs persistent | §66 |
| 7.11 | Documented and rehearsed disaster recovery procedures | §66 |
| 7.12 | Failure testing — AI worker, LiveKit node, SIP node, API restart, Redis, database, STT, LLM timeout, TTS, packet loss, high CPU, worker exhaustion; **document recovery behavior** | §73 |
| 7.13 | Documentation set: development, deployment, configuration, operations | §80 |

### Deliverables

- Troubleshooting entries in §12.
- Production Helm charts and `values.production.yaml`.
- Alert catalogue with thresholds and owners.
- DR runbook with a rehearsal record (not just a document).
- Failure-testing report — one section per failure mode in §73, stating observed recovery.

### Exit criteria

- A rolling deployment during active calls terminates **zero** calls.
- Killing a LiveKit node, a SIP node, and an AI worker each degrade gracefully; new calls keep
  connecting.
- HPA scales workers up under synthetic load and back down after, without dropping calls.
- A PostgreSQL restore from backup is demonstrated into a clean environment.
- No production secret exists in source, Git, images, logs, frontend, plaintext config, or
  prompts — verified by scan **and** review. [§54]
- Every failure mode in §73 has a documented, observed recovery behavior.

### Risks

| Risk | Mitigation |
|---|---|
| Long-lived calls fight Kubernetes' short-lived-pod assumptions | Treat calls as sessions (7.3); set grace periods above max call duration; drain rather than kill |
| SIP/RTP through cloud load balancers behaves unlike local Docker | Validate the media path on real infrastructure early in the phase, not at the end |
| HA for PostgreSQL/Redis deferred as "later" | Both are explicit exit criteria; a single-instance database is not a production architecture |

---

## 11. Phase 8 — Capacity Testing [§76]

### Goal

Prove the 1,000-concurrent-call target with measurements, or state honestly what the platform
actually supports. **No fake scalability.** [§75]

### Scope

| # | Work item | Spec |
|---|---|---|
| 8.1 | Load-test harness driving real SIP calls | §71 |
| 8.2 | Progressive runs at 10, 25, 50, 100, 250, 500, 750, 1000 concurrent calls | §71, §76 |
| 8.3 | Measure at each step: CPU, RAM, network, SIP capacity, RTP performance, packet loss, jitter, STT latency, LLM latency, TTS latency, first-audio latency, call failure rate, worker utilization, LiveKit utilization | §71 |
| 8.4 | Sustained runs at high concurrency for 15, 30, and 60 minutes | §72 |
| 8.5 | Tests exercise the production architecture and **real** AI providers, including SIP, LiveKit, workers, STT, LLM, TTS, recording, tools, and RAG where production-enabled | §75 |
| 8.6 | Establish the true safe calls-per-worker figure | §49, §74 |
| 8.7 | Capacity report derived from benchmarks, with production headroom | §74 |
| 8.8 | Remediation cycles for whatever the tests expose | §71–§75 |
| 8.9 | Final acceptance test — the complete §77 UI-only journey | §77 |

### Deliverables

- Troubleshooting entries in §12.
- **Capacity report**: measured calls-per-worker, required worker count with headroom,
  per-component saturation points, and the observed limits of the current architecture.
- Per-step measurement dataset for all metrics in §71.
- Sustained-load results at 15/30/60 minutes.
- Final acceptance test record.

### Exit criteria

- The progression through 1,000 concurrent calls is executed and documented — including any
  step where it failed and what was changed.
- At target concurrency, a **60-minute** sustained run holds without degradation. Short bursts
  do not count. [§72]
- Worker count is derived from measured calls-per-worker, not assumed. [§74]
- No capacity claim in any document or UI exceeds what the tests demonstrated. [§75]
- The §77 acceptance journey completes through the UI alone.

### Risks

| Risk | Mitigation |
|---|---|
| AI provider rate limits cap the test before the platform does | Negotiate quota early; record provider limits as a first-class capacity constraint, not a test artifact |
| Provider cost of a 60-minute 1,000-call run | Budget explicitly at phase start; use the progression to extrapolate cost before the largest run |
| Load generator becomes the bottleneck | Distribute generators; validate the harness's own ceiling before trusting results |
| Pressure to claim 1,000 calls on extrapolation | §75 is an acceptance criterion — the claim requires the measurement |

---

## 12. Environment Notes and Troubleshooting

Everything below was hit while building this platform, not anticipated. Each
entry names the symptom first, because that is what a future engineer will
have, and the symptom is often nothing like the cause.

Entries marked **environment** are properties of a particular host or
dependency version. Entries marked **defect** were bugs in this codebase, kept
here because the symptom will recur if the fix is ever reverted.

### 12.0 Phase 0 — Foundations

#### Host port collisions

**Symptom:** `Bind for 0.0.0.0:5432 failed: port is already allocated`,
partway through `compose up`, leaving the stack half-started.
**Cause (environment):** 5432, 6379 and 8081 were already taken by other
stacks on the same machine.
**Fix:** the project owns a dedicated host-port block (see the README's
endpoint table), and `make up` runs `scripts/preflight.py` first, which derives
every published port from the compose file and reports what holds it. Run
`make preflight` before blaming the application.

> On any new machine, run `make preflight` **before** the first `compose up`.
> It is the difference between a one-line message and a half-started stack.

#### Dependency conflict on prometheus-client

**Symptom:** `ResolutionImpossible` during the worker image build.
**Cause (environment):** `livekit-agents` requires `prometheus-client>=0.22`;
the pin was 0.21.1.
**Fix:** all three components pin the same version. When bumping
`livekit-agents`, re-check this: the shared package, the API and the worker
must agree or the image build fails.

#### LiveKit deprecation warning at startup

**Symptom:** `prometheus_port is deprecated, please switch prometheus.port`.
**Fix:** nested form in `deploy/livekit/livekit.yaml`:

```yaml
prometheus:
  port: 6789
```

#### livekit-sip ignores the config path

**Symptom:** `open /sip/config.yaml: no such file or directory`, looping.
**Cause (environment):** the image reads `/sip/config.yaml`. It does not take
a path from the environment, despite `SIP_CONFIG_FILE` existing.
**Fix:** the configuration is now passed inline via `SIP_CONFIG_BODY` in the
compose file, which also allows environment substitution — a mounted YAML file
cannot interpolate `${SIP_NAT_IP}`.

#### Settings fail to parse a list from the environment

**Symptom:** `SettingsError: error parsing value for field "cors_allow_origins"`.
**Cause (defect):** pydantic-settings JSON-decodes complex fields *before*
field validators run, so `http://a,http://b` is rejected as invalid JSON.
**Fix:** annotate the field `Annotated[list[str], NoDecode]`, which defers to
the validator.

#### Tooling not on PATH inside containers

**Symptom:** `sh: 1: ruff: not found` after a successful `pip install`.
**Cause (environment):** pip installs console scripts to `~/.local/bin` for the
image's non-root user, which is not on PATH.
**Fix:** invoke through `python -m ruff` / `python -m mypy`.

#### Lint passes locally but the config is ignored

**Symptom:** `ruff check` reports "All checks passed" while clearly violating
the configured ruleset.
**Cause (defect):** ruff discovers `pyproject.toml` by walking up from the
target files. The service images do not contain the repository root, so ruff
silently fell back to its defaults.
**Fix:** `make lint` and `make fmt` run in a throwaway container with the
repository root mounted, exactly as CI invokes them. If a lint run looks
suspiciously clean, check which config was discovered.

#### Ruff selector prefix surprise

**Symptom:** 21 `ANN001` findings from a ruleset that never selected `ANN`.
**Cause (environment):** ruff selectors are prefix-matched, so `"A"`
(flake8-builtins) also selects `ANN` (flake8-annotations).
**Fix:** `"ANN"` is in the ignore list, with the reason recorded there.

#### Alembic revision generation fails on a post-write hook

**Symptom:** `FAILED: Could not find entrypoint console_scripts.ruff`.
**Cause:** the hook ran inside the runtime image, which has no linter.
**Fix:** the hook is removed. Shipping a linter in a runtime image to format
generated migrations is the wrong trade; `make fmt` covers them.

#### Container-internal port collision

**Symptom:** worker exits immediately with
`[Errno 98] error while attempting to bind on address ('0.0.0.0', 8081)`.
**Cause (environment):** the LiveKit Agents runtime binds 8081 for its own HTTP
server — the same port the worker's health listener used.
**Fix:** the health listener moved to 8090. This is not a host-port conflict;
`make preflight` cannot see it, because both listeners are inside one
container.

#### Every log line emitted twice

**Symptom:** each event appears once in this platform's JSON format and once in
LiveKit's, doubling log volume.
**Cause (environment):** the agents runtime installs its own root handler.
**Fix:** wrap `setup_logging`, then reassert our configuration. It has to be
patched on the module that **calls** it — `livekit.agents.cli.cli` — because
`cli.py` does `from .log import setup_logging` and holds a direct reference.
Patching `livekit.agents.cli.log.setup_logging` has no effect, which is a
convincing dead end to spend an hour in.

### 12.1 Phase 1 — Basic Call

#### `486 Busy` with `reason: flood` — the expensive one

**Symptom:** every inbound INVITE is rejected. FreeSWITCH reports
`NO_ANSWER`; livekit-sip logs `Rejecting inbound flood` at `inbound.go:890`
and `status: 486, reason: "flood"`.

**Cause (environment):** "flood" is how livekit-sip classifies a call that
**matches no dispatch rule**. It is not a rate limiter, and nothing about the
message says "dispatch". The rule existed and was attached to the right trunk,
but its `inbound_numbers` filter was set to the dialled number `1001` — and
`inbound_numbers` on a *dispatch rule* matches the **caller's** number, not the
number that was called. It therefore matched nothing.

**Fix:** leave `inbound_numbers` empty on the dispatch rule and let the trunk's
`numbers` field restrict which DIDs the trunk accepts. Set `inbound_numbers`
only to filter by *calling* party.

**Diagnosis that actually worked, in order:**

1. `reason: "flood"` at info level says nothing useful. Set the SIP log level
   to `debug` (`SIP_LOG_LEVEL=debug`) to get the `caller` field, which names
   the source line and distinguishes this rejection from every other 486.
2. Check whether the trunk matched: the debug line carries `sipTrunk`. If a
   trunk ID is present, trunk matching is fine and the problem is downstream.
3. Confirm the request even arrives, before suspecting configuration. A
   throwaway UDP listener on a spare published port, plus a SIP `OPTIONS`
   probe, separates "not arriving" from "arriving and rejected".

Time was lost chasing rate limiting, IP allow-listing and authentication in
turn, because the word "flood" implies volume. It does not.

#### Docker Desktop rewrites inbound UDP source addresses

**Symptom:** livekit-sip logs `fromIP: 167.82.48.223` for a call from a PBX at
`192.168.0.113`. IP allow-listing never matches.
**Cause (environment):** Docker Desktop for macOS rewrites the source address
of inbound packets to a synthetic public address. Verified directly: a UDP
packet sent from the PBX to a throwaway listener arrived with the rewritten
source.

**Consequences, both real:**

- **IP allow-listing (spec 53) cannot function in this development setup.**
  Not a design flaw, an environment property. On a Linux host, or Kubernetes
  with `externalTrafficPolicy: Local`, the source address survives. It must be
  re-verified there rather than assumed to work because it was configured.
- livekit-sip flood-rejects sources outside its trusted networks, so
  `local_net` has to be widened in development.

**Fix in development:** SIP digest authentication instead of an IP allowlist —
which is better practice regardless, and what spec 15 asks for. The trunk
carries `auth_username` plus an encrypted password, and the calling side
authenticates.

#### No audio, or one-way audio, while signalling succeeds

**Symptom:** the call connects and then nobody hears anything.
**Cause (environment):** livekit-sip advertises its own address in SIP
`Contact` headers and in SDP. Inside Docker that is the bridge address
(`172.x.y.z`), which a PBX on another host cannot route to. Signalling still
succeeds, so this presents as silence rather than an error.
**Fix:** set `nat_1_to_1_ip` to an address the PBX can reach.
`./scripts/lan-ip.sh` prints it; `SIP_NAT_IP` in `.env` carries it. Do **not**
use `use_external_ip: true` on a LAN — STUN returns the internet-facing
address, which is wrong for a PBX on the same network.

#### `UpdateSIPInboundTrunk` is not implemented

**Symptom:** `no handler for path "/twirp/livekit.SIP/UpdateSIPInboundTrunk"
(code=bad_route)`.
**Cause (environment):** the SDK exposes the call; LiveKit server 1.8.4 does
not route it.
**Fix:** `bad_route` maps to `LiveKitUnsupportedError`, and synchronisation
falls back to delete-and-recreate. Two consequences that are easy to miss:

- LiveKit refuses a second trunk claiming a number an existing trunk already
  has (`Conflicting inbound SIP Trunks ... without AllowedNumbers set`), so the
  old trunk must be deleted **before** the new one is created.
- Recreating changes the trunk ID, so every dispatch rule referencing it must
  be rebuilt. A rule left pointing at a deleted trunk matches nothing and
  presents as — again — `486 flood`.

#### Extension calls reach the dialplan, bridge, and then never authenticate

**Symptom:** a dialplan entry matches and bridges correctly — the FreeSWITCH log
shows `Regex (PASS)` and `EXECUTE bridge(...)` — but no call ever reaches the
platform. livekit-sip logs `Created digest challenge`, then
`auth challenge timed out without authenticated retry`.

**Cause (defect, in the dialplan):** `sip_auth_username` and
`sip_auth_password` were set as dialplan `set` actions. Those apply to the
**A-leg**; they do not propagate to the outbound leg that `bridge` creates. The
calling side therefore never answers the `407`.

**Fix:** put them inside the dial string, where they belong to the new leg:

```
bridge({sip_auth_username=lkdev,sip_auth_password=...}sofia/external/sip:1801@HOST:5060)
```

This is why `originate` tests passed while the dialplan failed: the `originate`
command carried the credentials in its own `{...}` prefix, which *is* the
outbound leg. Two paths that look equivalent and are not.

#### Testing a dialplan from the CLI lands in the wrong context

**Symptom:** `originate user/3001@domain &transfer(1801 XML domain)` shows the
dialplan being evaluated as `parsing [default->...]`, and a custom entry in the
domain context is never reached.

**Cause (environment):** an originated leg *to* an extension is an outbound
call, so it takes the `default` context — not the extension's `user_context`.
Setting `context=` on the channel or passing a context to `transfer` does not
change it.

**Fix:** use a loopback channel, which does enter a chosen context:

```bash
fs_cli -x "originate {origination_caller_id_number=3001}loopback/1801/DOMAIN/XML &park"
```

The dial string is `loopback/<destination>/<context>/<dialplan>`. Note that
`-ERR MANDATORY_IE_MISSING` on the loopback A-leg is normal here and does not
mean the test failed — check the log for the dialplan match and the resulting
call record instead.

#### FusionPBX serves a cached dialplan, so SQL inserts appear to do nothing

**Symptom:** a dialplan row exists in `v_dialplans` and is correct, but
FreeSWITCH behaves as though it does not exist. `reloadxml` changes nothing.

**Cause (environment), and this entry had it half right until it cost another
evening:** there are *three* representations, not two.

| Where | What it is |
|---|---|
| `v_dialplan_details` | The rows Dialplan Manager edits. **Not what FreeSWITCH reads.** |
| `v_dialplans.dialplan_xml` | A pre-rendered XML blob. **This is the source of truth.** |
| `/var/cache/fusionpbx/dialplan.<domain>` | A file cache of the generated context |

Pressing **Save** in the GUI does two things: it re-renders the detail rows
into `dialplan_xml`, *and* it clears the cache. A direct write to the detail
rows does neither — so the edit is visible in the GUI, correct in the database,
and completely inert. Clearing the cache alone does not help either: the cache
regenerates from `dialplan_xml`, which still holds the old rendering.

**Fix, in order of preference:**

1. Open the entry in Dialplan Manager and press **Save** — this also validates
   that the inserted rows render correctly.
2. **Advanced → Cache → Flush Cache** in the GUI.
3. Update **both** `v_dialplan_details` *and* `v_dialplans.dialplan_xml`, then
   clear the cache. Editing only the detail rows changes nothing at all.

To clear the cache without root, use FusionPBX's own Lua API through
FreeSWITCH — it runs as the user that owns the directory, so it can delete what
a group member cannot:

```lua
-- /tmp/clear_dialplan_cache.lua, then: fs_cli -x "lua /tmp/clear_dialplan_cache.lua"
local cache = require "resources.functions.cache"
cache.del("dialplan:<domain>")
```

`fs_cli` reports `-ERR no reply` and deletes the file anyway; check the file
rather than the return value. The script must live somewhere the FreeSWITCH
user can read — `/tmp`, not a home directory.

Note that the cache **file** is group-writable by `www-data` while the
**directory** is not, so a member of that group can rewrite the file in place
but cannot delete it. **Do not truncate it to force a regenerate.** FusionPBX
serves an empty cache file as an empty dialplan: every number in the domain
stops resolving, including ordinary extensions, until the file is restored.
That was done here despite this paragraph already warning about it, and the
only reason it lasted two minutes was a backup taken first. Take the backup.

Also worth knowing: `xml_locate dialplan` returns "can't find anything" for
anything served by `mod_xml_curl`, because the XML is fetched per call rather
than held in FreeSWITCH's static registry. It is not a useful test here.

#### A dialplan inserted by SQL does not appear in Dialplan Manager

**Symptom:** the row exists in `v_dialplans`, is correct, and FreeSWITCH routes
calls through it — but it is absent from the FusionPBX Dialplan Manager list,
so it cannot be viewed or edited in the UI.

**Cause (environment, and a SQL three-valued-logic trap):** the list query
includes

```sql
and app_uuid <> 'c03b422e-13a8-bd1b-e42b-b6b9b4d27ce4'   -- inbound routes
```

`NULL <> 'anything'` evaluates to **NULL, not TRUE**, so any row with
`app_uuid IS NULL` is silently filtered out. Nothing warns you: the dialplan
works perfectly and is simply invisible.

**Fix:** set `app_uuid` to the dialplans app's own uuid, which is the first
uuid in `/var/www/fusionpbx/app/dialplans/app_config.php` and matches what
existing manually-created entries use. Also set `dialplan_destination` to
`'false'` to match the convention. Verify by running the app's own WHERE clause
against the database rather than trusting the UI:

```sql
SELECT count(*) FROM v_dialplans
WHERE (domain_uuid = '<domain uuid>' OR domain_uuid IS NULL)
  AND app_uuid <> 'c03b422e-13a8-bd1b-e42b-b6b9b4d27ce4'
  AND dialplan_context <> 'public'
  AND dialplan_name = '<your dialplan>';
```

The general lesson: when inserting into an application's schema directly, the
row has to satisfy that application's *queries*, not just its constraints. The
database will accept a row the UI can never show.

#### The agent transcribes the caller but does not reply

**Symptom:** a live call produces caller transcript segments — real speech,
correctly transcribed — and **no AI segments at all**. The caller hears
nothing and says "can you hear me?". One `turn_completed` event fires for
several caller utterances.

**Evidence from a real call** (`call_20260911T063710`):

```
caller: "Hello?"
caller: "..."
caller: "Can you hear me?"
  → 0 AI segments
  → 1 turn_completed: eou=2581ms transcription=1103ms llm_ttft=2243ms
                      time_to_first_audio=4744ms
```

Two warnings in the worker log name the cause:

```
transcript arrives after turn has been committed.
  consider raising `min_delay` in the endpointing
skipping user input, speech scheduling is paused
```

**Cause (configuration, not a defect):** the pipeline is too slow for its own
endpointing window. Transcription takes ~1.1s and end-of-utterance detection
~2.6s, so a transcript lands **after** the turn it belongs to has already been
committed, and the utterance is dropped rather than answered.

**Time to first audio of 4.7 seconds is not a conversation.** A caller
experiences that as a dead line, which is exactly what happened.

**What to change:**

1. **Move STT off the public internet.** `gpt-4o-mini-transcribe` over the
   network costs ~1.1s per turn before the model has even seen the words. The
   self-hosted endpoint already configured for TTS is the obvious candidate.
2. **Tune endpointing** — raise `min_delay` so a late transcript still joins
   its turn, and measure `min_endpointing_delay` / `max_endpointing_delay`
   against real speech rather than defaults.
3. **Only then judge the model.** Until a turn survives its own endpointing
   window, LLM latency is not the binding constraint.

**Why this was invisible until now:** without transcript persistence and
per-turn latency (Phase 2, both landed), a call like this looked identical to a
healthy one — it connected, ran the state machine, and completed with a normal
duration. This entry exists because the observability work is what made the
failure legible, which is the argument for doing 2b.7 and the latency tuning
before anything else in Phase 2b.

#### Confirmed on a real PBX call: the agent replies, and `tiny` is not good enough

A live call from the FusionPBX at 192.168.0.113 to DID 1801, parked so it stays
open, with FreeSWITCH's own `ivr-welcome_to_freeswitch.wav` broadcast into it
after the greeting finished:

```
AI     : Hello. You are through to the development voice agent…
CALLER : Welcome to Free Switch, The Future Up to Lathany.
AI     : Thanks! How can I assist you with Free Switch today?
```

**The 2b.7 symptom is gone on a real call, not only in the browser.** SIP,
media, STT, LLM and TTS all work end to end, and 131 RTP frames arrived while
132 went back.

**And `faster-whisper-tiny` on 8 kHz telephony audio is not good enough.** The
recording says *"Welcome to FreeSWITCH, the future of telephony"*. The
transcript says *"The Future Up to Lathany"* — half the sentence, from a clean
studio recording of clearly enunciated English. This is the accuracy question
that the browser path explicitly could not answer, because a browser sends
wideband audio and a phone does not. It is answered now, and the answer is no.

Latency on that turn:

| | |
|---|---|
| end-of-utterance | 1308 ms |
| transcription | 1296 ms |
| LLM first token | 2953 ms |
| TTS first audio | 986 ms |
| **time to first audio** | **5247 ms** |

Better than the browser figures and still too slow, with the LLM now the
largest single term rather than STT.

**Two things this changes:**

1. **2b.9 gets a concrete next step**: `faster-whisper-base` or `-small`, or
   hosted STT. `tiny` was chosen for speed on a CPU-only box, and the capacity
   ceiling measured earlier means a larger local model makes latency worse. The
   honest options are hosted STT or different hardware — the tradeoff is now
   measured on both axes instead of assumed on one.
2. **The test recipe in §9a.8 needs the parked form.** `originate … &playback`
   hangs up as soon as the file ends, which is 2.6 s — before the agent's
   greeting finishes. That produced a call with audio flowing both ways, a
   `COMPLETED` row, and **zero transcript segments**, which reads exactly like
   a broken pipeline. The working form parks the call, waits for the greeting,
   then uses `uuid_broadcast`.

#### The agent does hold a conversation — measured over the browser test path

The first complete turn this project has recorded, from `make test-room` plus a
browser participant against published v2 (self-hosted STT, hosted LLM,
self-hosted TTS):

```
AI     : Hello. You are through to the development voice agent. How can I help?
CALLER : Hello, I would like to book a table for two people tomorrow evening.
AI     : Sure! What time would you like to book the table for?
```

So the 2b.7 symptom — caller segments and no AI segments — is **gone on this
path**. Whether it is gone on a PBX call is a separate question and needs a
real call to answer; this audio is 24 kHz from Kokoro, not 8 kHz from a phone.

**The latency is worse than the failing call, and that is not a contradiction.**

| | Failing PBX call | Browser, warm |
|---|---|---|
| end-of-utterance | 2581 ms | 1682 ms |
| transcription | 1103 ms | 1661 ms |
| LLM first token | 2243 ms | 2272 ms |
| TTS first audio | not reached | 1899 ms |
| **time to first audio** | 4744 ms | **5854 ms** |

The 4744 ms figure was measured on a turn that never produced a reply, so it
was never a complete measurement to beat. 5854 ms is the first honest one, and
it is still far too slow to be a conversation.

**Two things were claimed here on the first pass and both were wrong.** They
are left described rather than deleted, because the way they were wrong is the
lesson.

*Claimed: nine `event loop blocked` warnings in one call, 114 ms to 794 ms.*
The count came from `grep -c` over a log tail that also covered worker startup,
where four inference processes initialise at once. Isolating a single call
between two timestamps gives **three** blocks, of which one is per-call.

*Claimed: every per-stage latency figure is inflated by it.* The per-call block
sits between `call_configuration_loaded` and the session starting — **before**
any turn exists. It delays the greeting the caller hears; it does not touch
`transcription_delay_ms` or `llm_first_token_ms` for a turn that happens
afterwards.

Both errors came from reading an aggregate where a timeline was needed. The
count and the conclusion were available from the same logs, correctly, in about
the same effort.

**What the per-call block actually was, measured:**

| | Block between config load and session start |
|---|---|
| before | 1020 ms |
| after loading VAD in `prewarm_fnc` | **628 ms** |

Silero is an ONNX model, loading it is synchronous, and it was being loaded
inside the entrypoint on every call. `prewarm_fnc` loads it once per process;
the model is stateless and identical for every tenant, so sharing it changes no
behaviour.

The remaining 628 ms is inside LiveKit's own `AgentSession.start`. The obvious
suspect was the three provider constructors building HTTP clients and SSL
contexts, and that was measured and ruled out: 34 ms cold for STT, 3-4 ms for
LLM and TTS, 3 ms each warm. Finding the rest needs a profiler on the hot path,
not another guess.

#### Self-hosted Whisper on this host cannot support a conversation

Measured against `hos_speaches` (`ghcr.io/speaches-ai/speaches:latest-cpu`),
`faster-whisper-tiny`, one fixed 3.6 s utterance:

| | p50 | p95 | max | realtime× |
|---|---|---|---|---|
| sequential | 773 ms | 2655 ms | 3024 ms | 4.6 |
| 2 concurrent | 3964 ms | 4311 ms | 4465 ms | **0.9** |
| 4 concurrent | 7571 ms | 9141 ms | 9286 ms | **0.5** |

*realtime×* is the audio's own duration over the transcription time. Below 1
the server transcribes slower than a caller can speak, so a conversation falls
progressively further behind.

Three conclusions, in order of how much they matter:

1. **It saturates at roughly 1.7 transcriptions per second and stays there.**
   Wall time is linear in request count across all three runs, and the
   container draws 170-375 % CPU on a *single* request — ONNX is already using
   every core it can get. This is a compute ceiling, not a queueing artefact,
   so more concurrency buys nothing.
2. **Two concurrent calls is already past the limit.** At ×2 the realtime
   factor is 0.9. The platform's own soft cap is ten concurrent calls
   (`worker_max_concurrent_calls`), and STT alone cannot serve two.
3. **Even uncontended, p95 is 2655 ms.** A single call with nothing else
   running has a tail four times its median. That alone is too slow.

**This reverses the conclusion of 2b.9 for this host.** Hosted
`gpt-4o-mini-transcribe` measured 1103 ms on a real call — slower than local's
*best* case and far faster than its p95, with none of the local CPU cost. The
548 ms figure that justified the switch was p50 against a completely idle
server with no call in progress, which is not a state that occurs while the
platform is running. Measuring a shared resource in isolation measures the
wrong thing.

The architecture is not wrong — §25 requires self-hosted models to be usable,
and they are. What is wrong is the hardware: this is a laptop with 7.75 GiB of
Docker memory also running a Kubernetes control plane and a second application
stack. Self-hosted STT needs a GPU or a dedicated box, and a larger Whisper
model on this host would be worse, not better.

**What follows for the plan:**

- Endpointing (2b.7) must not be tuned against local STT on this host. The
  input to the tuning would vary by 4x at p95 on an idle machine and 10x under
  any load.
- The repeated-measurement harness (2b.8) should report the realtime factor,
  not just latency. It is the figure that says whether a configuration can hold
  a conversation at all, and it is the one that made this obvious.
- Tenant choice of hosted or self-hosted STT is now a real decision with
  measured numbers behind it, which is what the catalog split in 4b.9 was for.
  **2b.9 encoded the gate (2026-09-12):** `stt_placement()` — self-hosted only
  when rtf ≥ 1.0 at ×2 and sequential p95 ≤ 2 s. This host: hosted.

#### Latency on this host is dominated by variance, not by the pipeline

Three browser-path calls against the same published version, same audio, same
endpoint, minutes apart:

| | call 1 | call 2 | call 3 |
|---|---|---|---|
| transcription | 2314 ms | 1661 ms | **11832 ms** |
| LLM first token | 5311 ms | 2272 ms | 2339 ms |
| time to first audio | 9220 ms | 5854 ms | 16250 ms |

The isolated benchmark of the same model on the same endpoint gave a warm p50
of 548 ms — measured against an idle server. These calls were made while that
server was being repeatedly hit, and `faster-whisper` on CPU is the thing
being contended.

**So no single-call number here is usable, including the ones quoted
approvingly earlier in this section.** A 5854 ms time-to-first-audio is not
evidence of anything except that one call took 5854 ms.

This is a finding about the measuring setup, not the product, and it changes
what should happen next: **endpointing cannot responsibly be tuned on this
host until STT latency is stable enough to measure.** Tuning a turn-taking
window against a transcription time that varies by 7x would fit the window to
the noise. That makes the repeated-measurement harness in **2b.8** a
prerequisite for 2b.7 rather than a later nicety, and it makes a second look at
where `faster-whisper` runs — thread count, model size, whether it has a GPU —
part of 2b.9 rather than closed.

#### Self-hosted STT is about half the latency, and that is not enough

Measured against the same speaches endpoint the agent now uses, transcribing
4.3 s of speech, 15 consecutive requests:

| | Local `faster-whisper-tiny` | OpenAI `gpt-4o-mini-transcribe` |
|---|---|---|
| p50 | **548 ms** | 1103 ms (from `call_20260911T063710`) |
| p95 | 733 ms | not measured |
| min | 489 ms | — |
| cold | **2.6–3.6 s** | n/a |

So item 2b.9 saves roughly 550 ms per turn. Applied to the failing call that
gives a projected time-to-first-audio near 4.2 s instead of 4.7 s — **still not
a conversation.** The dominant terms are unchanged: end-of-utterance detection
at 2581 ms and LLM time-to-first-token at 2243 ms. 2b.9 was worth doing and it
does not fix 2b.7.

It may still change the *symptom*, which is worth knowing before the next call
is judged. The failure was `transcript arrives after turn has been committed` —
a race between transcription and the endpointing window. Taking 550 ms out of
transcription makes the transcript more likely to land inside the window, so
the agent may begin replying without any endpointing change at all. If it does,
the underlying timing is still marginal and 2b.7 remains necessary.

**The cold start is a separate problem.** The first transcription after the
model is idle costs 2.6–3.6 s, because speaches unloads it. Every measurement
above discards the first three requests for that reason. On a real deployment
that cost lands on a caller, not on a benchmark — the first call after a quiet
period gets it. A keep-warm ping or a pinned model is the fix; recorded as part
of 2b.9 rather than discovered again during load testing.

The accuracy question is not answered here. The benchmark transcribed clean
synthetic speech at full bandwidth and got the sentence back verbatim; a real
call is 8 kHz telephony audio, where `tiny` is materially worse. If transcripts
degrade after this change, the answer is a larger local model
(`faster-whisper-base` or `-small`), not a return to the network.

#### A broken LLM produces a call that sounds fine

**Symptom:** calls connect, the greeting plays correctly, the call record shows
`COMPLETED` with a sensible duration — and the agent never answers anything the
caller says. In the worker log, `Error in _llm_inference_task` with
`AttributeError: 'NoneType' object has no attribute 'max_retry'`.

**Cause (defect):** the development echo adapter constructed its `LLMStream`
with `conn_options=None`. The base class reads `.max_retry` from it, so every
turn raised. **Fix:** pass a real `APIConnectOptions`
(`DEFAULT_API_CONNECT_OPTIONS` when the caller supplies none).

**Why it went unnoticed, which is the useful part:** the greeting is spoken by
`session.say()`, not by the model. A completely broken LLM therefore yields a
call that connects, greets the caller correctly, transitions through the state
machine, and completes with a normal duration. Every outside signal looks
healthy.

Two habits that would have caught it immediately:

1. **Verify a provider by exercising the adapter and reading its output**, not
   by the absence of errors on a call. A direct `generate()` call returning a
   real sentence is proof; a completed call is not.
2. **Treat missing per-turn observability as a blocker, not a nicety.** With no
   transcript rows and no latency metrics (both Phase 2), a conversation can
   only be judged from the outside — which is exactly how this hid.

#### Orphaned LiveKit resources after a failed sync

**Symptom:** LiveKit holds more dispatch rules than our records name.
**Cause (defect, partially):** interrupted delete-and-recreate cycles leave
resources behind. Detecting them needs a list-and-compare in the
LiveKit-to-database direction; a per-row check only finds the opposite case.
**Fix (Plan 5.7, 2026-09-12):** `detect_drift()` lists LiveKit trunks and
rules and compares them to PostgreSQL. Orphans surface on `/platform/livekit`
and in `make detect-drift`; they are not deleted automatically. A missing
mirrored ID is marked `DRIFTED` so Repair (`sync-livekit` / tenant
Synchronize) can recreate it.

#### Caller and called numbers silently swapped

**Symptom:** `calls.caller_number` empty while the room name plainly contains
the caller's number.
**Cause (defect):** `sip.phoneNumber` was used as a fallback for the *called*
number. It holds the **caller's** number.
**Why it mattered more than the empty column:** on a trunk that does not set
`sip.trunkPhoneNumber`, that fallback would have routed the call by the
caller's number and matched the wrong DID — a silent mis-route rather than a
visible failure.
**Fix:** the called number comes only from `sip.trunkPhoneNumber` or
`sip.calledNumber`, with no cross-fallback.

The authoritative attribute set from LiveKit 1.8, captured from a live call:

```json
{
  "sip.phoneNumber":      "15550001111",   // the CALLER
  "sip.trunkPhoneNumber": "1001",          // the number DIALLED
  "sip.trunkID":          "ST_...",
  "sip.ruleID":           "SDR_...",
  "sip.callID":           "SCL_..."
}
```

The worker logs this set at debug level on every call
(`sip_participant_attributes`). These names have changed across LiveKit
versions, so check the log rather than trusting this table after an upgrade.

#### `LOG_LEVEL=debug` has no effect in the worker

**Symptom:** debug diagnostics never appear, whatever `LOG_LEVEL` is set to.
**Cause (defect):** the logging wrapper passed LiveKit's CLI log level through
instead of this service's configured level, so `LOG_LEVEL` was silently
overridden. Found only while trying to debug something else — the failure mode
of a broken debug switch is that you conclude the code path never ran.
**Fix:** the platform's own `LOG_LEVEL` wins.

#### Worker prints CLI usage and exits

**Symptom:** the agents CLI help text, then exit.
**Cause (environment):** `cli.run_app` expects a subcommand (`start`, `dev`,
`download-files`).
**Fix:** `start` is injected when no known subcommand is present, so the
container command stays a plain `python -m worker.main`.

#### First call on a fresh pod is slow

**Cause (environment):** the Silero VAD weights download on first use.
**Fix:** `python -m worker.main download-files` runs at image build. Worth
keeping in mind for any model added later — a per-pod first-call download is
invisible in development and obvious in production.

#### VAD "slower than realtime" warnings

**Symptom:** `VAD inference is slower than realtime` and
`event loop blocked for ~100ms` on an 8 GB arm64 Docker Desktop.
**Assessment:** expected on a laptop, and a capacity signal rather than a bug.
It is exactly the kind of thing Phase 8 must measure on production-shaped
hardware instead of extrapolating from here (spec 75).

#### A provider row cannot be registered twice for two endpoints

Symptom: a self-hosted STT endpoint and the hosted service both need to be
selectable, and the second `INSERT` violates `uq_providers_kind_slug`. Renaming
one of them makes the worker raise `ProviderUnavailableError: no STT adapter for
provider 'selfhosted_speech'`.

Cause: `providers.slug` was doing two jobs — the row's unique name *and* the key
into the worker's adapter registry. One adapter therefore meant one row per
kind, and §25's "hosted or local, same protocol" could not be expressed at all.
The local fallback tier in §55 had nothing distinct to point at.

Fix: `providers.adapter`, nullable, falling back to the slug so every existing
row keeps resolving as before; `_VERSION_SQL` selects
`COALESCE(p.adapter, p.slug)`. The general lesson is worth keeping: a column
that is both a human name and a code contract will eventually need to be two
columns, and the constraint that reveals it will look unrelated.

#### A tenant credential quietly overrides a self-hosted provider's endpoint

A credential's `base_url` wins over the provider's `default_base_url`, which is
deliberate — it is how a tenant points at its own endpoint (§25). The surprise
is what it does to a *no-key* provider: the development seed stored an OpenAI
key with `base_url: https://api.openai.com/v1` against the self-hosted speech
row, because at that time there was only one row per kind. The result is a
provider called "self-hosted" whose calls go to OpenAI.

There is no bug to fix in the precedence; the fix is data. With distinct hosted
and self-hosted rows now seeded, move the key to the hosted row and leave the
self-hosted one without one. Worth checking before concluding that a local tier
is not being used: the tier may be resolving exactly as configured, to the
wrong place.

#### `MissingGreenlet` while serialising a saved draft

Symptom: `PUT /agents/{id}/draft` returns 500 with
`greenlet_spawn has not been called; can't call await_only() here`, naming
`updated_at`. The draft *is* saved — the audit row is written and the
transaction commits — so the console shows "could not save the draft" over a
change that landed.

Cause: `updated_at` carries `onupdate=func.now()`, so after an UPDATE its value
is in PostgreSQL and not in the instance. Pydantic then reads it from a
synchronous context and SQLAlchemy attempts lazy IO. `expire_on_commit=False`
does not help: the attribute is not expired, it was never loaded.

Fix: `await session.refresh(version)` before validating. What makes this worth
recording is *why it appeared when it did*: `onupdate` only fires when a column
actually changes, and the builder previously sent so few fields that a save was
often a no-op. Adding the provider tiers made every save a real UPDATE and
turned an intermittent 500 into a certain one. A latent fault that only
triggers on a genuine write is invisible in exactly the tests you would write
for it.

#### `NegotiationError: negotiation timed out` in the browser

Symptom: **Call Test** connects, the agent joins, and publishing the microphone
fails with `NegotiationError: negotiation timed out`. Nothing in the message
mentions ports, addresses or time.

**The first two explanations were wrong, and both were plausible.** The RTC TCP
port was genuinely mismatched — `livekit.yaml` said `7881` while compose
published `7981` — so a browser falling back to TCP dialled a closed port. That
was a real bug and fixing it did not fix this one. Guessing twice cost more than
measuring once would have.

**Measuring it.** Exposing the `Room` on `window` in development made the
publish path reproducible without a microphone, using a WebAudio oscillator as
the track:

```js
const ctx = new AudioContext(), osc = ctx.createOscillator();
const dst = ctx.createMediaStreamDestination();
osc.connect(dst); osc.start();
await room.localParticipant.publishTrack(dst.stream.getAudioTracks()[0],
                                         { source: 'microphone' });
```

It **succeeded, in 14,992 ms**. livekit-client's `peerConnectionTimeout`
defaults to 15,000 ms. Negotiation was losing a race with its own timeout by
eight milliseconds, which is why it looked like a hard failure rather than a
slow one.

**Cause:** fifty UDP media ports. Each becomes its own ICE candidate and its own
Docker Desktop forwarding entry, and gathering across all of them is what took
the time. The range was chosen to satisfy the port-range requirement in §11 and
was never needed here.

| | publish time |
|---|---|
| 50-port range | 14,992 ms |
| one muxed port (`rtc.udp_port`) | **7,206 ms** |

Muxing is also what LiveKit recommends in production, so the range was costing
something and buying nothing. `peerConnectionTimeout` is then raised to 45 s as
headroom rather than as the fix — this machine also runs a Kubernetes control
plane and a second application stack, and 7 s is not a number to leave three
seconds of margin against.

**The lesson is the one that keeps recurring in this section**: an aggregate
symptom with a plausible cause is not evidence. Two ports were misconfigured
and neither was the problem; the problem was a duration, and it took ten
seconds to measure once the path was reproducible.

#### A browser test call waited fifteen seconds before the agent did anything

Symptom, as reported: the agent "asks a question after connected" — but only
after a long pause — then does not answer what the caller says next.

Cause: the two waits were **sequential**. `_await_sip_participant` blocked for
its full fifteen-second timeout before the browser branch was even reached, so
every browser test call sat in silence for fifteen seconds, then greeted into a
conversation the caller had already started, having discarded whatever was said
in the meantime. It presents exactly as an agent that will not answer — which
is the defect this path was built to investigate, so the tool was manufacturing
the symptom it existed to study.

Fix: `_await_caller` waits for both kinds at once and takes the first. Measured
from the browser: agent joins at **2.0 s** and publishes audio at **4.0 s**,
against fifteen-plus before. A real call is unaffected, because no browser
participant arrives and the SIP branch resolves as it always did.

A test now asserts the two waits have not become sequential again, by name.

#### `NegotiationError` on every reply: the server was three versions behind

**The answer was in the server log the whole time, and I spent hours on the
network instead of reading it.**

```
974.99  mediaTrack published        | console-...
984.51  error reading data channel  | console-...     <- ten seconds later
984.54  participant closing
984.67  starting RTC session                          <- reconnect
999.78  participant closing                           <- and again
```

Preceded, every time, by `unsupported datachannel added`.

livekit-client 2.22 opens a datachannel that `livekit-server:v1.8` (protocol
15) does not understand. The server fails reading it, drops the participant,
and the client reconnects — every ten to fifteen seconds, forever. The agent's
reply renegotiates, the renegotiation lands in a reconnect, and it times out.

**Upgrading the server to v1.9 fixes it**, measured:

| | v1.8 | v1.9 |
|---|---|---|
| publish on an established connection | 40,622 ms | **54 ms** |
| reconnects in 14 s | one every 10-15 s | **none** |
| agent joins and publishes audio | 15 s+ | 1,611 ms |

The SIP path was re-tested immediately after, because it is what the upgrade
risked, and it is unaffected — greeting, caller speech, spoken reply, and the
trunk and dispatch rule survived the restart as `SYNCED`.

**What went wrong in the diagnosis, because it is the more useful lesson.**
Three network theories were pursued and each was a real misconfiguration:
a container IP in the ICE candidates, a renumbered RTC TCP port, and a
fifty-port UDP range. Fixing all three was worth doing and fixed none of this.
Meanwhile `unsupported datachannel added` sat in every log dump I took, and I
read past it because it was at info level and looked like noise.

Worse, I acted on the mismatch in the wrong direction first — downgrading the
client — on one timing sample, and shipped a regression that stopped the
caller's microphone working. The signal that would have pointed the right way
was also already present: the client prints *"Consider upgrading your LiveKit
server version"*, which says which side is old.

**The rule:** when a client and server disagree about a protocol, read which
one says the other is out of date. And a repeated ten-second interval in a log
is a timeout, not a coincidence — find what it belongs to before theorising
about anything else.

#### Signalling connects and media never does

Symptom: a browser joins the room, the console log shows `signal connected` and
`connected to Livekit Server`, and then the call fails with
`could not establish pc connection`. The agent is in the room; nobody hears
anything.

Cause: LiveKit advertises ICE candidates for `172.x`, its address on the Docker
bridge. Another container can route to that; a browser on the host cannot. The
candidate-pair stats show every pair `failed` with `responsesReceived: 0`.

Fix: `NODE_IP` set to this machine's LAN address, which both the host's browser
and the other containers can reach. **Not `127.0.0.1`** — inside every other
container that means the container itself, so it fixes the browser and breaks
the worker and SIP.

This is the same class of problem as `SIP_NAT_IP`, and the same fix. Worth
checking both after this machine changes network: `./scripts/lan-ip.sh`
reported `192.168.103.15` while `.env` still held `192.168.0.107` from a
previous network, which would have failed a PBX call in exactly this way.

#### A test that read a comment and reported on the code

The first assertion that the browser-test token grants nothing beyond
`room_join` searched the function's source text for `room_create`. It failed —
matching the comment that says *"No room_create, no room_admin, no
room_list"*. Written the other way round it would have passed for the same
reason, which is worse.

Rewritten to parse the function with `ast` and read the keywords actually
passed to `VideoGrants`. Any test that greps source text is really testing the
prose around it.

#### A browser client joins and no agent ever answers

Applies to any client of your own — the console's **Call Test** button handles
this itself.

Symptom: the client joins the room and appears as a participant, nothing ever
speaks, and the worker logs `no_caller` once the participant timeout expires.
No call row is written, so the attempt leaves no trace in the database either.

Two independent causes, either of which produces exactly this:

1. **The participant declared no DID.** The worker accepts a non-SIP caller
   only when one arrives in a participant attribute or the room's metadata
   (`did`, `test.did`, `sip.trunkPhoneNumber`). Without it there is no tenant
   to attribute the call to, and a call row with a null tenant would violate
   §6 — so it is refused, exactly as a SIP-less call is.
2. **No agent was dispatched into the room.** The worker registers under an
   explicit agent name, and LiveKit only auto-dispatches agents registered
   *without* one. A room created by hand therefore gets a participant, no
   agent, and silence. `make test-room` creates the room *and* the dispatch for
   this reason, and the console endpoint does both too.

LiveKit's own Agents Playground was evaluated and dropped: it mints its own
token and cannot declare which DID it is calling, so it only reaches an agent
through a room someone else prepared. It is not in the compose file.

#### Testing against a real PBX without touching its configuration

The development calls were placed from an existing FreeSWITCH/FusionPBX with
**no configuration changes** — no gateway, no dialplan entry:

```bash
fs_cli -x "originate {origination_caller_id_number=15550001111,\
sip_auth_username=lkdev,sip_auth_password=<password>}\
sofia/external/sip:1001@<livekit-sip-host>:5060 &park"
```

`sip_auth_username` / `sip_auth_password` are per-call channel variables, so
FreeSWITCH answers LiveKit's `407` challenge without a stored gateway.
Replacing `&park` with
`&playback(/usr/share/freeswitch/sounds/en/us/callie/ivr/8000/ivr-welcome_to_freeswitch.wav)`
gives the agent real speech to transcribe.

This is the right way to test a new environment: it proves the path before
anything on the PBX is modified, and it cannot break an existing deployment.

### 12.4 Phase 4c — AI Setup

#### AI Setup dropdown empty + "Failed to fetch" on agent configure

**Symptom:**
1. AI Setup page → click "Add LLM/STT/TTS/Embedding" → the provider dropdown inside the modal shows no options for any tab.
2. Agents page → click **Configure** on an existing agent → `TypeError: Failed to fetch` at `api.agents.versions()`.

**Cause (defect):** Migration `20260911_2330` was written with `ALTER TYPE providerkind ADD VALUE IF NOT EXISTS 'EMBEDDING'`, assuming a native PostgreSQL enum named `providerkind`. In this codebase, `enum_column()` uses `native_enum=False` throughout — `providers.kind` is a plain VARCHAR(64) column, not a native enum. PostgreSQL has no type named `providerkind` to alter.

When `make migrate` was run, the migration did `op.execute("COMMIT")` (which committed Alembic's own transaction mid-flight) and then `ALTER TYPE providerkind ...` failed with `type "providerkind" does not exist`. Because the transaction was already committed before the failure, Alembic could not record the migration as applied. The migration stayed in a permanent retry-fail loop, blocking migration `20260911_2331` from ever running.

Migration `20260911_2331` adds `embedding_provider_id` and `embedding_model_id` columns to `agent_versions`. Without those columns, the SQLAlchemy model includes them in every `SELECT agent_versions.*` — PostgreSQL rejects every such query with `column "embedding_provider_id" does not exist`. That crash propagates as "Failed to fetch" on the Configure dialog (Next.js in dev mode renders some server-side failures at the network level when the response body is malformed).

Because the catalog seeder runs separately and is idempotent, the new popular providers also did not exist in the database yet — hence the empty dropdowns.

**Fix (defect):**
1. Rewrote `20260911_2330` as a true no-op with an explanatory comment. The providers table needs no schema change; VARCHAR accepts any Python enum value without modification.
2. Ran `make migrate` — both revisions now applied successfully in one pass.
3. Ran `python -m app.cli seed-platform` — 29 providers seeded across all 4 kinds.

**Checklist for future enum additions to ProviderKind:**
- Add the Python value to `shared/models/enums.py`.
- Add seed entries to `app/services/seed.py` (the DB accepts them immediately — no migration needed for the providers table).
- Only add an Alembic migration if a *different* table gains a column or constraint referencing the new kind.
- Never write `ALTER TYPE providerkind ...` — there is no such type.

### 12.5 Phase 6 — Tools

#### Save as new draft 500s; Publish stays disabled after a model change

**Symptom:** On a published agent that already has tools (Service Agent, Development Agent), changing the language model leaves **Publish** disabled. **Save as new draft** returns Internal Server Error.
**Cause (defect):** Two stacked bugs. (1) `_replace_tool_grants` deleted every `agent_tools` row and inserted the same `(agent_version_id, tool_id)` pairs in one flush, tripping `uq_agent_tools_version_tool`. That is the save the builder always does: `_editable_draft` copies grants onto vN+1, then the form sends the same `tool_ids` back. (2) Publish was gated on the *saved* validation report. A live version with no draft is not publishable, so a local model change never enabled the button; Publish also did not persist first, so even a forced click would 409 (`there is no draft to publish`).
**Fix (defect):** Sync grants — delete only removed IDs, insert only new ones, leave existing rows. The builder tracks `dirty`; any field change enables Publish; Publish calls `saveDraft` then `publish`. Only `severity === "error"` issues block the button after a clean load.

#### Import refused, or a key appeared in an export

**Symptom:** Settings → Import file returns 400 "bundle contains a plaintext secret", or an export JSON contains an API key / tool auth secret.
**Cause:** The importer refuses any string on a secret-bearing key (`api_key`, `password`, `token`, `secret`, `ciphertext`, …) so a hand-edited file cannot sneak a credential in. A real export never writes those strings — only `configured` / `auth_configured` flags and a `key_hint`.
**Fix:** Re-export from Settings. Paste keys in **AI Setup** and tool auth on **Tools** after import. Agent versions land as drafts; Publish after credentials exist. Missing catalog slugs are skipped, not invented. Re-run `seed-platform` if Google/Azure/ElevenLabs STT rows are absent.

#### Human decline ended the caller instead of running fallback

**Symptom:** Warm transfer rings a human who rejects. The original caller is dropped; `transfer_fallback_taken` is empty.
**Cause (defect):** `_wait_for_disconnect` treated every `participant_disconnected` as the end of the call. The human SIP participant leaving mid-transfer resolved the wait and the finally block tore the room down before `WarmTransfer` could speak a fallback.
**Fix (defect):** Ignore `human-agent-*` disconnects until the legs are bridged. After bridge, either leg hanging up ends the call as before.

#### Adding a phone number with an agent assigned returns 500

**Symptom:** Phone Numbers → Add → `1802` + Service Agent → Add. The form shows an error and the row never appears. API log: `TypeError: Object of type UUID is not JSON serializable` on `INSERT INTO audit_logs`.
**Cause (defect):** `audit.snapshot` copied `inbound_agent_id` (and the other UUID FKs) onto `new_value`. JSONB encoding uses the stdlib encoder, which cannot dump `uuid.UUID` or `Enum`. The INSERT failed, the transaction rolled back, and the DID was never committed. Seeded numbers existed because seed writes the row without going through this API path.
**Fix (defect):** `audit.snapshot` / `audit.record` coerce UUIDs, enums and datetimes to JSON-safe values. Create/update/delete/enable of a DID also enqueue a trunk sync so LiveKit's accepted list includes the new number (the SIP wizard already did this; the Phone Numbers form did not).

---

### 12.2 Bringing up a new environment

The order that avoids most of the above:

1. `make preflight` — settle host ports before anything starts.
2. `make up`, then `make migrate`, then `make health`.
3. `./scripts/lan-ip.sh` and set `SIP_NAT_IP`. Without it, calls connect and
   have no audio.
4. Seed the platform and a tenant, then `sync-livekit`, then `show-config` and
   confirm LiveKit matches the records.
5. Probe reachability from the PBX with a SIP `OPTIONS` before attempting a
   call. A `200 OK` separates network problems from configuration problems.
6. Place one call with `originate`. If it fails, set `SIP_LOG_LEVEL=debug`
   first — the info-level SIP logs do not say why.

### 12.3 Notes for later phases

Recorded now because they will be cheaper to handle deliberately than to
rediscover:

| Phase | Watch for |
|---|---|
| 2 | Transcript persistence is not implemented yet; `call_transcript_segments` stays empty until item 2.7. The pipeline runs without it, so its absence is easy to mistake for an STT failure. |
| 2 | **No per-turn observability yet.** Metric definitions exist but the pipeline does not populate them, so a conversation can only be judged from the outside. This is what made a broken LLM look like a working call for several attempts — items 2.5 and 2.7 close it, and they are worth doing early in the phase rather than last. |
| 2 | The greeting is spoken by `session.say()`, not the model. Any check that treats "the caller heard the greeting" as evidence the pipeline works will pass on a completely broken LLM. |
| 2 | The duplicate-log fix covers the main worker process. Job subprocesses forward records to the parent, which can re-emit them; check log volume per call before load testing. |
| 4 | `inbound_numbers` semantics (§12.1) must be encoded in the dispatch-rule UI, or every tenant will hit the same `486 flood`. |
| 5 | Orphan detection is the LiveKit-to-database direction (5.7). Per-row GETs still miss leftovers from a failed delete-and-recreate. |
| 5 | `UpdateSIPInboundTrunk` may still be unimplemented; keep the delete-and-recreate path and the dependent-rule rebuild. |
| 7 | IP allow-listing must be verified on real infrastructure. It cannot be validated on Docker Desktop at all. |
| 6 | An existing stack that was seeded before 6.1 has Service Agent but no `create_ticket` grant. Re-run `seed-dev-tenant` — it is idempotent and adds the builtin library plus the allow-list. |
| 8 | **RTC media is one muxed UDP port** in development, not a range — a range cost 15 s of ICE gathering per call through Docker Desktop (§12.1). Muxing is also LiveKit's production advice, so the Helm values should mux too rather than widening a range. SIP's RTP range is still 50 ports, roughly two per call, and does need widening before load testing or concurrency caps near 25 calls for reasons that look like LiveKit faults. |

---

## 13. Milestones

| M | Milestone | Closes | Meaning |
|---|---|---|---|
| M1 | First successful AI call | Phase 1 | The architecture is real, not theoretical |
| M2 | Production-quality voice at 10 calls | Phase 2 | It sounds good enough to sell |
| M3 | Provable tenant isolation | Phase 3 | It is safe to onboard a second customer |
| M4 | Self-service configuration | Phase 4 | Onboarding no longer needs an engineer |
| M5 | LiveKit fully platform-managed | Phase 5 | Operations no longer need the CLI |
| M6 | Agents with tools, RAG, and transfer | Phase 6 | Feature-complete against the spec |
| M7 | Production-ready infrastructure | Phase 7 | Deployable, observable, recoverable |
| M8 | Measured capacity + final acceptance | Phase 8 | The 1,000-call claim is earned |

---

## 14. Cross-Phase Traceability

Every spec section maps to the phase that delivers it.

| Spec § | Topic | Phase |
|---|---|---|
| §1–2 | Objective, two-plane architecture | 0–1 |
| §3–4 | Stack, containerization | 0 |
| §5 | Production architecture | 7 |
| §6–7 | Multi-tenancy, isolation | 1 (schema) · 3 (enforcement) |
| §8 | Authentication, RBAC | 3 |
| §9–10 | Configuration-driven, Configuration API | 1 · 4 |
| §11–13 | LiveKit management, sync, infra split | 1 (minimum) · 5 (full) |
| §14–17 | PBX, SIP trunks, wizard, DIDs | 4 |
| §18–19 | Agent management, versioning | 4 |
| §20–21 | Routing, dispatch rules | 1 (minimum) · 4 · 5 |
| §22–23 | Per-call architecture, worker | 1 |
| §24–27 | Provider abstraction, providers, ElevenLabs, voices | 1 (interfaces) · 4 · 6 |
| §28–29 | Realtime pipeline, barge-in | 2 |
| §30–32 | Tools, tool builder, permissions | 6 |
| §33 | Knowledge base / RAG | 6 |
| §34 | Conversation memory | 2 (basic) · 6 (summarization) |
| §35–36 | Human transfer, warm-transfer announcement + summary whisper (CR-1) | 6 |
| §37–38 | Business hours, fallback routing | 4 |
| §39–41 | Recording, transcription, call DB | 1 (call DB) · 2 |
| §42–43 | State machine, correlation ID | 1 |
| §44–45 | Redis, configuration cache | 1 |
| §46 | Drift detection | 5 |
| §47–48 | Call limits, capacity dashboard | 4 · 5 |
| §49–52 | Worker scaling, HPA, shutdown, HA | 7 |
| §53–54 | Security, secrets | 3 · 7 |
| §55 | Provider failure handling | 6 |
| §56–59 | Latency, monitoring, logging, health | 0 · 2 · 7 |
| §60–62 | Consoles, Agent Builder UI | 4 |
| §63–64 | Configuration + dependency validation | 4 |
| §65 | Import/export | 6 |
| §66 | Backup and DR | 7 |
| §67–68 | API structure, schema | 1 |
| §69 | Audit logs | 3 |
| §70 | Testing | all phases |
| §71–75 | Load, sustained, failure testing, capacity, no fake scalability | 7 (failure) · 8 |
| §76 | Development phases | this document |
| §77–78 | Acceptance test, final call flow | 8 |
| §79–80 | Architectural rule, dev instructions | all phases |

---

## 15. Decisions Needed Before They Block Work

Carried from PRD §21. Each is listed against the phase where it stops being deferrable.

| Q | Decision | Needed by |
|---|---|---|
| Q1 | LiveKit self-hosted vs Cloud | **Phase 1** — determines whether §11/§13 cluster administration is even possible |
| Q4 | Isolation via RLS, application scoping, or both | **Phase 3** — structural choice, expensive to retrofit |
| Q5 | Launch languages/locales | **Phase 4** — drives provider and voice selection |
| Q6 | Knowledge-base corpus size per tenant | **Phase 6** — decides whether pgvector suffices |
| Q7 | Recording/transcript retention policy | **Phase 7** — backup and DR design depends on it |
| Q8 | Recording consent per jurisdiction | **Before production traffic** — legal exposure, not a feature flag |
| Q9 | Target production cloud | **Phase 7** — Helm values and secret backend |
| Q3 | Billing engine vs usage records | **Phase 4** — shapes `usage`/`billing`/`subscriptions` |
| Q2 | Outbound calling in scope | Phase 4 (schema keeps the option open regardless) |
| Q10 | "Campaign" as a routing attribute vs an entity | Phase 4 |

---

## 16. Definition of Done (Programme)

The platform is done when every acceptance criterion in PRD §19.2 passes, and:

1. A tenant administrator completes the entire §77 journey through the UI — no code, no SQL,
   no SSH, no LiveKit CLI.
2. The Control Plane and Voice Execution Plane are separate services, and the Control Plane
   handles no realtime audio.
3. No tenant-specific value is hard-coded anywhere.
4. Tenant isolation is proven by explicit tests, not asserted.
5. Capacity is stated from measurement, with the load-test dataset attached.
6. Documentation covers development, deployment, configuration, and operations.

> The resulting project must be production-oriented, modular, testable, observable, and capable
> of evolving from a small deployment into a 1,000+ concurrent-call multi-tenant SaaS
> platform. [§80]
