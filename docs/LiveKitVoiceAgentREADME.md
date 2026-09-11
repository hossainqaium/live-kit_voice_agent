# Multi-Tenant AI Voice Agent Platform (LiveKit)

A production-oriented, multi-tenant SaaS platform for AI-powered voice agents, built on
**LiveKit** as the realtime media and SIP/telephony layer.

Calls arrive from external PBXs, SIP trunks, or PSTN-connected systems, pass through LiveKit
SIP into a per-call LiveKit room, and are answered by an AI Agent Worker whose entire behavior
— prompt, STT/LLM/TTS providers, voice, tools, knowledge base, transfer rules, call policies —
is loaded from configuration at call start.

> **Nothing tenant-specific is hard-coded.** The AI Agent Worker is a generic execution engine;
> everything it does comes from the Control Plane.

**Related documents**
- [`LiveKitVoiceAgentPRD.md`](./LiveKitVoiceAgentPRD.md) — requirements, acceptance criteria, traceability to the Master Requirements Specification
- [`LiveKitVoiceAgentPlan.md`](./LiveKitVoiceAgentPlan.md) — phased implementation plan

---

## Table of Contents

1. [Architecture](#1-architecture)
2. [Technology Stack](#2-technology-stack)
3. [Repository Layout](#3-repository-layout)
4. [Prerequisites](#4-prerequisites)
5. [Quick Start (Local Development)](#5-quick-start-local-development)
6. [Configuration and Environment Variables](#6-configuration-and-environment-variables)
7. [Database and Migrations](#7-database-and-migrations)
8. [Connecting a PBX](#8-connecting-a-pbx)
9. [Creating Your First Agent](#9-creating-your-first-agent)
9a. [**Running and Testing the Voice Agent**](#9a-running-and-testing-the-voice-agent)
9c. [**Configuring AI Providers**](#9c-configuring-ai-providers)
9d. [**Loading and Testing the Interfaces**](#9d-loading-and-testing-the-interfaces)
10. [API Surface](#10-api-surface)
11. [Roles and Permissions](#11-roles-and-permissions)
12. [Observability](#12-observability)
13. [Testing](#13-testing)
14. [Load Testing](#14-load-testing)
15. [Production Deployment](#15-production-deployment)
16. [Operations Runbook](#16-operations-runbook)
17. [Security Notes](#17-security-notes)
18. [Troubleshooting](#18-troubleshooting)
19. [Contributing Rules](#19-contributing-rules)

---

## 1. Architecture

The system is split into two planes that never blur together.

### Control Plane

Configuration, administration, authentication, authorization, tenants, agents, routing,
provider configuration, LiveKit resource management, monitoring, business configuration.

```
Web UI
  |
  v
Configuration / Management API  ──►  LiveKit APIs
  |                        |
  v                        v
PostgreSQL              Redis / Event System
```

**The Control Plane never touches the realtime audio stream.**

### Voice Execution Plane

Realtime calls and AI processing.

```
PBX / SIP / PSTN
      |
      v
  LiveKit SIP
      |
      v
    LiveKit  ──► SIP Dispatch Rule ──► Unique LiveKit Room ──► Agent Dispatch
                                                                    |
                                                                    v
                                                          AI Agent Worker Pool
                                                            |     |     |
                                                           STT   LLM   TTS
                                                            |     |     |
                                                          LiveKit Room
                                                                |
                                                                v
                                                          SIP / RTP → PBX
```

**The Voice Execution Plane contains no tenant-specific configuration.**

### Per-call sequence

```
1. PBX sends INVITE to LiveKit SIP
2. LiveKit SIP matches a long-lived dispatch rule (never created per call)
3. LiveKit creates a unique room for the call
4. Agent dispatch assigns the call to an available AI Agent Worker
5. Worker resolves: tenant → PBX → SIP trunk → DID → routing rule → agent → published version
6. Worker loads the full agent configuration once and caches it for the call's lifetime
7. Realtime loop: audio → VAD/turn detection → STT → LLM (+tools, +RAG) → TTS → audio
8. On transfer: warm handoff back to the PBX — caller hears the announcement while the human
   agent is dialled and whispered the AI summary, then the legs are bridged
9. On hangup: persist call record, transcript, recording metadata
```

### Data ownership

| Store | Holds |
|---|---|
| **PostgreSQL** | Configuration source of truth, calls, transcripts, usage, audit logs, pgvector embeddings |
| **Redis** | Active call state, temporary state, distributed locks, rate limits, caches, worker coordination, config events |
| **Object storage** (S3/MinIO) | Recordings and large files — never in PostgreSQL |
| **LiveKit** | Realtime media and SIP execution; resource IDs are mirrored into PostgreSQL |

---

## 2. Technology Stack

| Layer | Choice |
|---|---|
| Backend | Python · FastAPI · Pydantic · SQLAlchemy · Alembic · AsyncIO |
| Frontend | React · Next.js · TypeScript (responsive, desktop-administration oriented) |
| Database | PostgreSQL + pgvector |
| Cache / distributed state | Redis |
| Realtime | LiveKit Server · LiveKit SIP · LiveKit Agents · WebRTC · SIP · RTP |
| Object storage | S3 / MinIO / any S3-compatible |
| Monitoring | Prometheus · Grafana (optional: Loki, Tempo, OpenTelemetry) |
| Packaging | Docker · Docker Compose (dev) · Kubernetes + Helm (production) |

---

## 3. Repository Layout

The intended structure. Do **not** collapse this into a monolith.

```
.
├── docs/
│   ├── LiveKitVoiceAgentPRD.md
│   ├── LiveKitVoiceAgentREADME.md
│   └── LiveKitVoiceAgentPlan.md
│
├── services/
│   ├── configuration-api/          # Control Plane — FastAPI
│   │   ├── app/
│   │   │   ├── api/v1/             # routers: pbxs, sip_trunks, agents, routing, catalog,
│   │   │   │                     #   admin, platform, browser_test (the one that
│   │   │   │                     #   issues a credential — see §9d.7)
│   │   │   ├── core/               # settings, security, JWT, RBAC, tenant context
│   │   │   ├── db/                 # SQLAlchemy models, session, repositories
│   │   │   ├── schemas/            # Pydantic request/response models
│   │   │   ├── services/           # business logic (agents, routing, validation)
│   │   │   ├── livekit/            # LiveKit admin client, sync, drift detection
│   │   │   └── main.py
│   │   ├── alembic/
│   │   └── tests/
│   │
│   ├── ai-agent-worker/            # Voice Execution Plane — LiveKit Agents
│   │   ├── worker/
│   │   │   ├── entrypoint.py       # job accept → resolve config → run session
│   │   │   ├── config_loader.py    # loads + caches agent version for the call
│   │   │   ├── pipeline/           # VAD, turn detection, barge-in, session loop
│   │   │   ├── providers/
│   │   │   │   ├── base.py         # STTProvider / LLMProvider / TTSProvider
│   │   │   │   ├── stt/            # deepgram, whisper, elevenlabs, google, azure
│   │   │   │   ├── llm/            # openai, anthropic, gemini, local
│   │   │   │   └── tts/            # elevenlabs, cartesia, openai, google, azure, deepgram
│   │   │   ├── tools/              # HTTP tool executor, variable substitution
│   │   │   ├── rag/                # pgvector retrieval
│   │   │   ├── transfer/           # summary generation, destination handling
│   │   │   └── resilience/         # spec 55. Fallback is done — the provider chain
│   │   │                         #   becomes a LiveKit FallbackAdapter. Timeout,
│   │   │                         #   retry, backoff and circuit breaker are NOT:
│   │   │                         #   this package is still only a docstring (4b.10)
│   │   └── tests/
│   │
│   ├── frontend/                   # Next.js — platform console + tenant console
│   │   ├── app/                    # one directory per console section
│   │   ├── components/
│   │   │   ├── Shell.tsx           # sidebar, navigation, identity
│   │   │   ├── ProviderChain.tsx   # STT/LLM/TTS per tier, keys, connection test
│   │   │   ├── BrowserCall.tsx     # the Call Test panel (livekit-client)
│   │   │   └── ui.tsx              # buttons, fields, dialogs, badges, toasts
│   │   └── lib/                    # typed API client; no tenant ID anywhere in it
│   │
│   └── shared/                     # cross-service Python package (pip-installable)
│       ├── pyproject.toml
│       ├── shared/
│       │   ├── logging/            # structured JSON logging, correlation context
│       │   ├── models/             # shared enums: call/transfer states, roles, permissions
│       │   └── telemetry/          # Prometheus metric definitions
│       └── tests/
│
├── deploy/
│   ├── docker-compose.yml
│   ├── docker-compose.override.yml.example
│   ├── livekit/                    # livekit.yaml, livekit-sip.yaml
│   ├── prometheus/
│   ├── grafana/
│   └── helm/                       # production charts, one per scalable component
│
├── tests/
│   ├── integration/                # PBX → SIP → LiveKit → agent → STT/LLM/TTS
│   ├── e2e/                        # real SIP calls
│   └── load/                       # progressive + sustained load harness
│
├── scripts/
│   ├── preflight.py                # port-collision check, run by `make up`
│   └── postgres-init/              # extensions created at first initialisation
│
├── .github/workflows/ci.yml        # secret scan, lint, types, tests, images, compose
├── pyproject.toml                  # repository-wide ruff and mypy configuration
├── .env.example
└── Makefile
```

---

## 4. Prerequisites

| Requirement | Notes |
|---|---|
| Docker + Docker Compose | v2 compose syntax |
| Python 3.11+ | for running services outside containers |
| Node.js 20+ | frontend |
| A SIP-capable PBX | Asterisk, FreePBX, FreeSWITCH, FusionPBX, Kamailio, 3CX, Cisco, Mitel, or any SIP-compatible system |
| Provider API keys | at least one each of STT / LLM / TTS |
| Public UDP reachability | required for RTP once you test with real calls |

> Realtime media needs the RTP port range reachable from the PBX. On a laptop behind NAT,
> use a PBX on the same LAN, or a host with a public IP.

---

## 5. Quick Start (Local Development)

```bash
git clone <repo-url> livekit-voice-agent && cd livekit-voice-agent
```

`make up` creates `.env` with generated development secrets, checks for port
collisions, and starts all ten services:

```bash
make up
```

The Compose environment contains the ten services the specification requires:
`postgresql`, `redis`, `livekit`, `livekit-sip`, `configuration-api`, `ai-agent-worker`,
`frontend`, `minio`, `prometheus`, `grafana`.

Apply migrations:

```bash
make migrate
```

Verify every service is up:

```bash
make health
```

Run `make help` to see every target.

### Local endpoints

This project owns a **dedicated host-port block** so it can run alongside other stacks on the
same machine. Ports like 5432, 6379, 8000, 3000, 9000 and 9090 are commonly already taken;
a collision surfaces as `port is already allocated` partway through startup, leaving the stack
half-up. `make up` runs the preflight check first, and you can run it on its own:

```bash
make preflight
```

| Service | URL / port | Container port |
|---|---|---|
| Frontend | http://localhost:3200 | 3000 |
| Configuration API | http://localhost:8200 | 8000 |
| OpenAPI docs | http://localhost:8200/docs | 8000 |
| AI agent worker health | http://localhost:8202/health | 8081 |
| LiveKit | ws://localhost:7980 | 7880 |
| LiveKit RTC (TCP) | localhost:7981 | 7881 |
| LiveKit metrics | http://localhost:6889/metrics | 6789 |
| PostgreSQL | localhost:5435 | 5432 |
| Redis | localhost:6381 | 6379 |
| MinIO API | http://localhost:9200 | 9000 |
| MinIO console | http://localhost:9201 | 9001 |
| Prometheus | http://localhost:9290 | 9090 |
| Grafana | http://localhost:3201 | 3000 |

Every host port above is remappable in `.env`. **Three are not**, because the protocol
advertises them and a host/container mismatch breaks calls rather than erroring loudly:

| Fixed | Why |
|---|---|
| SIP `5060/udp` + `5060/tcp` | `livekit-sip` announces its own port in SIP `Via`/`Contact` headers |
| RTP `10100-10149/udp` | advertised in SDP |
| RTC `50100/udp` | advertised in ICE candidates |
| RTC `7981/tcp` | advertised in ICE candidates |

To change one of those, change it in `deploy/docker-compose.yml` **and** the matching LiveKit
config (`deploy/livekit/livekit-sip.yaml` `sip_port` / `rtp_port`, or
`deploy/livekit/livekit.yaml` `rtc.udp_port` / `rtc.tcp_port`) together. A mapping that
renumbers one of these — `7981:7881`, say — points a browser at a closed port and the call
fails with `NegotiationError: negotiation timed out`, naming neither the port nor the time.

**RTC media is one muxed UDP port, not a range.** It was a 50-port range, and that turned out
to cost real latency: every port becomes its own ICE candidate and its own Docker Desktop
forwarding entry, and gathering across fifty of them took about 15 seconds — past
livekit-client's negotiation timeout. Muxing brought it to ~7 s and is what LiveKit recommends
in production anyway. Spec 11's port-range administration belongs in the Helm values, where
the range actually buys something.

RTP for SIP is still a range (`10100-10149/udp`), roughly two ports per concurrent call, so
this supports a handful of simultaneous calls: enough for Phases 1–2, widened in the Helm
values for load testing.

### Health checks

```bash
curl -fsS http://localhost:8200/health && curl -fsS http://localhost:8200/ready
```

Every service exposes `/health` (process is alive, no dependencies consulted) and `/ready`
(required dependencies verified). Liveness deliberately ignores PostgreSQL and Redis: if it
did not, a brief database blip would get every container killed and turn a partial outage into
a full one.

---

## 6. Configuration and Environment Variables

`.env` is for **local development only**. Production credentials come from a secret manager —
see [§17](#17-security-notes).

### Core

| Variable | Purpose |
|---|---|
| `ENVIRONMENT` | `development` \| `staging` \| `production` |
| `LOG_LEVEL` | `debug` \| `info` \| `warning` \| `error` |
| `API_BASE_PATH` | defaults to `/api/v1` |

### PostgreSQL

| Variable | Purpose |
|---|---|
| `POSTGRES_HOST`, `POSTGRES_PORT` | connection target |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | credentials |
| `DATABASE_URL` | full async DSN, overrides the parts above |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | connection pooling |

### Redis

| Variable | Purpose |
|---|---|
| `REDIS_URL` | e.g. `redis://redis:6379/0` |
| `REDIS_CALL_STATE_TTL_SECONDS` | expiry for active-call keys |

### Auth

| Variable | Purpose |
|---|---|
| `JWT_SECRET` / `JWT_PRIVATE_KEY` | token signing |
| `JWT_ALGORITHM` | e.g. `RS256` |
| `ACCESS_TOKEN_TTL_MINUTES`, `REFRESH_TOKEN_TTL_DAYS` | session lifetimes |
| `CREDENTIAL_ENCRYPTION_KEY` | envelope key for encrypting stored provider/SIP credentials |

### LiveKit

| Variable | Purpose |
|---|---|
| `LIVEKIT_URL` | How *services* reach LiveKit: `ws://livekit:7880` |
| `LIVEKIT_PUBLIC_URL` | How a *browser* reaches it: `ws://localhost:7980`. Not the same value, and the API hands this one to the console — see §9d.7 |
| `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | Admin API credentials, and what join tokens are signed with. Compose builds `LIVEKIT_KEYS` from them, so no key lives in `livekit.yaml` — generate the secret with `openssl rand -hex 32` |
| `LIVEKIT_NODE_IP` | The address LiveKit advertises in ICE candidates. **Defaults to `SIP_NAT_IP`**, because they are the same fact and two variables holding it means one goes stale. `make refresh-ip` updates it |
| `LIVEKIT_IMAGE_TAG` | Server version, pinned (`v1.9`). v1.8 answers protocol 15 and the current browser SDK opens a datachannel it rejects — see §12.1 of the Plan |
| `LIVEKIT_SIP_URI` | SIP entry point advertised to PBXs |
| `LIVEKIT_RTP_PORT_RANGE` | Infrastructure-controlled; not tenant-visible |
| `ALLOW_BROWSER_TEST_SESSIONS` | Lets the console mint a join token for a test call (§9d.7). Development only |
| `ALLOW_BROWSER_TEST_PARTICIPANT` | Lets the worker answer a browser caller. Development only |

### Object storage

| Variable | Purpose |
|---|---|
| `S3_ENDPOINT_URL` | MinIO endpoint locally |
| `S3_BUCKET_RECORDINGS` | recordings bucket |
| `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_REGION` | credentials |

### AI providers

Provider credentials are **tenant/platform configuration stored encrypted in PostgreSQL** and
managed through the UI — not environment variables. The `.env` keys below exist only to
bootstrap local development before any provider is configured in the database.

| Variable | Purpose |
|---|---|
| `BOOTSTRAP_DEEPGRAM_API_KEY` | dev-only STT bootstrap |
| `BOOTSTRAP_OPENAI_API_KEY` | dev-only LLM/STT/TTS bootstrap |
| `BOOTSTRAP_ANTHROPIC_API_KEY` | dev-only LLM bootstrap |
| `BOOTSTRAP_ELEVENLABS_API_KEY` | dev-only TTS bootstrap |

### Worker

| Variable | Purpose |
|---|---|
| `WORKER_MAX_CONCURRENT_CALLS` | soft cap per worker; the real number comes from load testing |
| `WORKER_DRAIN_TIMEOUT_SECONDS` | graceful-shutdown budget on `SIGTERM` |
| `WORKER_AGENT_NAME` | agent-dispatch identity registered with LiveKit |

---

## 7. Database and Migrations

PostgreSQL is the configuration source of truth. Schema is designed **before** business logic.

Minimum tables:

```
tenants
users · roles · permissions · user_roles
pbxs · sip_trunks · sip_credentials · phone_numbers
agents · agent_versions · agent_tools · agent_voices
providers · provider_credentials · models · voices
routing_rules · business_hours · transfer_destinations
knowledge_bases · knowledge_documents
tools · tool_permissions
calls · call_events · call_transcripts · call_transcript_segments · call_recordings
usage · billing · subscriptions
audit_logs
```

**Every tenant-owned table carries `tenant_id` and enforces tenant isolation.**

Create a migration:

```bash
docker compose -f deploy/docker-compose.yml exec configuration-api alembic revision --autogenerate -m "add routing_rules"
```

Apply migrations:

```bash
docker compose -f deploy/docker-compose.yml exec configuration-api alembic upgrade head
```

Roll back one revision:

```bash
docker compose -f deploy/docker-compose.yml exec configuration-api alembic downgrade -1
```

### LiveKit resource mirroring

Rows that back a LiveKit resource store its LiveKit ID plus a sync state:

| Column | Meaning |
|---|---|
| `livekit_*_id` | e.g. `livekit_trunk_id`, `livekit_dispatch_rule_id` |
| `sync_status` | `SYNCED` · `PENDING` · `FAILED` · `DRIFTED` |
| `last_synced_at` | last successful reconciliation |
| `sync_error` | last failure detail |

Reconciliation offers **Synchronize**, **Retry**, and **Repair** from the UI/API.

---

## 8. Connecting a PBX

Use the **SIP Configuration Wizard** in the tenant console — no config-file editing:

```
1. Select PBX        6. Connection Test
2. SIP Configuration 7. Phone Number
3. Authentication    8. AI Agent
4. Codec             9. Routing
5. Security         10. Complete
```

On completion the platform: validates the input, writes PostgreSQL rows, creates the matching
LiveKit SIP trunk and dispatch rule through the LiveKit API, stores the returned LiveKit
resource IDs, and marks the records `SYNCED`.

On the PBX side, point an outbound trunk/route for the DID at `LIVEKIT_SIP_URI`, and allow the
LiveKit SIP signalling and RTP ranges through your firewall.

**Test Connection** on both the PBX and SIP trunk records verifies reachability before you
route live traffic.

---

## 9. Creating Your First Agent

In the tenant console → **AI Agents** → **Create**:

1. **Identity** — name, description, language, greeting, system prompt
2. **Speech** — STT provider + model
3. **Reasoning** — LLM provider + model, temperature
4. **Voice** — TTS provider + model + voice from the Voice Library
5. **Behavior** — interruption, silence timeout, maximum call duration
6. **Capture** — recording, transcription
7. **Capability** — tools (explicitly allow-listed), knowledge base
8. **Escalation** — transfer policy and destinations, business rules

Then **Save Draft → Test → Publish**.

Publishing validates STT, LLM, TTS, voice, prompt, tools, knowledge base, transfer destination,
routing, PBX, SIP trunk, and DID. Invalid configuration cannot become production-active.

### Versioning behavior

- Versions carry state: `v1 Published`, `v2 Draft`, `v3 Testing`.
- Publishing a new version **does not affect calls already in progress**.
- In-flight calls finish on the version they started with.
- New calls pick up the current published version.
- **Rollback** re-publishes a previous version.

### Warm transfer to a human agent

When an agent escalates, the platform performs a **warm (attended) transfer back to the
tenant's PBX**. The caller and the human agent hear different audio at the same time:

```
              AI decides to transfer
                        |
          +-------------+-------------+
          |                           |
     CALLER LEG                  AGENT LEG
          |                           |
  "Your call is being         Dial human agent
   transferred to a            through the PBX
   human agent.                       |
   Please wait."              Agent answers
          |                           |
  Announcement / hold         Whisper AI summary
  media continues             (agent only — the
          |                    caller cannot hear it)
          |                           |
          |                    Agent accepts
          +-------------+-------------+
                        |
                Bridge both legs
                        |
          AI leaves; call continues human-to-human
```

Configurable per tenant and per agent:

| Setting | Purpose |
|---|---|
| Announcement text or audio | What the caller hears when the transfer starts. Default: *"Your call is being transferred to a human agent. Please wait."* Rendered in the agent's language and voice. |
| Hold media | What continues playing while the human agent is being reached. Silence is not an option. |
| Spoken summary template | The whisper the human agent hears, built from the seven summary fields — Customer, Reason, Summary, Actions Taken, Order Information, Sentiment, Required Next Action. |
| Maximum whisper duration | Caps how long the caller waits while the summary plays. |
| DTMF skip key | Lets the human agent jump straight to the caller. |
| Fallback chain | Where the caller goes if the agent does not answer, is busy, or rejects: secondary agent → PBX queue → voicemail. |

Two guarantees worth stating plainly, because they are what a customer notices:

- **The caller never hears the summary.** The whisper is published only into the agent leg's
  audio path. This is verified by recording both legs separately and asserting the caller's
  leg is free of it.
- **The caller is never dropped into silence.** Every failure branch — no answer, busy,
  rejected, technical failure — lands on the configured fallback.

Progress is visible on the call record as `transfer_status`: `REQUESTED → ANNOUNCING →
DIALING_AGENT → WHISPERING_SUMMARY → BRIDGED`, with `AGENT_NO_ANSWER`, `AGENT_BUSY`,
`AGENT_REJECTED`, `FAILED` and `ABANDONED` as the alternative outcomes.

After the bridge the AI leaves, and the call continues human-to-human with recording and
transcription intact — the human portion attributed to the `Human Agent` speaker type.

---

---

## 9a. Running and Testing the Voice Agent

This is the end-to-end path from a cold checkout to a phone call answered by
the AI agent. Everything here is verified working against a real
FreeSWITCH/FusionPBX.

### 9a.1 Numbers used in this guide

Two numbers matter, and confusing them is the most common cause of a failed
test call:

| | Value here | What it is |
|---|---|---|
| **Agent number (DID)** | `1801` | The number the caller **dials**. LiveKit matches it against the SIP trunk's `numbers`, and the platform resolves it to a tenant and agent through `phone_numbers`. |
| **Caller number** | `3001` (or any extension) | The number the call appears to come **from**. Only used for caller-based routing conditions, analytics, and the transfer summary. |

Their LiveKit attribute names are easy to swap, so for reference:

```
sip.trunkPhoneNumber  →  1801    the AGENT number that was dialled
sip.phoneNumber       →  3001    the CALLER's number
```

Both are configurable; nothing about `1801` is special. Change the agent number
with `--did`, and the caller number per call.

**Choose the agent number to avoid collisions.** A PBX numbers its extensions
from some base — FusionPBX starts at 1000 — and an agent number that collides
with a real extension reaches that extension instead. The symptom is reaching a
colleague, not an error. Check the PBX's full extension list, not just the
registered ones, before picking. `18xx` was chosen here because the entire
range was unused.

### 9a.2 Start the platform

```bash
make up
```

```bash
make migrate
```

```bash
make health
```

All four checks must read `healthy` / `ready` before continuing. If a port is
already taken, `make up` stops at the preflight check and names the holder.

### 9a.3 Tell LiveKit which address to advertise

Skip this and calls will connect with **no audio** — signalling succeeds while
media goes to an unroutable address.

```bash
./scripts/lan-ip.sh
```

Put the result in `.env` as `SIP_NAT_IP`, then:

```bash
docker compose -f deploy/docker-compose.yml --env-file .env up -d livekit-sip
```

### 9a.4 Seed a tenant and agent

The console now covers this (§9d.1). The CLI remains the way to do it without a
browser, and is what seeds the same rows the UI
will write. `--pbx-host` is your PBX's address; `--did` is the agent number.

```bash
docker compose -f deploy/docker-compose.yml --env-file .env exec configuration-api python -m app.cli seed-dev-tenant --did 1801 --pbx-host 192.168.0.113
```

This creates a tenant, a PBX record, a SIP trunk, the agent number, an AI agent
with a **published** version, a routing rule, and a dispatch rule. The agent is
configured with local STT and TTS, so no provider API key is needed to get a
first call working.

### 9a.5 Create the LiveKit resources

Seeding writes the database only. Shaping LiveKit is a separate, explicit step:

```bash
docker compose -f deploy/docker-compose.yml --env-file .env exec configuration-api python -m app.cli sync-livekit
```

Then confirm LiveKit agrees with the records:

```bash
docker compose -f deploy/docker-compose.yml --env-file .env exec configuration-api python -m app.cli show-config
```

Expected shape — note the trunk carries the agent number, and the rule targets
the worker's agent name:

```
LiveKit inbound trunks:
  ST_xxxxxxxx  Development Trunk  numbers=['1801']  allowed=[]

LiveKit dispatch rules:
  SDR_xxxxxxx  Development Dispatch  trunks=['ST_xxxxxxxx']  prefix=dev-call-  agents=['voice-agent']

our records:
  trunk Development Trunk: livekit=ST_xxxxxxxx status=SYNCED
  rule  Development Dispatch: livekit=SDR_xxxxxxx status=SYNCED agent=voice-agent
```

`agents=['voice-agent']` must match the worker's `WORKER_AGENT_NAME`. If it does
not, calls connect to a room where nobody answers.

Confirm the worker is registered:

```bash
docker compose -f deploy/docker-compose.yml --env-file .env logs ai-agent-worker | grep "registered worker"
```

### 9a.6 The trunk's SIP credentials

The seed in §9a.4 provisions these automatically and **prints the password
once**:

```
  SIP trunk credentials — shown once, not recoverable:
    username       lkdev
    password       <generated>
```

Capture it then. The stored copy is encrypted and cannot be read back
(spec 54), and re-running the seed will not regenerate it — inventing a new
password would silently break a working trunk.

The trunk authenticates inbound calls with SIP digest auth rather than an IP
allowlist. That is better practice generally, and **required** on Docker
Desktop for macOS, which rewrites inbound source addresses so an allowlist can
never match — see
[Plan §12.1](./LiveKitVoiceAgentPlan.md#121-phase-1--basic-call).

If you lose the password, set a new one on the trunk and re-run `sync-livekit`
so LiveKit receives it.

### 9a.7 Check reachability before calling

A `200 OK` here separates a network problem from a configuration problem, and
takes seconds. Run from the PBX:

```bash
printf 'OPTIONS sip:LIVEKIT_HOST:5060 SIP/2.0\r\nVia: SIP/2.0/UDP PBX_HOST:5060;branch=z9hG4bK-probe\r\nFrom: <sip:probe@PBX_HOST>;tag=p1\r\nTo: <sip:LIVEKIT_HOST:5060>\r\nCall-ID: probe-1\r\nCSeq: 1 OPTIONS\r\nContent-Length: 0\r\n\r\n' | nc -u -w 4 LIVEKIT_HOST 5060
```

### 9a.8 Place a test call

**No PBX configuration change is required.** `originate` takes a full SIP URI,
and the credentials travel as per-call channel variables, so no gateway or
dialplan entry is needed — and nothing on an existing PBX is touched.

Ring the agent and hold the call open:

```bash
fs_cli -x "originate {origination_caller_id_number=15550001111,sip_auth_username=lkdev,sip_auth_password=YOUR_PASSWORD}sofia/external/sip:1801@LIVEKIT_HOST:5060 &park"
```

`+OK <uuid>` means the agent answered. `-ERR NO_ANSWER` means it did not — see
§9a.10.

To give the agent real speech to transcribe, **keep `&park` and broadcast into
the live call** once the greeting has finished:

```bash
UUID=$(uuidgen | tr 'A-Z' 'a-z')
fs_cli -x "originate {origination_uuid=$UUID,origination_caller_id_number=15550001111,sip_auth_username=lkdev,sip_auth_password=YOUR_PASSWORD}sofia/external/sip:1801@LIVEKIT_HOST:5060 &park"
sleep 12   # let the greeting play out
fs_cli -x "uuid_broadcast $UUID /usr/share/freeswitch/sounds/en/us/callie/ivr/8000/ivr-welcome_to_freeswitch.wav aleg"
sleep 20   # give the agent time to answer
fs_cli -x "uuid_kill $UUID"
```

**Do not use `originate … &playback(...)` instead.** FreeSWITCH hangs up as
soon as the file ends — 2.6 s for that clip — which is before the agent's
greeting finishes. The result is a call with audio flowing in both directions,
a `COMPLETED` row, and **zero transcript segments**: indistinguishable from a
broken pipeline, and this document recommended it for some time.

Speaking over the greeting has the same effect for a different reason: the
greeting is agent speech, so the caller's audio arrives as barge-in and the
turn is discarded. That is what the `sleep 12` is for.

A successful run looks like this:

```
AI     : Hello. You are through to the development voice agent…
CALLER : Welcome to FreeSwitch, the future up to Lafini.
AI     : Thank you! How can I assist you today?
```

Note the transcript. The recording says *"the future of telephony"* — that is
`faster-whisper-tiny` on 8 kHz telephony audio, and it is why Plan 2b.9 is
still open.

To hear the agent yourself, register a softphone to the PBX and dial the agent
number.

#### Testing the dialplan from the CLI

`originate user/<ext>@<domain> &transfer(...)` **cannot** test a
domain-context dialplan: an originated leg *to* an extension is an outbound
call and takes the `default` context, so a dialplan in the domain context is
never reached. Setting `context=` on the channel or passing a context to
`transfer` does not change it.

Use a loopback channel, which does enter a chosen context:

```bash
fs_cli -x "originate {origination_caller_id_number=3001}loopback/1801/YOUR_DOMAIN/XML &park"
```

The dial string is `loopback/<destination>/<context>/<dialplan>`.
`-ERR MANDATORY_IE_MISSING` on the loopback A-leg is **normal** and does not
mean the test failed — judge it by the dialplan match in the FreeSWITCH log
and by whether a call row appears.

### 9a.8b Calling the agent from a PBX extension

`originate` bypasses the dialplan entirely, which is why it needs no PBX
configuration. A desk phone dialling a number goes *through* the dialplan, so
until a route exists the call never leaves the PBX.

Two ways to bridge that gap, in increasing order of permanence.

#### Beware the number collision

FusionPBX numbers extensions from 1000 upward, so an agent number of `1001` is
very likely an existing extension. Dialling it from a phone would reach that
extension, not the agent, and the symptom is "my call went to a colleague"
rather than an error.

Pick a trigger number nothing else uses — `1801` here, or a feature code like
`*8001`. It does **not** have to equal the agent number: the dialplan can map
a free trigger onto the configured agent number, so nothing on the platform
side changes.

Check first:

```bash
fs_cli -x "list_users" | grep -E "^1001|,1001,"
```

#### Option 1 — ring your extension, no dialplan change

Calls your phone, and bridges it to the agent when you answer. Nothing on the
PBX is modified, so this is the right first test.

```bash
fs_cli -x "originate user/1000@YOUR_DOMAIN &bridge({origination_caller_id_number=1000,sip_auth_username=lkdev,sip_auth_password=YOUR_PASSWORD}sofia/external/sip:1801@LIVEKIT_HOST:5060)"
```

Replace `1000` with your extension and `YOUR_DOMAIN` with its FusionPBX domain
(`fs_cli -x "sofia status profile internal" | grep -i alias` if unsure).

Your phone rings first, so you hear the agent's greeting from the beginning.
Reversing the order — calling the agent first and bridging your phone second —
loses the greeting while your phone is still ringing.

#### Option 2 — dial a number from the phone, permanent

One **additive** dialplan entry. It creates a new route and touches nothing
existing, so it is safe to add and trivial to remove.

In FusionPBX: **Dialplan → Dialplan Manager → add**, in your extension's
domain/context:

| Field | Value |
|---|---|
| Name | `livekit-voice-agent-dialplan` |
| Order | e.g. `310` — before your outbound routes, after local extensions |
| Condition | `destination_number` matches `^1801$` |
| Action 1 | `set` → `effective_caller_id_number=${caller_id_number}` |
| Action 2 | `set` → `effective_caller_id_name=${caller_id_name}` |
| Action 3 | `bridge` → `{sip_auth_username=lkdev,sip_auth_password=YOUR_PASSWORD}sofia/external/sip:1801@LIVEKIT_HOST:5060` |

**The credentials belong inside the bridge dial string, in `{...}` — not as
`set` actions.** A `set` applies to the A-leg and never reaches the outbound
leg that `bridge` creates, so the caller never answers LiveKit's `407` and the
call dies after a successful match. This is the single easiest way to lose an
hour here: the dialplan matches, the bridge executes, and the call still fails.
See [Plan §12.1](./LiveKitVoiceAgentPlan.md#121-phase-1--basic-call).

The two `set` actions pass the real extension through as the caller identity,
so `calls.caller_number` shows who rang rather than a placeholder.

The condition and the bridge both use `1801` here because the agent number is
free on this PBX. They do not have to match: if your chosen agent number
collides with an extension, match a free trigger number in the condition and
target the configured agent number in the bridge.

Then reload and dial `1801` from the phone:

```bash
fs_cli -x "reloadxml"
```

### FusionPBX specifics

Two things that will otherwise waste your afternoon:

- **The dialplan lives in the database, not in files.** With `mod_xml_curl`,
  `/etc/freeswitch/dialplan/` holds only `empty.xml` and edits there do
  nothing. Use Dialplan Manager, or insert into `v_dialplans` and
  `v_dialplan_details`.
- **FusionPBX caches the generated XML** at
  `/var/cache/fusionpbx/dialplan.<domain>` and only invalidates it on a GUI
  save. A correct SQL insert therefore appears to do nothing until you flush
  the cache (GUI **Advanced → Cache → Flush**, or delete the file and
  `reloadxml`).

If you insert by SQL, also set `app_uuid` to the dialplans app's own uuid
(the first uuid in `/var/www/fusionpbx/app/dialplans/app_config.php`).
FusionPBX's list query filters with `app_uuid <> '<inbound routes uuid>'`, and
`NULL <> x` is NULL rather than TRUE in SQL — so a row with a null `app_uuid`
works perfectly but is **invisible in Dialplan Manager**.

Equivalent XML, if you manage dialplans as files rather than through FusionPBX:

```xml
<extension name="livekit-voice-agent-dialplan" continue="false">
  <condition field="destination_number" expression="^1801$">
    <action application="set" data="effective_caller_id_number=${caller_id_number}"/>
    <action application="set" data="effective_caller_id_name=${caller_id_name}"/>
    <action application="bridge" data="{sip_auth_username=lkdev,sip_auth_password=YOUR_PASSWORD}sofia/external/sip:1801@LIVEKIT_HOST:5060"/>
  </condition>
</extension>
```

To remove it: delete the dialplan entry (or the file) and `reloadxml`. Nothing
else was changed.

#### A gateway instead of a direct bridge

Tidier if several routes will point at the platform, but note that LiveKit SIP
**does not accept SIP registrations** — the gateway must be created with
`register` set to `false`, with the username and password used only to answer
LiveKit's digest challenge. A registering gateway will sit in permanent retry
and is a common way to spend an afternoon.

#### If the phone dials and nothing happens

| Symptom | Cause |
|---|---|
| Reaches a colleague's phone | The trigger number collides with a real extension. Pick another. |
| Fast busy immediately | No dialplan match. Check the order value and the context/domain. |
| `407` loop in the SIP logs | `sip_auth_password` does not match the trunk's. |
| Rings then drops, `486 flood` | The dispatch rule does not match — see §9a.10. |
| Connects but silent | `SIP_NAT_IP` unset or wrong (§9a.3). |

Watch the routing decision live while you dial:

```bash
fs_cli -x "console loglevel debug" && fs_cli
```

### 9a.9 Verify what happened

Follow the worker as the call runs:

```bash
docker compose -f deploy/docker-compose.yml --env-file .env logs -f ai-agent-worker
```

The expected sequence, all sharing one `call_id`:

```
sip_participant_joined      called_number=1001  caller_number=15550001111
call_configuration_loaded   agent_name=...  version_number=1
call_state_changed          ANSWERED     -> AI_CONNECTED
call_state_changed          AI_CONNECTED -> IN_PROGRESS
conversation_started
call_state_changed          IN_PROGRESS  -> COMPLETED
conversation_ended
```

Then the call record:

```bash
docker compose -f deploy/docker-compose.yml --env-file .env exec postgresql psql -U voice_agent -d voice_agent -x -c "SELECT call_id, state, did, caller_number, duration_seconds, hangup_reason FROM calls ORDER BY created_at DESC LIMIT 1;"
```

A healthy result: `state = COMPLETED`, `did` = the agent number,
`caller_number` = the caller, a non-zero `duration_seconds`, and a
`hangup_reason`.

And the state transitions, which are the audit trail behind that record:

```bash
docker compose -f deploy/docker-compose.yml --env-file .env exec postgresql psql -U voice_agent -d voice_agent -c "SELECT from_state, to_state, occurred_at FROM call_events ORDER BY occurred_at DESC LIMIT 5;"
```

Every investigation starts from `call_id`. It appears in every call-related log
line in every service, and on the call, transcript and recording rows.

### 9a.10 When the call does not connect

Work outwards from the caller. Each step rules out everything before it.

| Symptom | First thing to check |
|---|---|
| `-ERR NO_ANSWER`, nothing in the SIP logs | Reachability (§9a.7). If `OPTIONS` gets no reply, it is the network, not the platform. |
| `486 Busy`, `reason: flood` | **Not rate limiting.** It means the call matched no dispatch rule. Confirm the rule's trunk is the one LiveKit currently holds, and that `inbound_numbers` is empty — that field matches the *caller's* number, not the dialled one. |
| `407 Unauthorized`, repeatedly | The trunk's credentials and the ones in `originate` disagree. Re-run `sync-livekit` after changing them. |
| Call answers, then silence | `SIP_NAT_IP` is unset or wrong (§9a.3). Signalling works; media has nowhere to go. |
| Call answers, no agent joins | `agents=[...]` on the dispatch rule does not match `WORKER_AGENT_NAME`, or no worker is registered. |
| `call_not_routable` in the worker log | No active `phone_numbers` row matches the dialled number, or its agent has no published version. |
| `call_rejected_tenant_limit` | The tenant is at its concurrent-call limit — working as intended (spec 47). The dev seed sets it deliberately low. |

Before investigating a rejected call, turn up the SIP logs. The info level does
not say why a call was refused:

```bash
SIP_LOG_LEVEL=debug docker compose -f deploy/docker-compose.yml --env-file .env up -d livekit-sip
```

The debug line carries a `caller` field naming the source location, which is
what distinguishes one `486` from another.

### 9a.11 Current limitations

Honest about what this does and does not do yet, so a test result is not
misread as a fault:

| | Status |
|---|---|
| Inbound call from a PBX extension, agent answers, speaks a greeting | Working |
| Dedicated dialplan entry routing a chosen number to the agent | Working |
| Call record, state machine, correlation ID across services | Working |
| Speech to text | Working, **but `faster-whisper-tiny` is not accurate enough on 8 kHz telephony audio**: a real call transcribed "the future of telephony" as "The Future Up to Lathany". Latency is fine (1296 ms on that call); quality is not. Hosted STT or a larger local model — Plan 2b.9. |
| Language model | Working — OpenAI `gpt-4o-mini`, or any OpenAI-compatible endpoint. Verified by exercising the adapter directly (§9c.5). |
| Text to speech | Working — self-hosted Kokoro, or OpenAI |
| Per-turn transcript persistence in `call_transcript_segments` | **Not yet — Phase 2.** The table stays empty. That is not an STT failure. |
| Per-turn latency metrics (STT, LLM first token, TTS first audio) | Working — populated per turn. **But single-call figures are not usable on this host**: transcription varied 1661-11832 ms across three identical calls, because self-hosted Whisper on CPU is the contended resource. Repeated measurement is Plan 2b.8. |
| Recording to object storage | Not yet — Phase 2 |
| Barge-in and interruption handling | Partially, via the pipeline's VAD. Tuned and verified in Phase 2. |
| The agent answering a real PBX call and replying | **Working** — confirmed on a live FusionPBX call to DID 1801, greeting then a full turn. Time to first audio 5247 ms, which is still too slow. |
| Warm transfer to a human agent | Not yet — Phase 6 |
| Tools, function calling, RAG | Not yet — Phase 6 |
| Configuration through the UI instead of the CLI | Yes — both consoles cover every section (§9d.1) |
| Selecting STT, LLM, TTS and voice per agent in the UI | Working — with a fallback and a local tier (§9c.3, §9c.7) |
| Entering a provider API key and testing it in the UI | Working — §9c.2. The last CLI-only tenant step is gone. |
| Provider fallback when one errors | Working — via LiveKit's `FallbackAdapter` |
| Provider timeout, retry, backoff, circuit breaker | **Not yet — Plan 4b.10.** A *slow* provider does not trigger fallover. |
| Testing the agent from a browser instead of a phone | **Working** — Phone Numbers → **Call Test** (§9d.7). Development only, and no substitute for a real call: a browser sends wideband audio and a phone does not. |

Two honest caveats about interpreting a test call:

- **A successful greeting does not prove the model works.** The greeting is
  spoken by `session.say()`, not by the LLM, so a broken model still yields a
  call that connects and greets correctly. Verify the model with §9c.5.
- **No per-turn logging yet.** Absence of errors is not evidence a turn
  completed. Phase 2 adds the transcript and latency instrumentation that makes
  a conversation observable rather than inferred.

---

---

## 9c. Configuring AI Providers

Providers are **configuration, not code** (spec 9, 25). Switching a tenant
between a self-hosted model and a hosted one is a row update; no adapter
change, no deployment.

### 9c.1 One adapter, many endpoints

A single `openai_compatible` adapter serves every server that speaks the
OpenAI API shape:

| Endpoint | Works via |
|---|---|
| OpenAI | `https://api.openai.com/v1` |
| Self-hosted speech (speaches, faster-whisper, Kokoro) | your own base URL |
| ollama, vLLM, LM Studio, llama.cpp | your own base URL |

The base URL is per credential, so two tenants can point the same adapter at
different endpoints. That is what satisfies spec 25's local/self-hosted
requirement without a second code path.

**A provider row is an endpoint; the adapter is the code that talks to it.**
These are separate columns:

| Column | Means | Example |
|---|---|---|
| `providers.slug` | The row's name, unique within its kind | `openai_hosted` |
| `providers.adapter` | The worker's registry key | `openai_compatible` |

They used to be one column, and because it is unique per kind the catalog could
hold only **one row per protocol per kind**. "OpenAI-compatible hosted" and
"OpenAI-compatible self-hosted" could not both exist, which meant the local
fallback tier (§9c.7) had nothing distinct to point at. `adapter` is nullable
and falls back to the slug, so a row registered before the split resolves
exactly as it always did.

The development seed now registers both for speech:

| Kind | Slug | Endpoint | Key |
|---|---|---|---|
| STT | `openai_compatible` | speaches, self-hosted | none needed |
| STT | `openai_hosted` | `https://api.openai.com/v1` | required |
| TTS | `openai_compatible` | Kokoro, self-hosted | none needed |
| TTS | `openai_hosted` | `https://api.openai.com/v1` | required |

### 9c.2 Storing a credential

Keys are stored **encrypted** (Fernet, with key versioning) and never returned
by the API or written to a log.

**From the console**, which is now the normal path: open an agent, and beside
whichever provider needs one, use **Set key**. Then **Test connection** —
it makes one real request (`GET {base_url}/models`) with the stored key and
reports the status, the latency and the URL it checked. A key is write-only by
construction: `api_key` appears on the request model and on no response model
anywhere in the API, which a test asserts across every route rather than only
the credential ones.

One key per provider, shared by every tier and every agent that names it.
Replacing it clears the verified mark until it is tested again — a rotated key
nobody has tried should not show a green tick.

What *Test connection* proves: the endpoint is reachable from this deployment,
DNS and TLS work, and the key is accepted. What it does not prove: that a
particular model is available to that key. That is a different request with a
different failure mode, and reporting them as one result would put a tick beside
a model the key cannot reach.

**From the CLI**, still supported and still the right tool for scripted setup.
It reads the key from **stdin**, so it never appears in a process listing or a
shell history file:

```bash
printf %s "$OPENAI_API_KEY" | docker compose -f deploy/docker-compose.yml --env-file .env exec -T configuration-api python -m app.cli set-credential --kind LLM --provider openai_compatible --base-url https://api.openai.com/v1
```

Repeat per kind (`STT`, `LLM`, `TTS`) — they are separate credentials, so a
tenant can use a hosted LLM with self-hosted speech.

Confirm what is stored without decrypting anything:

```bash
docker compose -f deploy/docker-compose.yml --env-file .env exec postgresql psql -U voice_agent -d voice_agent -c "SELECT p.kind, p.slug, c.label, c.key_hint, c.base_url FROM provider_credentials c JOIN providers p ON p.id = c.provider_id;"
```

Only a four-character `key_hint` is kept for display. Re-running
`set-credential` rotates the key in place and records `rotated_at`.

### 9c.3 Pointing an agent at a provider

**In the console:** Agents → Configure. Each of the three stages — speech to
text, language model, text to speech — offers a provider and a model, and TTS
also offers a voice. Choosing a provider filters the model list to that
provider and pre-selects the catalog's default, so the common case is one
click. Only `ACTIVE` catalog rows are offered: a retired provider would be
refused by pre-publish validation, so offering it would produce a choice that
cannot be published.

Each stage has up to three tiers, tried in order (§9c.7):

| Tier | When it is used |
|---|---|
| Primary | Every call. |
| Fallback | The primary errored or timed out. |
| Local | Nothing hosted answered. STT and TTS only. |

Saving creates or updates a **draft**; the live version is untouched until you
publish, so a call in progress keeps the configuration it started with (spec
19, 45).

**From the CLI**, unchanged:

```bash
docker compose -f deploy/docker-compose.yml --env-file .env exec configuration-api python -m app.cli set-agent-provider --kind LLM --provider openai_compatible --model gpt-4o-mini
```

```bash
docker compose -f deploy/docker-compose.yml --env-file .env exec configuration-api python -m app.cli set-agent-provider --kind STT --provider openai_compatible --model gpt-4o-mini-transcribe
```

A model not yet in the catalog is registered automatically — the catalog is
data, and providers ship new models constantly.

**Calls already in progress keep the configuration they started with**; only
new calls pick up the change (spec 19, 45).

### 9c.4 A working combination

What this environment runs, and why:

| | Provider | Model | Reasoning |
|---|---|---|---|
| STT | OpenAI | `gpt-4o-mini-transcribe` | Transcription quality dominates whether a conversation works at all. A tiny local Whisper mis-hears enough to make a correct agent look broken. |
| LLM | OpenAI | `gpt-4o-mini` | Fast and cheap; latency matters more than raw capability for short spoken turns. |
| TTS | Self-hosted | Kokoro via speaches | Audio is the highest-volume cost per minute, and local quality is good enough. Switch to `gpt-4o-mini-tts` if you prefer. |

**Since Plan 2b.9 the seeded agent uses self-hosted STT as well.** A credential
stored against the self-hosted provider with `base_url: https://api.openai.com/v1`
was overriding its endpoint, so "self-hosted STT" was reaching OpenAI over the
internet — worth checking in any environment seeded before that fix. Removing
that credential is what makes the provider use its own endpoint.

Mixing hosted and self-hosted like this is the point of the abstraction: the
worker cannot tell the difference.

### 9c.5 Verifying a provider actually works

Logs prove a call connected; they do not prove the model replied. Exercise the
adapter directly:

```bash
docker compose -f deploy/docker-compose.yml --env-file .env exec ai-agent-worker python -c "
import asyncio
from worker.db import get_session_factory
from worker.config_loader import CallConfigLoader, SipCallInfo
from worker.providers.registry import build_llm
from worker.providers.base import Message

async def main():
    async with get_session_factory()() as s:
        ctx = await CallConfigLoader().load(s, call_id='probe', room_name='probe',
            sip=SipCallInfo(None, '1801', '3001', None), worker_id='probe')
    print(ctx.llm.redacted())
    out = [c.text async for c in build_llm(ctx.llm).generate(
        [Message(role='user', content='Reply with one short sentence.')]) if c.text]
    print('reply:', ''.join(out))

asyncio.run(main())"
```

A real sentence back means the credential, adapter, base URL and model all
work. `ctx.llm.redacted()` shows `has_credential: true` and never the key
itself.

Note this writes a call row, since the loader records one — delete it
afterwards if it would confuse your analytics.

### 9c.6 The development stand-in

The seed configures a keyless `echo_dev` LLM so a first call works before any
credential exists. It repeats what it heard and is **refused outside
development**.

It is genuinely useful beyond bootstrap: Phase 8 load testing at 1,000
concurrent calls needs the media path exercised without a million tokens of
provider cost.

A caveat worth knowing, because it cost real time here: the greeting is spoken
by `session.say()`, not by the model. A broken LLM therefore produces a call
that connects, greets correctly, and only fails once somebody speaks — so
"the greeting worked" is not evidence the model works. Use §9c.5.

### 9c.7 Fallback and local tiers

§55 requires a fallback provider for every AI provider. It is configured per
agent as an ordered chain, and the worker resolves it into LiveKit's own
`FallbackAdapter` for each stage — LiveKit's adapters already know which errors
are worth failing over for and recover to the primary when it returns, which is
better than a retry wrapper written here.

The tiers are **named rather than numbered** because they insure against
different things: a fallback covers a vendor having a bad day, a local endpoint
covers the internet being unavailable. An anonymous ordered list would hide
that from whoever operates it.

**LLM has no local tier.** A self-hosted language model is a deployment
decision with its own hardware, not a switch a tenant can flip, and offering
the control without that would be a promise the platform cannot keep.

A tier only helps if it can fail independently:

- **Each tier uses its own provider's key.** A chain whose fallback inherited
  the primary's credential would pass every test against one vendor and 401 the
  first time it was needed.
- **A local tier must name a self-hosted provider.** The builder tags each
  option `· self-hosted` and warns if a public API is selected as Local, because
  a hosted fallback behind a hosted primary fails for the same reasons.
- **A credential's `base_url` overrides the provider default.** That is how a
  tenant points at its own endpoint — and it is also how a provider named
  "self-hosted" ends up calling OpenAI, if a key was stored against it with a
  vendor URL. Check this before concluding a local tier is unused.

**What is not implemented, stated plainly.** §55 lists timeout, retry,
exponential backoff, circuit breaker and fallback. Only the last is configured.
A provider that *errors* fails over; one that has become *slow* does not,
because there is no timeout policy to trip, and there is no circuit breaker, so
a dying provider is retried on every call. That is Plan item 4b.10.

---

---

## 9d. Loading and Testing the Interfaces

### 9d.1 What exists today

Both consoles are real. Sign in at http://localhost:3200 with one of the
development accounts below; a platform account lands on the platform console
and a tenant account on the tenant one, because the two have different
navigation and a platform account has no tenant to act in.

| Interface | URL | State |
|---|---|---|
| **Tenant console** | http://localhost:3200 | **Working** — every section in spec 61 |
| **Platform console** | http://localhost:3200/platform | **Working** — every section in spec 60 |
| Swagger UI | http://localhost:8200/docs | Working — still the fastest way to reach an endpoint with a specific payload |
| ReDoc | http://localhost:8200/redoc | Working, read-only reference |
| Grafana | http://localhost:3201 | Working; no voice dashboard yet (Phase 2b.6) |
| Prometheus | http://localhost:9290 | Working, metrics scraped |
| MinIO console | http://localhost:9201 | Working, empty until recording lands (Phase 2b.3) |
| LiveKit admin UI | — | **None exists** for self-hosted LiveKit. The platform console is the configuration surface, by design — §9d.6 |
| **Call Test** (browser call) | in the console, per number | **Working** — §9d.7. Replaced LiveKit's Agents Playground, which cannot declare which DID it is calling |

#### Tenant console sections

| Section | Path | What it does |
|---|---|---|
| Dashboard | `/` | Counts, connection-test state, API reachability |
| PBXs | `/pbxs` | Register, edit, enable/disable, connection-test |
| SIP Trunks | `/sip-trunks` | Trunks with their LiveKit sync state and a re-sync |
| Phone Numbers | `/phone-numbers` | DIDs, their trunk and the agent that answers, plus **Call Test** — a browser call to that number (§9d.7) |
| Agents | `/agents` | The builder: providers and models per tier, keys with a connection test, versions, validation, publish, rollback |
| Routing | `/routing` | Priority-ordered rules with their fallback chain (spec 20, 38) |
| Business Hours | `/business-hours` | Schedules with intervals and dated exceptions (spec 37) |
| Transfer Targets | `/transfer-destinations` | Where a warm transfer goes, and whether it whispers the summary (CR-1) |
| Tools | `/tools` | HTTP tools with schema validation and a write-only secret (spec 31–33) |
| Knowledge Bases | `/knowledge-bases` | Create and assign a base; **ingestion is Phase 6** |
| Calls / Transcripts | `/calls` | History, per-call detail, transcript and events |
| Recordings | `/recordings` | Recording metadata; **empty until egress lands (Plan 2b.3)** |
| Analytics | `/analytics` | Volume and outcomes over a window (spec 57) |
| Users | `/users` | Tenant users and roles (spec 8) |
| Usage | `/usage` | Standing against each limit, and which are enforced (spec 47) |
| Settings | `/settings` | Name, timezone, default language |

#### Platform console sections

| Section | Path | What it does |
|---|---|---|
| Dashboard | `/platform` | Tenant count, live calls, LiveKit state |
| Tenants | `/platform/tenants` | Create a tenant with its first administrator, set limits, suspend |
| LiveKit | `/platform/livekit` | Reachability, room count, and every row whose mirror disagrees (spec 46) |
| Infrastructure | `/platform/infrastructure` | Live dependency probes and links into Grafana, Prometheus and MinIO |
| Capacity | `/platform/capacity` | Inventory, live load, licensed concurrency (spec 48) |
| Providers | `/platform/providers` | The STT/LLM/TTS catalog, including `requires_credential` |
| Models | `/platform/models` | Model slugs per provider, with one default each |
| Voices | `/platform/voices` | Voice identifiers for TTS providers (spec 30) |
| System Users | `/platform/system-users` | Platform staff and their two roles |
| Audit Logs | `/platform/audit-logs` | The append-only trail, filterable (spec 69) |
| Settings | `/platform/settings` | Effective configuration, secrets shown only as set/not-set |

#### Two sections that exist but have no data yet

Recordings and Knowledge Bases are **built and live**, not disabled. Their
sidebar entries carry a small tag and each screen states the position:

* **Recordings** reads `GET /api/v1/recordings`, which returns an empty page
  because the worker does not start a LiveKit egress job (Plan 2b.3). An agent
  version can already have recording switched on; that records the intent and
  produces no audio.
* **Knowledge Bases** can be created, configured and assigned to an agent. The
  document list is live; nothing populates it until Phase 6 ingestion lands, so
  an agent pointed at a base retrieves nothing rather than failing.

Disabling either menu entry would have hidden the configuration that *is*
usable behind the feature that is not.

#### Role adaptation

The navigation adapts to the signed-in role. As `viewer@dev.example.com` the
write buttons disappear, row actions reduce to read-only ones, and sections the
role cannot read drop out entirely. That is presentation only — the API
enforces every permission independently, so a hidden control is not a missing
check. A test asserts that each section's API route requires the same
permission its menu entry claims, because the two disagreeing would either
hide a usable section or show one every request refuses.

Ports come from `.env`; the defaults avoid the common collisions. Run
`python3 scripts/preflight.py` if anything refuses to bind.

### 9d.1a Checking the console yourself

```bash
make typecheck-web
```

```bash
make build-web
```

`build-web` overrides two things and both are worth knowing, because each
produces a failure that names the wrong cause:

* **`NODE_ENV`.** The compose file sets it to `development` for the dev server.
  `next build` under that value warns about a non-standard `NODE_ENV` and then
  fails prerendering `/404` with `<Html> should not be imported outside of
  pages/_document`. Nothing in this project imports `next/document`.
* **`NEXT_DIST_DIR`.** `.next` is on the bind mount the dev server writes to
  continuously, so a build sharing it reads half-written chunks and fails the
  same way. The build writes to `.next-build` instead, so it can run without
  stopping the dev server.

There is no ESLint configuration, so `npm run lint` prompts interactively
rather than running. `tsc --noEmit` is the gate.

### 9d.2 Driving the API from Swagger UI

Open http://localhost:8200/docs. Every endpoint is there with its schema,
permissions and error cases.

To authenticate, get a token:

```bash
./scripts/api-token.sh
```

Then click **Authorize** in Swagger, paste the token, and every endpoint
becomes callable as that user.

The script takes an email, which is how the authorization model is best
explored:

```bash
./scripts/api-token.sh viewer@dev.example.com
```

```bash
./scripts/api-token.sh admin@acme.example.com
```

The password comes from `API_PASSWORD` when set, so it need not be typed or
left in shell history.

### 9d.3 Seeing the guarantees for yourself

These are worth running once, because they are the properties the whole
tenancy model rests on and they are more convincing performed than described.

**RBAC is enforced by the backend, not the UI.** As `viewer@dev.example.com`,
`POST /api/v1/pbxs` returns **403** naming the missing permission:

```bash
curl -s -X POST http://localhost:8200/api/v1/pbxs -H "Authorization: Bearer $(./scripts/api-token.sh viewer@dev.example.com)" -H 'Content-Type: application/json' -d '{"name":"Test","pbx_type":"ASTERISK","host":"10.0.0.1"}'
```

**Tenant isolation returns 404, not 403.** Fetch a `dev` PBX as
`admin@acme.example.com` and the row reads as absent — a 403 would confirm it
exists, and cross-tenant existence is itself information spec 7 protects.

**A client-supplied tenant ID is refused, not ignored:**

```bash
curl -s "http://localhost:8200/api/v1/auth/me?tenant_id=00000000-0000-0000-0000-000000000000" -H "Authorization: Bearer $(./scripts/api-token.sh)"
```

That returns **400**. The same applies to an `X-Tenant-Id` header and to a
`tenant_id` field in a JSON body.

**The audit trail records every change:**

```bash
docker compose -f deploy/docker-compose.yml --env-file .env exec postgresql psql -U voice_agent -d voice_agent -c "SELECT occurred_at, action, user_email, resource_type, ip_address FROM audit_logs ORDER BY occurred_at DESC LIMIT 10;"
```

### 9d.4 Development accounts

Created by `python -m app.cli create-user`. All use `DevPassword123!` in this
environment and exist only to exercise the authorization model.

| Email | Role | Tenant |
|---|---|---|
| `super@platform.example.com` | `SUPER_ADMIN` | none — platform staff |
| `admin@dev.example.com` | `TENANT_ADMIN` | dev |
| `viewer@dev.example.com` | `VIEWER` | dev |
| `admin@acme.example.com` | `TENANT_ADMIN` | acme |

Two tenants exist deliberately: isolation cannot be tested with one.

Note that **reserved TLDs are refused** — `.test`, `.invalid` and `.localhost`
are rejected by email validation, so an account using one could be created by
an earlier version of the CLI and then never sign in. The CLI now validates
with the same rules as the API.

### 9d.5 Working on the frontend

Layout:

| Path | Role |
|---|---|
| `app/login/page.tsx` | Sign-in screen |
| `app/page.tsx` | Tenant dashboard |
| `app/pbxs/page.tsx` | PBX management — the reference screen |
| `app/platform/page.tsx` | Platform console shell |
| `components/Shell.tsx` | Sidebar, navigation, identity |
| `components/ui.tsx` | Button, Field, Badge, Dialog, toasts, empty and loading states |
| `lib/auth.tsx` | Session state; identity always from `GET /auth/me`, never by decoding the token client-side |
| `lib/api.ts` | Typed client with silent token refresh |
| `app/globals.css` | Design tokens; dark mode comes from the token indirection |

Tokens are kept in `localStorage`, which is a deliberate development-stage
choice with a real tradeoff: anything achieving script execution on this origin
can read them. The production answer is httpOnly, SameSite cookies with a CSRF
token, which needs backend cookie support — recorded as Phase 7 work rather
than left implicit.

The container runs `next dev` with the source bind-mounted, so an edit to
`services/frontend/app` reloads in the browser without a rebuild:

```bash
docker compose -f deploy/docker-compose.yml --env-file .env logs -f frontend
```

`services/frontend/lib/api.ts` is the API client. It deliberately has **no way
to pass a tenant ID** — tenant identity comes from the session, and leaving it
off the client means no call site can supply one by accident.

### 9d.6 LiveKit's own interfaces

A question that comes up early: is there a LiveKit UI for configuring LiveKit
itself? For a self-hosted deployment, no.

| Surface | Configuration UI | Applies here |
|---|---|---|
| `livekit-server` | None. Port 7880 serves the WebSocket and Twirp API, 6789 serves Prometheus text; no path serves HTML. | Running |
| `livekit-sip` | None at all. Inbound trunks and dispatch rules exist only over the Twirp API. | Running |
| LiveKit Cloud dashboard | Yes — keys, SIP trunks, dispatch rules, room monitoring, analytics | **No.** Cloud-only, and PRD Q1 chose self-hosted because §11/§13 require port-range, TURN and cluster administration. |
| Cloud **Agent Console** | Yes — realtime agent debugging | **No.** It debugs agents running anywhere, self-hosted included, but it launches from a LiveKit Cloud project dashboard. With no Cloud project there is no button to press. |
| `lk` CLI (livekit-cli) | The real self-hosted admin surface: `lk room list`, `lk sip inbound create`, `lk dispatch` | Not installed, deliberately — see below |

**The absence is by design, not a missing integration.** §12 makes PostgreSQL
the source of truth and LiveKit the mirror: every write goes to PostgreSQL
first and LiveKit second. A second UI writing straight to LiveKit would be a
second source of truth, and anything entered there is overwritten by the next
re-sync — which is precisely the drift §46 asks the platform to detect. So
`/platform/livekit` reports the mirror's *agreement* with the database — sync
state counts and drifted rows — rather than an inventory of LiveKit objects.
The useful question about LiveKit here is "does it still match the records",
not "what does it contain".

The one genuinely UI-less piece is infrastructure configuration:
`deploy/livekit/livekit.yaml` holds the RTC ports, TURN and the Redis address.
It holds **no API key**: the pair arrives as `LIVEKIT_KEYS` from the
environment, because the file used to carry `devkey`/`devsecret-…` — the values
from LiveKit's own examples — committed to this repository. That is platform/DevOps controlled under §13 and must never reach a
tenant administrator — the file's own header says so. Its runtime behaviour is
observed through Grafana (§12), not edited through a console.

### 9d.7 Calling an agent from the browser

**Phone Numbers → Call Test** — the green handset in the Actions column.

It appears on any active number that has an agent behind it, and opens a panel
that joins the call, publishes your microphone and shows the conversation as it
happens. No PBX in the path.

The button is deliberately absent on a disabled number or one with no agent:
either would produce a call that connects to silence, which is the exact
failure this button exists to diagnose. The handset is drawn from the `--ok`
token rather than a literal green, so it stays legible in dark mode.

Development only, and off unless configured on:

```
ALLOW_BROWSER_TEST_SESSIONS=true      # the console endpoint
ALLOW_BROWSER_TEST_PARTICIPANT=true   # the worker accepting a browser caller
```

Both are already set in the development `.env`, and both are refused outside
`environment=development` whatever they say. Nothing else is needed — no
separate client, no room to create by hand.

`make test-room DID=1801` still exists for driving a client of your own: it
creates the room with the DID in its metadata *and* dispatches the agent, which
the console endpoint also does. Both are needed — the worker registers under an
explicit agent name, so LiveKit will not dispatch it into a room on its own,
and a room without the dispatch gets a participant, no agent, and silence.

LiveKit's own Agents Playground was tried and dropped: it mints its own token
and cannot declare which DID it is calling, so it only reaches an agent through
a pre-created room, and the button does that better. It is not in the compose
file.

#### Why it needs two gates and a credential

This is **the only endpoint in the platform that issues a credential**.
Everything else reads or writes configuration; `POST /browser-test/session`
mints a LiveKit join token, which grants media access to a room. It carries
four independent guards, all in one file so none can be dropped without reading
why:

| Guard | Stops |
|---|---|
| `allow_browser_test_sessions` | an unconfigured deployment |
| `environment == "development"` | a production deployment, whatever the flag says |
| `agents.write` | a read-only account minting tokens |
| the DID resolved through `TenantRepository` | reaching another tenant's agent |

The token grants `room_join` on one named room — no create, no admin, no list —
and lives ten minutes. A test asserts that by parsing the call rather than
searching the source text, because the first version of it passed on a
*comment* listing the grants being withheld.

The worker's side is the same idea: it accepts a non-SIP participant only when
that participant declares a DID, in a participant attribute or the room's
metadata. It relaxes *where the DID comes from*, not whether there is one, so
the spec 6 guarantee that every call has a tenant still holds.

#### Two things that make it work, both easy to get wrong

**LiveKit must advertise an address the browser can reach.** Without
`LIVEKIT_NODE_IP` it offers only its container address (`172.x`), which another
container can route to and a browser on the host cannot. Signalling connects,
media never does, and the error is `could not establish pc connection` — which
says nothing about addresses. Set it to this machine's LAN address:

```bash
./scripts/lan-ip.sh     # then set LIVEKIT_NODE_IP in .env
```

Loopback does not work: `127.0.0.1` means "myself" inside every other
container, which breaks the worker and SIP paths.

**Media is on one muxed UDP port, not a range.** `rtc.udp_port: 50100`. A
fifty-port range makes ICE gathering take about 15 seconds through Docker
Desktop, and livekit-client abandons negotiation at 15 — publishing a
microphone failed with `NegotiationError: negotiation timed out` by a margin of
milliseconds. One port brings it to ~7 s; the console also raises
`peerConnectionTimeout` to 45 s as headroom. Muxing is what LiveKit recommends
in production anyway.

**Ports that appear in ICE candidates must not be renumbered.** LiveKit
advertises its RTC TCP port as a candidate, so `rtc.tcp_port` in
`livekit.yaml` and the published host port have to be the same number — they
are both `7981`. Mapping `7981 -> 7881` made the browser dial a closed port
whenever it fell back from UDP, failing with `NegotiationError: negotiation
timed out`. The UDP media range is 1:1 for the same reason.

**The browser reaches LiveKit on the published port, not the internal one.**
Services use `ws://livekit:7880`; a browser needs `ws://localhost:7980`. The
API returns the right one to the console, which is why `LIVEKIT_PUBLIC_URL`
exists as a separate setting.

#### One console message that is not a fault, and is filtered

```
publisher data channel 'DATA_TRACK_LOSSY' closed unexpectedly
```

livekit-client logs this at **error** level about a second into a call that
then works, and Next.js's development overlay promotes any console error to a
full-screen banner — so a healthy call presented as a failure. Measured
alongside the message: track published in 63 ms, agent present, audio in both
directions, no reconnects.

The SDK creates the publisher's data channels before the publisher connection
exists and replaces them once it does, reporting the first closing as an error.

`BrowserCall.tsx` filters **that one message, by substring, at error level,
for as long as the panel is open**. Everything else reaches the console
untouched. Two tidier routes were tried and measured first: `setLogExtension`
is additive and leaves the console output in place, and
`setLogLevel(silent, LoggerNames.DataTracks)` did not cover it — the message
comes from another logger, and guessing which is fragile where an exact string
is not.

The narrowness is the point. A silenced error channel is how this project lost
an afternoon to `unsupported datachannel added` sitting unread in a log.

#### What it does not replace

Telephony audio is 8 kHz and a browser is not, so speech recognition that
sounds fine here can still be wrong on a real call — the panel says so on
screen. It also cannot catch SIP-layer faults. It replaces the *iteration*, not
the acceptance test.

Microphone capture needs a secure context, so this works on
`http://localhost:3200` but not over the LAN without HTTPS.

---

## 10. API Surface

Base path `/api/v1`. Full OpenAPI is generated and served at `/docs` (and `/openapi.json`).

```
POST /tenants                     GET  /tenants
POST /pbxs                        GET  /pbxs              PUT /pbxs/{id}
POST /sip-trunks                  GET  /sip-trunks        PUT /sip-trunks/{id}
POST /phone-numbers               GET  /phone-numbers
POST /agents                      GET  /agents            PUT /agents/{id}
POST /agents/{id}/publish
POST /agents/{id}/rollback
POST /providers                   GET  /providers
POST /voices                      GET  /voices
POST /tools                       GET  /tools
POST /knowledge-bases             GET  /knowledge-bases
POST /routing-rules               GET  /routing-rules
GET  /calls                       GET  /calls/{id}
GET  /analytics                   GET  /usage
```

### Request conventions

- **Never** send `tenant_id` from the browser. Tenant identity is derived from the
  authenticated token, server-side.
- Authenticate with `Authorization: Bearer <jwt>`.
- Long-running operations (LiveKit sync, knowledge-base ingestion, load-test runs) are
  asynchronous and return a job handle.

```bash
curl -fsS http://localhost:8200/api/v1/agents \
  -H "Authorization: Bearer $TOKEN"
```

---

## 11. Roles and Permissions

**Platform:** `SUPER_ADMIN`, `PLATFORM_OPERATOR`

**Tenant:** `TENANT_ADMIN`, `MANAGER`, `AGENT_MANAGER`, `ANALYST`, `VIEWER`

**Granular permissions:** `agents.read`, `agents.write`, `agents.publish`, `pbxs.read`,
`pbxs.write`, `sip_trunks.read`, `sip_trunks.write`, `calls.read`, `recordings.read`,
`analytics.read`, `users.manage`, `billing.read`

The backend enforces permissions independently of the frontend. Hiding a button in the UI is
never the control.

---

## 12. Observability

### Structured logging

JSON only. Every call-related event carries `tenant_id`, `call_id`, `room_id`, `agent_id`,
`agent_version_id`, `service`, `timestamp`, `event`:

```json
{
  "service": "voice-agent",
  "tenant_id": "tenant_001",
  "call_id": "call_123",
  "agent_id": "sales",
  "event": "tts_started"
}
```

`call_id` is generated once per call and propagated through PBX, SIP, LiveKit, agent, STT, LLM,
TTS, database, logs, recording, and transcript.

### Metrics

| Domain | Tracked |
|---|---|
| Infrastructure | CPU, memory, network, disk, containers, pods, restarts |
| LiveKit | rooms, participants, active sessions, packet loss, jitter, latency, media connections |
| AI | active calls, agent jobs, STT/LLM/TTS latency, errors, worker utilization |
| Voice latency | STT latency, LLM first-token, TTS first-audio, time to first response, time to first audio, end-to-end response latency |
| Business | calls, answered, failed, average duration, transfers, AI minutes |

Grafana dashboards ship in `deploy/grafana/`.

### Call state machine

```
NEW → RINGING → ANSWERED → AI_CONNECTED → IN_PROGRESS → COMPLETED
                                    ├──► TRANSFERRING → HUMAN_AGENT → COMPLETED
                                    └──► FAILED | TIMEOUT | CANCELLED | BUSY | NO_ANSWER
```

`TRANSFERRING` covers the whole warm-transfer sequence; the step-by-step detail lives in
`transfer_status` on the call record rather than in extra top-level states.

### Audit log

Every configuration change records user, tenant, timestamp, action, resource, resource ID,
old value, new value, and IP — e.g. `agent.published`, `trunk.updated`, `credential.rotated`.

---

## 13. Testing

```bash
make test
```

| Suite | Command | Covers |
|---|---|---|
| Unit | `make test-unit` | tenant isolation, authorization, routing, configuration, agent logic, provider adapters, tools |
| Integration | `make test-integration` | PBX → SIP → LiveKit → AI Agent → STT → LLM → TTS |
| End-to-end | `make test-e2e` | real SIP/phone calls through the complete system |
| Frontend | `make test-frontend` | component and flow tests |

Tenant isolation has **explicit** tests: for every tenant-scoped endpoint, a Tenant A token must
fail to read or mutate a Tenant B resource.

---

## 14. Load Testing

The 1,000-concurrent-call target is a claim only once measured. Run the progression:

```bash
make load-test CONCURRENCY=10
```

Steps: **10 → 25 → 50 → 100 → 250 → 500 → 750 → 1000**.

Then sustain at high concurrency for **15, 30, and 60 minutes** — short bursts do not validate
scalability.

Measure at every step: CPU, RAM, network, SIP capacity, RTP performance, packet loss, jitter,
STT latency, LLM latency, TTS latency, first-audio latency, call failure rate, worker
utilization, LiveKit utilization.

Load tests must exercise the **production architecture and real AI providers**, including SIP,
LiveKit, AI workers, STT, LLM, TTS, recording, tools, and RAG wherever those are enabled in
production configuration.

The resulting **capacity report** derives worker counts from measurements. The arithmetic
pattern — `1000 / calls_per_worker = workers`, plus headroom — is only meaningful once
`calls_per_worker` is a benchmarked number, not a guess.

---

## 15. Production Deployment

Kubernetes + Helm. Each of these scales **independently**:

```
Frontend · API · LiveKit · LiveKit SIP · AI Agent Workers
```

```
            Load Balancer
                 |
        +--------+--------+
        |                 |
     Frontend            API
                          |
              +-----------+-----------+
              |                       |
          PostgreSQL                Redis
              |
   +----------+----------+
   |                     |
LiveKit Cluster    AI Worker Cluster
   |
LiveKit SIP Cluster
```

Install:

```bash
helm upgrade --install voice-agent deploy/helm/voice-agent -f deploy/helm/values.production.yaml
```

### Autoscaling

Kubernetes HPA (or equivalent) driven by **active AI jobs, worker utilization, CPU, memory**.
Voice calls are long-running **jobs/sessions**, not short HTTP requests — configure
`terminationGracePeriodSeconds` to exceed your maximum call duration so drains complete.

### Graceful shutdown

On `SIGTERM` a worker stops accepting new calls, finishes existing calls, disconnects cleanly,
and exits. Deployments must not drop active calls.

### High availability

Multiple LiveKit nodes · multiple SIP nodes · multiple AI workers · multiple API replicas ·
PostgreSQL HA · Redis HA · redundant load balancers. No single points of failure.

### Adding capacity

Adding LiveKit, SIP, API, or AI worker capacity is a **scaling operation, not a code change**.

```bash
kubectl scale deployment/ai-agent-worker --replicas=40
```

---

## 16. Operations Runbook

### Configuration drift

The platform compares PostgreSQL against LiveKit and reports
**"Configuration Drift Detected"** — e.g. database says `SIP trunk = Active`, LiveKit reports
`SIP trunk = Missing`. Resolve from the LiveKit admin section with **Synchronize**, **Repair**,
or **Retry**.

### Tenant call limits

Each tenant has Maximum Concurrent Calls, Maximum Daily Calls, and Maximum Monthly Minutes.
Limits are checked **before a call is accepted**, not after.

### Platform capacity

`SUPER_ADMIN` sees Total Active Calls, Total Capacity, Available Capacity, LiveKit Nodes,
SIP Nodes, AI Workers, Worker Utilization, CPU, Memory, Network, Provider Health.

### Provider failure

Every provider adapter has timeout, retry, exponential backoff, circuit breaker, and a fallback
provider. One provider degrading must never crash the worker pool. Provider Health on the
capacity dashboard shows open circuits.

### Backup and disaster recovery

Back up PostgreSQL, configuration, recording metadata, and object storage. Redis keys are
classified as disposable or persistent and treated accordingly. DR procedures are documented
and rehearsed — not assumed.

### Failure drills

Rehearse and document recovery for: AI worker failure, LiveKit node failure, SIP node failure,
API restart, Redis failure, database failure, STT failure, LLM timeout, TTS failure, network
packet loss, high CPU, worker exhaustion.

---

## 17. Security Notes

Implemented controls: HTTPS/TLS · JWT authentication · RBAC · tenant isolation · SIP
authentication · IP allowlists · encrypted credentials · secret management · audit logging ·
secure object storage.

### Secrets

Production credentials come from **Kubernetes Secrets**, or preferably **HashiCorp Vault,
AWS Secrets Manager, Google Secret Manager, or Azure Key Vault**.

**Never** place production credentials in:

- source code
- Git
- Docker images
- logs
- frontend code
- plaintext configuration
- agent prompts

### Tenant isolation

Enforced server-side. A tenant ID from the browser is never trusted; tenant identity always
comes from the authenticated session/token.

### Import / export

Tenant configuration export/import covers agents, agent versions, tools, routing, knowledge-base
configuration, and business rules. **Secrets are never exported in plaintext.**

---

## 18. Troubleshooting

| Symptom | Where to look |
|---|---|
| Call rings but nobody answers | Dispatch rule exists and is `SYNCED`? Worker registered under `WORKER_AGENT_NAME`? Worker logs for the `call_id`. |
| One-way or no audio | RTP port range reachable from the PBX; NAT/`external_ip` in `livekit.yaml`; codec agreement on the trunk. |
| Agent answers with the wrong behavior | Which `agent_version_id` is on the call record? Routing rule precedence and DID mapping. |
| Long silence before the first word | Voice-latency metrics: STT latency, LLM first-token, TTS first-audio. Streaming enabled on all three? |
| Agent talks over the caller | Barge-in path: VAD → turn detection → TTS cancellation; check interruption and silence-timeout settings. |
| `SIP trunk = Missing` in LiveKit | Configuration drift — run Synchronize/Repair. |
| Calls rejected at peak | Tenant call limits, or platform capacity exhausted — check Available Capacity and worker utilization. |
| Provider errors in bursts | Circuit breaker state and fallback provider configuration. |
| Caller heard the summary whisper | Audio isolation bug — the whisper was published to the wrong leg. Treat as a release blocker; check which participant the whisper track was published to. |
| Caller heard silence during transfer | Hold media not configured, or the announcement finished without looping. Check the agent's announcement and hold-media settings. |
| Transfer never reaches the human agent | `transfer_status` says which step stalled; then check the PBX extension/queue and what SIP response the PBX returned. |
| A service never becomes ready | `/ready` reports which dependency failed; check PostgreSQL, Redis, LiveKit, object storage. |

Every investigation starts from the `call_id`. It is present in every call-related log line
across every service.

### Problems already solved

The table above covers symptoms you might hit in normal operation.
[`LiveKitVoiceAgentPlan.md` §12](./LiveKitVoiceAgentPlan.md#12-environment-notes-and-troubleshooting)
is the full log of problems hit while building the platform — symptom, cause, fix — organised by
phase, plus a short runbook for bringing up a new environment in the order that avoids most of
them.

Two worth knowing before you debug anything SIP-related:

- **`486 Busy` with `reason: flood` does not mean rate limiting.** It is how livekit-sip
  reports a call that matched no dispatch rule.
- **A dispatch rule's `inbound_numbers` matches the *caller's* number**, not the number that was
  dialled. Restrict which DIDs a trunk accepts with the trunk's `numbers` field instead.

Set `SIP_LOG_LEVEL=debug` before investigating a rejected call. The info-level SIP logs do not
say why.

Three more from getting browser calls working, because each one presented as something else:

- **A call that connects and then reconnects every ten seconds** was
  `livekit-server:v1.8` rejecting a datachannel the current browser SDK opens.
  `unsupported datachannel added` then `error reading data channel` sit in the *server* log at
  info level; the browser only ever says `NegotiationError: negotiation timed out`. Pinned to
  `v1.9`.
- **`LIVEKIT_NODE_IP` going stale** when the machine changes network. LiveKit then advertises an
  address that no longer exists, media never flows, and the error mentions neither addresses nor
  networks. It now defaults to `SIP_NAT_IP`; `make refresh-ip` updates both.
- **Any port that appears in an ICE candidate must not be renumbered by the port mapping.**
  `7981:7881` made a browser dial a closed port. `livekit.yaml` and the published port have to
  agree.

A general rule earned expensively here: **a repeating interval in a log is a timeout, not a
coincidence.** Three plausible network misconfigurations were found and fixed before anyone
asked what the recurring ten seconds belonged to, and none of them was the fault.

---

## 19. Contributing Rules

Non-negotiable, from the Master Requirements Specification:

1. Do not build a monolith. Control Plane and Voice Execution Plane stay separate.
2. Design the database schema before the business logic.
3. Implement tenant isolation before tenant-facing features.
4. Enforce RBAC at the API layer.
5. Put provider-specific code behind provider adapters — never couple the agent to one vendor.
6. Keep LiveKit integration in a dedicated service/module.
7. Store LiveKit resource IDs in PostgreSQL; implement synchronization and drift detection.
8. Never hard-code tenant or agent configuration.
9. Never expose secrets to the frontend.
10. Use asynchronous processing for long-running operations.
11. Keep AI workers horizontally scalable and all services containerized.
12. Add `/health` and `/ready`, structured logging, and correlation IDs from the beginning.
13. Write automated tests for every major module.
14. Do not claim 1,000-call capacity until load testing proves it.
15. Before implementing a major component, define: **Architecture, Database Model, API Contract,
    Security Model, Error Handling, Tests, Scaling Considerations.**
