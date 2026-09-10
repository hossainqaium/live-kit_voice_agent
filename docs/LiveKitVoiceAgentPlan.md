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
| 1.12 | Test PBX (Asterisk or FreePBX in Docker) wired to LiveKit SIP | §14 |

### Deliverables

- Component design docs (the seven headings) for: Configuration API, LiveKit module, AI Agent Worker.
- Initial Alembic migration containing the complete §68 schema.
- A recorded walkthrough of one successful call, with the log trace filtered by `call_id`.

### Exit criteria

- A real phone call into the test PBX is answered by the AI agent, holds a short spoken
  exchange, and hangs up cleanly.
- A single `grep` on `call_id` returns the full lifecycle across every service.
- The `calls` row is complete and correct: timestamps, duration, status, hangup reason.
- The dispatch rule is reused across at least ten consecutive calls — none created per call.
- No tenant-specific value appears anywhere in worker source code.

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

## 6. Phase 3 — Multi-Tenancy [§76]

### Goal

Tenants, users, RBAC, and **provably enforced** tenant isolation — before any tenant-facing
feature is built on top. [§80]

### Scope

| # | Work item | Spec |
|---|---|---|
| 3.1 | `tenants`, `users`, `roles`, `permissions`, `user_roles` implemented and seeded | §68 |
| 3.2 | JWT authentication; tenant identity derived **only** from the token | §7, §53 |
| 3.3 | Platform roles `SUPER_ADMIN`, `PLATFORM_OPERATOR`; tenant roles `TENANT_ADMIN`, `MANAGER`, `AGENT_MANAGER`, `ANALYST`, `VIEWER` | §8 |
| 3.4 | Granular permissions: `agents.*`, `pbxs.*`, `sip_trunks.*`, `calls.read`, `recordings.read`, `analytics.read`, `users.manage`, `billing.read` | §8 |
| 3.5 | RBAC enforced at the API layer, independent of the frontend | §8, §80 |
| 3.6 | Tenant-scoping in a shared repository layer so no endpoint can forget it; PostgreSQL RLS as defense in depth | §7 |
| 3.7 | Reject any request that carries a client-supplied tenant ID | §7 |
| 3.8 | Explicit cross-tenant isolation test suite over every tenant-scoped endpoint | §7 |
| 3.9 | Audit logging: user, tenant, timestamp, action, resource, resource ID, old value, new value, IP | §69 |
| 3.10 | Credential encryption at rest for provider and SIP credentials | §53 |

### Deliverables

- Security model document: authentication, authorization, tenant-resolution, encryption.
- Isolation test suite report — one case per tenant-scoped endpoint.
- Audit log schema and event catalogue.

### Exit criteria

- Every tenant-scoped endpoint has a passing test proving Tenant A cannot read or mutate a
  Tenant B resource, for all resource types listed in §7.
- A forged/injected `tenant_id` in a request body or query string is ignored or rejected — never
  honored.
- Every mutating endpoint writes an audit record with old and new values.
- Permission checks are exercised by unit tests, with the frontend absent.
- No provider or SIP credential is readable in plaintext from the database.

### Out of scope

The full configuration UI (Phase 4), LiveKit resource management UI (Phase 5).

### Risks

| Risk | Mitigation |
|---|---|
| A single forgotten `WHERE tenant_id = ...` breaks the whole isolation guarantee | Make scoping structural (3.6) rather than per-query discipline; add RLS as a second layer |
| RBAC checks drift as endpoints are added | Permission declaration lives on the route; a test asserts every route declares one |

---

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

## 12. Milestones

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

## 13. Cross-Phase Traceability

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

## 14. Decisions Needed Before They Block Work

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

## 15. Definition of Done (Programme)

The platform is done when all eighteen acceptance criteria in PRD §19.2 pass, and:

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
