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
[`LiveKitVoiceAgentREADME.md`](./LiveKitVoiceAgentREADME.md)

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
| 2b.1 | Enforce `silence_timeout_seconds`. Loaded from the agent version today and ignored. | **defect** | §18, §29 |
| 2b.2 | Enforce `max_call_duration_seconds`. Same: loaded and ignored. Needs a watchdog, since no session option covers it. | **defect** | §18 |
| 2b.3 | Honour `recording_enabled` — start LiveKit egress to object storage, write `call_recordings` metadata, leave the audio out of PostgreSQL. Loaded and ignored today, so a tenant enabling recording gets nothing. | **defect** | §39 |
| 2b.4 | Map `interruption_enabled` and `interruption_min_words` onto the session's own options rather than reimplementing them. | mapping | §29 |
| 2b.5 | Conversation summarisation into `call_transcripts.summary`, which is also what the warm-transfer whisper reads. | feature | §34, §36 |
| 2b.6 | Grafana dashboard for the six voice-latency metrics. They are exposed and scraped; nothing charts them. | feature | §56, §57 |
| 2b.7 | Barge-in and endpointing verified against real speech. **Now evidence-backed and the highest priority in this phase**: a real call produced three caller utterances and no reply, because transcripts arrived after their turn was committed. See §12.1. | **defect** | §29, §56 |
| 2b.9 | Move STT to the self-hosted endpoint and re-measure. ~1.1s of the 4.7s time-to-first-audio is network transcription latency. | **defect** | §25, §56 |
| 2b.8 | 10-concurrent-call harness. | feature | §76 |

**Most of 2b.1, 2b.2 and 2b.4 is a mapping exercise.** `AgentSession` already
accepts `user_away_timeout`, `min_interruption_duration`,
`min_interruption_words` and `allow_interruptions`; the work is passing
configuration into them. Only maximum call duration needs new logic.

**Exit criteria**

- A call with a 10-second silence timeout ends by itself after 10 seconds of
  silence, with `hangup_reason = SILENCE_TIMEOUT`.
- A call exceeding its maximum duration ends with `hangup_reason = MAX_DURATION`.
- An agent with recording enabled produces an object in storage and a
  `call_recordings` row; PostgreSQL holds no audio.
- No setting in `agent_versions` is loaded at runtime and then ignored — the
  property worth asserting in a test, so this class of defect cannot recur.
- The latency dashboard renders p50 and p95 per stage.

### Phase 4b — Configuration Completion

**Goal:** the §77 criterion holds — a tenant administrator configures
everything through the UI, with no CLI step left in the path.

Both consoles are built and every §60/§61 section has a working screen, so what
is left here is specific and bounded rather than "finish the UI".

| # | Item | Kind | Spec |
|---|---|---|---|
| 4b.4 | **Tenant provider credentials through the API and console.** CLI-only today (`set-credential`), so a new tenant cannot reach a cloud provider without shell access. This is the one item blocking §77. Write-only field, Fernet at rest, never returned, key hint only — the constraints the CLI already honours. | feature | §26, §62, §77 |
| 4b.3 | **Routing engine evaluation at call setup.** Rules, business hours and the fallback chain are all configurable and none is consulted when a call arrives; the DID's directly assigned agent answers. A tenant building a rule today gets a row, not a behaviour. | **defect** | §20, §37, §38 |
| 4b.2 | DID form fields for routing rule, business hours and fallback. The columns exist and the API accepts them; the form does not offer them. | gap | §17 |
| 4b.1 | SIP Configuration Wizard — the 10 steps in §16 as a guided sequence. Every field is already reachable through the trunk and DID forms, so this is onboarding ergonomics, not capability. | feature | §16 |
| 4b.5 | Voice preview. §27 lists Test among the voice actions; needs a synthesis endpoint and a stored sample. | feature | §27 |
| 4b.6 | ElevenLabs TTS adapter with streaming. Only the OpenAI-compatible adapter exists, and the catalog will happily offer a provider nothing can drive. | feature | §26 |

**4b.3 is the most consequential and is listed as a defect for the same reason
as 2b.1–2b.3:** the console now presents routing rules, business hours and a
fallback chain as working configuration. A screen that writes a row nothing
reads is worse than no screen, because the operator has no way to tell.

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

### Phase 3b — Isolation and Account Hardening

**Goal:** the isolation guarantee holds even for a query written outside the
repository, and accounts are manageable through the API rather than the CLI.

