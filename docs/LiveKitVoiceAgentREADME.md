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
│   │   │   ├── api/v1/             # routers: tenants, pbxs, sip_trunks, agents, ...
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
│   │   │   └── resilience/         # timeout, retry, backoff, circuit breaker, fallback
│   │   └── tests/
│   │
│   ├── frontend/                   # Next.js — platform console + tenant console
│   │   ├── app/
│   │   ├── components/
│   │   └── lib/
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
| RTC `50100-50149/udp` | advertised in ICE candidates |

To change one of those, change it in `deploy/docker-compose.yml` **and** the matching LiveKit
config (`deploy/livekit/livekit-sip.yaml` `sip_port` / `rtp_port`, or
`deploy/livekit/livekit.yaml` `rtc.port_range_*`) together.

The development media ranges are deliberately narrow — 50 ports each. Every published port
becomes a forwarding entry on Docker Desktop, and a 10,000-port range makes `compose up` take
minutes. Roughly two ports per concurrent call, so this supports a handful of simultaneous
calls: enough for Phases 1–2, widened in the Helm values for load testing.

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
| `LIVEKIT_URL` | e.g. `ws://livekit:7880` |
| `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | admin API credentials |
| `LIVEKIT_SIP_URI` | SIP entry point advertised to PBXs |
| `LIVEKIT_RTP_PORT_RANGE` | infrastructure-controlled; not tenant-visible |

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
| **Agent number (DID)** | `1001` | The number the caller **dials**. LiveKit matches it against the SIP trunk's `numbers`, and the platform resolves it to a tenant and agent through `phone_numbers`. |
| **Caller number** | `15550001111` | The number the call appears to come **from**. Only used for caller-based routing conditions, analytics, and the transfer summary. |

Their LiveKit attribute names are easy to swap, so for reference:

```
sip.trunkPhoneNumber  →  1001           the AGENT number that was dialled
sip.phoneNumber       →  15550001111    the CALLER's number
```

Both are configurable; nothing about `1001` is special. Change the agent number
with `--did`, and the caller number per call.

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

Until the configuration UI lands in Phase 4, a CLI seeds the same rows the UI
will write. `--pbx-host` is your PBX's address; `--did` is the agent number.

```bash
docker compose -f deploy/docker-compose.yml --env-file .env exec configuration-api python -m app.cli seed-dev-tenant --did 1001 --pbx-host 192.168.0.113
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
  ST_xxxxxxxx  Development Trunk  numbers=['1001']  allowed=[]

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
fs_cli -x "originate {origination_caller_id_number=15550001111,sip_auth_username=lkdev,sip_auth_password=YOUR_PASSWORD}sofia/external/sip:1001@LIVEKIT_HOST:5060 &park"
```

`+OK <uuid>` means the agent answered. `-ERR NO_ANSWER` means it did not — see
§9a.10.

To give the agent real speech to transcribe, replace `&park` with a playback:

```bash
fs_cli -x "originate {origination_caller_id_number=15550001111,sip_auth_username=lkdev,sip_auth_password=YOUR_PASSWORD}sofia/external/sip:1001@LIVEKIT_HOST:5060 &playback(/usr/share/freeswitch/sounds/en/us/callie/ivr/8000/ivr-welcome_to_freeswitch.wav)"
```

To hear the agent yourself, register a softphone to the PBX and dial the agent
number through whatever route your dialplan already provides.

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
| Inbound call, agent answers, speaks a greeting | Working |
| Speech recognised, spoken reply | Working, using local STT and TTS |
| Conversational reasoning | Needs an LLM. The seed uses a keyless development stand-in that echoes what it heard; configure a real provider for genuine conversation. |
| Transcripts stored in `call_transcript_segments` | Not yet — Phase 2. The table stays empty; this is not an STT failure. |
| Recording to object storage | Not yet — Phase 2. |
| Barge-in, interruption handling | Partially, via the pipeline's VAD. Tuned and verified in Phase 2. |
| Warm transfer to a human agent | Not yet — Phase 6. |
| Configuration through the UI instead of the CLI | Not yet — Phase 4. |

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