| # | Item | Kind | Spec |
|---|---|---|---|
| 3b.1 | PostgreSQL Row Level Security on every tenant-owned table, as the second layer beneath `TenantRepository`. | feature | §7 |
| ~~3b.2~~ | User management endpoints — create, list, disable, assign roles. **Done**: `/users` for tenant users, `/platform/users` for platform staff, both with a console screen. | ~~feature~~ | §8, §61 |
| ~~3b.3~~ | Password reset. **Done**: `POST /users/{id}/password` and the platform equivalent, both revoking sessions in the same change — a reset that leaves old tokens working is not a reset. Self-service change by the signed-in user is still open (**3b.3a**). | ~~feature~~ | §53 |
| 3b.3a | Self-service password change, for a user rotating their own password without an administrator. | feature | §53 |
| ~~3b.4~~ | Audit read endpoint. **Done**: `GET /platform/audit-logs`, filterable, with no write or delete route anywhere in the API. | ~~feature~~ | §69 |
| 3b.5 | Audit coverage asserted across every mutating endpoint, rather than trusting that each one remembered. | verification | §69 |

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
| 1b.1 | Tenant call limits are enforced at call acceptance, but `max_daily_calls` and `max_monthly_minutes` rely on the `usage` rollup, which nothing writes yet. Only `max_concurrent_calls` is genuinely enforced. | **defect** | §47 |

**Exit criteria:** a tenant at its daily call limit has the next call rejected
before a LiveKit room is created, and the `usage` table is populated as calls
complete.

### Sequencing

**The ordering constraint was not met, and that is now the position to work
from rather than a plan to make.** 2b.1–2b.4 were meant to land before the
agent builder exposed those fields; the builder shipped first. The same has
since happened again with routing: the console presents rules, schedules and a
fallback chain that nothing evaluates at call setup (4b.3).

So the priority is the enforcement gap, not more screens:

1. **4b.3** — routing evaluation. The largest gap between what the console
   shows and what a call does.
2. **2b.7 and 2b.9** — endpointing and STT latency. The agent transcribes and
   does not reply; see §12.1. Nothing else in the product matters while that
   holds.
3. **2b.1–2b.4** — the agent-version settings that are loaded and ignored.
4. **4b.4** — provider credentials in the console, which is what §77 turns on.
5. **1b.1** — the usage rollup, so two of the three call limits stop being
   decorative.

3b.1 (Row Level Security) has no ordering constraint and can run in parallel.

**The lesson worth keeping:** a form is not a feature. Each of 2b.1–2b.3 and
4b.3 is a screen that writes a row nothing reads, and in every case the screen
was built first because it was the visible half. The test named in Phase 2b's
exit criteria — *no setting is loaded at runtime and then ignored* — should be
extended to cover configuration the console writes, not only fields the worker
loads.

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
| 4.7 | Routing UI + routing engine — conditions on tenant, PBX, SIP trunk, DID, caller number, destination number, business hours, campaign, priority | §20 |
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
| 4.3 | SIP Configuration Wizard (10 steps) | **4b.1** — the fields are all reachable through the trunk and DID forms; the guided sequence is not built |
| 4.4 | Phone Number / DID UI | **partly** — number, PBX, trunk and inbound agent are editable; routing rule, business hours and fallback are not yet on the form (**4b.2**) |
| 4.5 | Agent Builder UI | **done** |
| 4.6 | Agent versioning | **done** |
| 4.7 | Routing UI + engine | **UI done**; engine evaluation at call setup is **4b.3** |
| 4.8 | Business hours | **done**, including dated exceptions and `open_now` |
| 4.9 | Fallback routing chain | **configurable**; runtime traversal is **4b.3** |
| 4.10 | Provider UI — providers, models, credentials | **providers and models done**; tenant credential entry is CLI-only (**4b.4**) |
| 4.11 | Voice Library UI | **done** except voice preview (**4b.5**) |
| 4.12 | ElevenLabs TTS adapter | **4b.6** — only the OpenAI-compatible adapter exists |
| 4.13 | Validation before publish | **done** |
| 4.14 | Dependency validation tree | **done** — `GET /agents/{id}/versions/{n}/validate` returns issues and dependencies separately |
| 4.15 | Tenant call limits | **partly** — concurrency enforced; daily and monthly are Plan 1b.1 |
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
| 5.1 | LiveKit administration section — Clusters, SIP Configuration, SIP Trunks, Dispatch Rules, Agent Dispatch, Rooms, Participants, Media, Codecs, Recording/Egress, TURN/ICE, Health, Metrics | §11 |
| 5.2 | LiveKit SIP trunk management fully API-driven from the UI | §11, §15 |
| 5.3 | Dispatch rule management as long-lived objects; per-call creation explicitly prevented | §21 |
| 5.4 | Agent dispatch configuration per agent/DID | §21 |
| 5.5 | Resource synchronization flow: `UI → API → validate → PostgreSQL → LiveKit API → store LiveKit resource ID` | §12 |
| 5.6 | Sync status `SYNCED`/`PENDING`/`FAILED`/`DRIFTED` with Synchronize, Retry, Repair actions | §12 |
| 5.7 | Drift detection job comparing PostgreSQL against LiveKit; surfaces "Configuration Drift Detected" | §46 |
| 5.8 | Infrastructure/tenant configuration split — Redis, ports, RTP ranges, external IP, TLS, LBs, Kubernetes, networking, firewall, topology hidden from tenant admins | §13 |
| 5.9 | Asynchronous processing for all LiveKit operations | §80 |
| 5.10 | Platform capacity dashboard — Total/Available Capacity, LiveKit Nodes, SIP Nodes, AI Workers, Worker Utilization, CPU, Memory, Network, Provider Health | §48 |

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
| 6.1 | Tools / function calling in the pipeline — `get_customer()`, `check_order()`, `create_order()`, `cancel_order()`, `check_inventory()`, `send_sms()`, `send_email()`, `transfer_call()`; assignable per agent | §30 |
| 6.2 | API Tool Builder UI — name, description, method, URL, auth, headers, request/response schema, timeout, retry policy | §31 |
| 6.3 | Variable substitution — `{{customer_id}}`, `{{order_id}}`, `{{caller_number}}` | §31 |
| 6.4 | Tool schema validation before agent publish | §31 |
| 6.5 | Tool permissions — explicit per-agent allow-list; everything else denied | §32 |
| 6.6 | Knowledge bases from PDF, DOCX, TXT, CSV, web content; ingestion pipeline | §33 |
| 6.7 | RAG retrieval on PostgreSQL + pgvector; knowledge bases assignable to agents | §33 |
| 6.8 | Conversation summarization for long conversations | §34 |
| 6.9 | **Warm transfer back to the PBX** — two simultaneous audio paths: caller hears the configurable announcement ("Your call is being transferred to a human agent. Please wait.") plus hold media, while the human agent leg is dialled through the PBX. Destinations: SIP extension, PBX extension, PBX queue, external number, SIP URI | §35, CR-1 |
| 6.10 | **Summary whisper to the human agent** — TTS summary played into the agent leg only, with hard audio isolation so the caller cannot hear it; DTMF skip; bounded maximum duration; bridge only after the agent has heard it and accepted | §36, CR-1 |
| 6.10a | Transfer summary content — Customer, Reason, Summary, Actions Taken, Order Information, Sentiment, Required Next Action; configurable spoken template plus optional structured delivery to the PBX where supported | §36, CR-1 |
| 6.10b | `transfer_status` lifecycle: `REQUESTED → ANNOUNCING → DIALING_AGENT → WHISPERING_SUMMARY → BRIDGED`, with `AGENT_NO_ANSWER` / `AGENT_BUSY` / `AGENT_REJECTED` / `FAILED` / `ABANDONED` branches into the Phase 4 fallback chain; per-attempt timings persisted | §38, §41, CR-1 |
| 6.10c | AI leaves on bridge; the call continues human-to-human with recording and transcription intact, the human segment attributed to the `Human Agent` speaker type | §39, §40, CR-1 |
| 6.11 | Remaining provider adapters — all STT, LLM, TTS providers in §25 | §25 |
| 6.12 | Provider resilience — timeout, retry, exponential backoff, circuit breaker, fallback provider | §55 |
| 6.13 | Tenant configuration import/export, with secrets never exported in plaintext | §65 |

### Deliverables

- Troubleshooting entries in §12.
- Tool execution design doc: sandboxing, timeouts, retry semantics, variable resolution.
- RAG design doc: chunking, embedding model, retrieval strategy, per-tenant partitioning.
- Provider failover matrix — which provider falls back to which, per kind.

### Exit criteria

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

**Cause (environment):** with `mod_xml_curl`, FusionPBX generates the dialplan
XML and caches it at `/var/cache/fusionpbx/dialplan.<domain>`. Saving through
the GUI invalidates that cache; **a direct database write does not.**

**Fix, in order of preference:**

1. Open the entry in Dialplan Manager and press **Save** — this also validates
   that the inserted rows render correctly.
2. **Advanced → Cache → Flush Cache** in the GUI.
3. `rm -f /var/cache/fusionpbx/dialplan.<domain>` and `reloadxml`.

Note that the cache **file** is group-writable by `www-data` while the
**directory** is not, so a member of that group can rewrite the file in place
but cannot delete it. Editing the cache directly is a last resort: validate
that the result still parses as XML and that the extension count is unchanged
before writing, because a corrupt cache costs the whole domain its dialplan
until the next flush.

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
**Fix:** `SipResourceManager.list_dispatch_rules` and `list_inbound_trunks`
support this; wiring it into a scheduled reconciliation is Phase 5 (item 5.7).
Until then, orphans accumulate quietly and cost nothing except confusion.

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
| 5 | Orphan detection needs the LiveKit-to-database direction, not just per-row checks. |
| 5 | `UpdateSIPInboundTrunk` may still be unimplemented; keep the delete-and-recreate path and the dependent-rule rebuild. |
| 7 | IP allow-listing must be verified on real infrastructure. It cannot be validated on Docker Desktop at all. |
| 8 | Media port ranges are 50 ports each in development, roughly two per call. Widen them in the Helm values before load testing, or concurrency caps out around 25 calls for reasons that look like LiveKit faults. |

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
