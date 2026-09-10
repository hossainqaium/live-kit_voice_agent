# Product Requirements Document
## Multi-Tenant AI Voice Agent Platform (LiveKit)

| | |
|---|---|
| **Document** | LiveKitVoiceAgentPRD.md |
| **Version** | 1.0 (Draft) |
| **Date** | 2026-09-11 |
| **Source of truth** | `LiveKit AI Voice Agent.pdf` — Master Requirements Specification (§1–§80) |
| **Status** | Awaiting review |

> Traceability: every requirement below carries a `[§n]` reference back to the numbered
> section of the Master Requirements Specification it derives from. Requirements marked
> **(assumption)** are not in the source spec and are called out in §21 of this document.
>
> Requirements tagged **`CR-n`** are **customer clarifications added after the specification
> was issued**. They refine, and never contradict, the numbered sections they extend:
>
> | Tag | Clarification | Extends |
> |---|---|---|
> | `CR-1` | Human transfer is a **warm transfer back to the PBX**: the caller hears a "transferring you to a human agent, please wait" announcement while the receiving human agent first hears the AI conversation summary, and only then are the legs bridged. | §35, §36, §38, §40, §42 |

---

## 1. Project Objective

Build a production-ready, **multi-tenant SaaS platform for AI-powered voice agents** using
**LiveKit as the realtime media and SIP/telephony infrastructure**. [§1]

The platform receives calls from external PBXs, SIP trunks, or PSTN-connected systems and
routes them through LiveKit to **dynamically configured** AI voice agents. [§1]

Key framing constraints:

- Initial architecture supports development and testing at **small concurrency**, while being
  architected for production operation at **1,000+ concurrent AI voice calls**. [§1]
- The system must be **horizontally scalable**. Adding LiveKit, SIP, API, or AI worker capacity
  must **not require application-code changes**. [§1]
- The platform must be **configuration-driven**. Tenant-specific behavior must **never** be
  hard-coded. [§1, §9]
- The most important requirement: build a **generic, multi-tenant AI Voice Agent Platform, not
  a collection of hard-coded voice bots**. [§79]

### 1.1 Non-Goals (v1)

- Outbound campaign dialing / predictive dialer (data model must not preclude it).
- Video or screen-share agents.
- Replacing the tenant's PBX. The platform integrates with existing PBXs, it does not become one.
- A managed BYO-vector-database integration — PostgreSQL + pgvector is the v1 choice. [§33]

---

## 2. Core Architectural Principle

The system is divided into **two major planes**. [§2]

### 2.1 Control Plane

Responsible for configuration, administration, authentication, authorization, tenants, AI agents,
routing, provider configuration, LiveKit resource management, monitoring, and business
configuration. [§2]

```
Web UI
  |
  v
Configuration / Management API
  |
  +---------------+
  |               |
  v               v
PostgreSQL     LiveKit APIs
  |
  v
Redis / Event System
```

### 2.2 Voice Execution Plane

Responsible for realtime calls and AI processing. [§2]

```
PBX / SIP / PSTN
      |
      v
  LiveKit SIP
      |
      v
    LiveKit
      |
      v
AI Agent Worker Pool
      |
  +---+---+---+
  |   |   |
 STT LLM TTS
  |   |   |
  +---+---+---+
      |
      v
    LiveKit
      |
      v
PBX / SIP / PSTN
```

### 2.3 Hard Boundaries

| Rule | Source |
|---|---|
| The Control Plane **must not** process the realtime audio stream. | [§2] |
| The Voice Execution Plane **must not** contain tenant-specific hard-coded configuration. | [§2] |
| PostgreSQL is the **configuration source of truth**. | [§10, §79] |
| Redis provides distributed state, caching, and events. | [§79] |
| LiveKit provides realtime media and SIP execution. | [§79] |
| AI Agent Workers execute the configured voice-agent behavior. | [§79] |
| The Web UI is the primary configuration interface for tenant administrators. | [§79] |
| Infrastructure configuration remains under platform/DevOps control. | [§13, §79] |
| Do **not** design the system around one central AI process. | [§5] |

---

## 3. Personas and Roles

### 3.1 Platform Roles [§8]

| Role | Scope | Responsibilities |
|---|---|---|
| `SUPER_ADMIN` | Platform | Full platform control; tenants, LiveKit clusters, infrastructure, providers, capacity, audit. Sees platform capacity dashboard. [§48] |
| `PLATFORM_OPERATOR` | Platform | Day-2 operations, monitoring, LiveKit resource repair, drift resolution; no billing/tenant-destructive rights. |

### 3.2 Tenant Roles [§8]

| Role | Responsibilities |
|---|---|
| `TENANT_ADMIN` | Full control of one tenant: users, PBXs, SIP trunks, DIDs, agents, routing, billing view. |
| `MANAGER` | Operational configuration except user/billing management. |
| `AGENT_MANAGER` | Build, test, publish, and roll back AI agents; manage tools and knowledge bases. |
| `ANALYST` | Read calls, recordings, transcripts, analytics, usage. |
| `VIEWER` | Read-only. |

### 3.3 Granular Permissions [§8]

`agents.read`, `agents.write`, `agents.publish`,
`pbxs.read`, `pbxs.write`,
`sip_trunks.read`, `sip_trunks.write`,
`calls.read`, `recordings.read`, `analytics.read`,
`users.manage`, `billing.read`

**The backend must enforce permissions independently of the frontend.** [§8]

### 3.4 Primary User Journey (target experience) [§77]

A **non-developer tenant administrator** must be able to complete the entire onboarding flow
through the UI alone:

```
Create Tenant → Create User → Create PBX → Create SIP Trunk → Add DID →
Create AI Agent → Select STT → Select LLM → Select TTS → Select Voice →
Configure Prompt → Configure Tools → Configure Knowledge Base →
Configure Transfer → Create Routing Rule → Publish Agent → Receive Call
```

With **no** source-code modification, **no** direct database modification, **no** SSH, and
**no** manual LiveKit CLI configuration. [§77]

---

## 4. Technology Stack [§3]

| Layer | Technology |
|---|---|
| Backend | Python, FastAPI, Pydantic, SQLAlchemy, Alembic, AsyncIO |
| Frontend | React, Next.js, TypeScript — responsive, suitable for desktop administration |
| Database | PostgreSQL; pgvector for RAG where required |
| Cache / distributed state | Redis |
| Realtime | LiveKit Server, LiveKit SIP, LiveKit Agents, WebRTC, SIP, RTP |
| Object storage | S3, MinIO, S3-compatible |
| Monitoring | Prometheus, Grafana; optional Loki, Tempo, OpenTelemetry |

Recordings and large files **must not** be stored inside PostgreSQL. [§3, §39]

---

## 5. Multi-Tenancy and Isolation

### 5.1 Multi-Tenancy [§6]

Multi-tenancy is **mandatory from the beginning**. Each tenant owns: Users, PBXs, SIP Trunks,
Phone Numbers, AI Agents, Routing Rules, Tools, Knowledge Bases, Calls, Recordings, Analytics.

**Every tenant-owned database object must contain `tenant_id`.** [§6]

### 5.2 Tenant Isolation [§7]

| ID | Requirement |
|---|---|
| ISO-1 | Tenant isolation must be enforced **server-side**. |
| ISO-2 | **Never** trust a tenant ID supplied by the browser. |
| ISO-3 | Tenant identity must come from the authenticated user's session/token. |
| ISO-4 | Tenant A must never access Tenant B's users, PBXs, SIP trunks, DIDs, AI agents, prompts, provider credentials, tools, knowledge bases, calls, recordings, transcripts, analytics, usage, or billing information. |
| ISO-5 | Tenant isolation must be **tested explicitly**. |

---

## 6. Configuration-Driven Architecture [§9]

No tenant-specific configuration may be hard-coded in source code. The following must **all**
come from configuration:

tenant IDs · PBX addresses · SIP trunks · DIDs · agent mappings · prompts · voices ·
STT providers · LLM providers · TTS providers · models · routing · transfer destinations ·
business hours · API tools · knowledge bases · call limits

### 6.1 Configuration Control Plane [§10]

A dedicated **Configuration API** is the central control plane between the Web UI and
PostgreSQL, Redis, LiveKit APIs, and AI Worker configuration.

```
Web UI
  |
  v
Configuration API
  |
  +---------- PostgreSQL
  |
  +---------- Redis
  |
  +---------- LiveKit APIs
  |
  +---------- AI Worker configuration
```

---

## 7. LiveKit Management Requirements

### 7.1 LiveKit Administration Section [§11]

The platform must explicitly manage LiveKit through an administration section covering:
Clusters · SIP Configuration · SIP Trunks · Dispatch Rules · Agent Dispatch · Rooms ·
Participants · Media · Codecs · Recording/Egress · TURN/ICE · Health · Metrics.

- Where LiveKit provides an API for a resource, **use the API**. [§11]
- Administrators must **not** be required to run LiveKit CLI commands for normal tenant
  configuration. [§11]

### 7.2 LiveKit Resource Synchronization [§12]

When an administrator creates a LiveKit resource through the UI:

```
UI → Configuration API → Validate → PostgreSQL → LiveKit API → LiveKit Resource
   → Store LiveKit Resource ID
```

Example record shape for `sip_trunks`: `id`, `tenant_id`, `name`, `livekit_trunk_id`, `status`.

Synchronization status values: `SYNCED`, `PENDING`, `FAILED`, `DRIFTED`. [§12]

Operations provided: **Synchronize**, **Retry**, **Repair**. [§12]

### 7.3 Configuration Drift Detection [§46]

The platform must detect discrepancies between PostgreSQL and LiveKit — e.g. database says
`SIP trunk = Active` while LiveKit reports `SIP trunk = Missing` — surface
**"Configuration Drift Detected"**, and offer **Synchronize**, **Repair**, **Retry**.

### 7.4 Infrastructure vs Tenant Configuration [§13]

Infrastructure-level LiveKit configuration is **platform/DevOps controlled** and must **not**
be exposed to normal tenant administrators: Redis, RTC ports, TCP ports, RTP port ranges,
external IP, TLS certificates, load balancers, Kubernetes, networking, firewall, cluster
topology.

Tenant-level LiveKit configuration **must** be UI/API managed. [§13]

---

## 8. Telephony Configuration Requirements

### 8.1 PBX Management [§14]

Support SIP-compatible PBXs including **Asterisk, FreePBX, FreeSWITCH, FusionPBX, Kamailio,
3CX, Cisco, Mitel**, and other SIP-compatible systems.

- Fields: Name, Type, Host, Port, Transport, Status.
- Operations: Create, Edit, Delete, Enable, Disable, **Test Connection**.

### 8.2 SIP Trunk Management [§15]

- Operations: Create, Edit, Delete, Enable, Disable, **Test**.
- Configuration: Name, PBX, Direction, SIP Host, Port, Transport, Authentication, Username,
  Password, Allowed IPs, Codecs, DTMF, Media Encryption, Phone Numbers.
- The backend **must create/update the corresponding LiveKit SIP resource through the
  LiveKit API**.

### 8.3 SIP Configuration Wizard [§16]

A guided 10-step wizard: 1. Select PBX → 2. SIP Configuration → 3. Authentication →
4. Codec → 5. Security → 6. Connection Test → 7. Phone Number → 8. AI Agent →
9. Routing → 10. Complete.

**A normal tenant administrator must never need to edit SIP configuration files.** [§16]

### 8.4 Phone Number / DID Management [§17]

Fields: Number, Tenant, PBX, SIP Trunk, Inbound Agent, Routing Rule, Business Hours,
Fallback, Status.

---

## 9. AI Agent Requirements

### 9.1 Agent Builder [§18, §62]

Each agent must support: Name, Description, Language, Greeting, System Prompt;
STT Provider + STT Model; LLM Provider + LLM Model; TTS Provider + TTS Model + Voice;
Temperature, Interruption, Silence Timeout, Maximum Call Duration;
Recording, Transcription; Transfer Policy, Business Rules, Tools, Knowledge Base.

Operations: Create, Edit, Clone, Test, Publish, Rollback, Enable, Disable, Archive. [§18]
Agent Builder UI operations: Save Draft, Test, Publish, Rollback. [§62]

**A non-developer must be able to configure all of the above.** [§62]

### 9.2 Agent Versioning [§19]

Agents must have versions, e.g. `v1 Published`, `v2 Draft`, `v3 Testing`.

| ID | Requirement |
|---|---|
| VER-1 | Publishing a new version **must not** change existing calls. |
| VER-2 | Existing calls continue using the version with which they started. |
| VER-3 | New calls use the current published version. |
| VER-4 | A call must use a **consistent** agent configuration version throughout its lifetime. [§45] |

### 9.3 Agent Routing [§20]

```
Incoming Call → Tenant → PBX → SIP Trunk → DID → Routing Rule → AI Agent
              → Published Agent Version
```

Routing conditions may include: tenant, PBX, SIP trunk, DID, caller number,
destination number, business hours, campaign, priority.

### 9.4 LiveKit Dispatch Rules [§21]

The system must create and manage LiveKit dispatch rules from the configuration UI/API.
Example: Tenant `Hotel ABC`, DID `+880XXXXXXXXXX`, SIP Trunk `Hotel PBX`,
Agent `Hotel Reservation Agent`, Room Strategy `Individual Room`,
Agent Dispatch `hotel-reservation-agent`.

- Dispatch rules must be **long-lived configuration objects**. [§21]
- **Do not create a new dispatch rule for every call.** [§21]

### 9.5 Configuration Validation [§63]

Before publishing an agent, validate: STT provider, LLM provider, TTS provider, Voice, Prompt,
Tools, Knowledge Base, Transfer destination, Routing, PBX, SIP trunk, DID.

**Invalid configurations must not be allowed to become production-active.** [§63]

### 9.6 Dependency Validation [§64]

Show configuration dependencies as a tree:

```
Agent
 +-- STT
 +-- LLM
 +-- TTS
 |    +-- Voice
 +-- Tools
 +-- Knowledge Base
 +-- Transfer
```

If a required provider / voice / tool is disabled, **clearly show the problem**. [§64]

---

## 10. Call Execution Requirements

### 10.1 Per-Call Architecture [§22, §78]

```
PBX → LiveKit SIP → LiveKit → SIP Dispatch Rule → Unique LiveKit Room
    → Agent Dispatch → AI Agent Worker → (STT | LLM | TTS)
    → LiveKit Room → SIP/RTP → PBX
```

Production flow adds clustering: `PBX/SIP → LiveKit SIP Cluster → LiveKit Cluster →
Dispatch Rule → Unique LiveKit Room → Agent Dispatch → AI Worker Pool → STT/LLM/TTS →
LiveKit Room → SIP/RTP → PBX`. [§78]

### 10.2 AI Agent Worker [§23]

The AI Agent Worker must be a **generic execution engine** and must **not** contain hard-coded
customer/tenant logic. [§23, §79]

At call startup it loads: tenant, agent, agent version, prompt, STT configuration,
LLM configuration, TTS configuration, voice, tools, knowledge base, transfer rules,
call policies — then **executes that configuration**. [§23]

### 10.3 Realtime Voice Pipeline [§28]

```
Caller → SIP → LiveKit → Audio → VAD / Turn Detection → STT → LLM → TTS
       → LiveKit → SIP → Caller
```

**Use streaming wherever supported.** [§28]

### 10.4 Barge-In [§29]

When the caller starts speaking while TTS is playing:

```
Caller Speech → Detect Interruption → Cancel TTS → STT → LLM
```

Support: VAD, turn detection, interruption, TTS cancellation, silence timeout. [§29]

### 10.5 Conversation Memory [§34]

Each call must have **isolated** conversation state, storing `conversation_id`, `call_id`,
`tenant_id`, `messages`, `tool_calls`, `tool_results`, `summary`.
Use summarization for long conversations.

### 10.6 Configuration Cache [§45]

**Do not query PostgreSQL for every audio/conversation event.**

```
Worker → Load configuration → Cache/local call context → Execute call
```

### 10.7 Call State Machine [§42]

Primary states: `NEW`, `RINGING`, `ANSWERED`, `AI_CONNECTED`, `IN_PROGRESS`, `TRANSFERRING`,
`HUMAN_AGENT`, `COMPLETED`.
Additional states: `FAILED`, `TIMEOUT`, `CANCELLED`, `BUSY`, `NO_ANSWER`.

### 10.8 Call Correlation ID [§43]

Every call must have a unique `call_id`, propagated through PBX, SIP, LiveKit, Agent, STT, LLM,
TTS, Database, Logs, Recording, Transcript. **Mandatory for debugging and tracing.**

### 10.9 Graceful Shutdown [§51]

On `SIGTERM`, an AI worker must: stop accepting new calls → finish existing calls →
disconnect cleanly → exit. **Deployments must not unnecessarily terminate active calls.**

---

## 11. AI Provider Requirements

### 11.1 Provider Abstraction [§24]

```python
class STTProvider:
    async def transcribe(...): ...

class LLMProvider:
    async def generate(...): ...

class TTSProvider:
    async def synthesize(...): ...
```

**Provider-specific code must remain inside provider adapters.** [§24]

### 11.2 Supported Providers [§25]

| Kind | Providers |
|---|---|
| STT | Deepgram, OpenAI Whisper, ElevenLabs, Google, Azure |
| LLM | OpenAI, Anthropic, Google Gemini, local/self-hosted models |
| TTS | ElevenLabs, Cartesia, OpenAI, Google, Azure, Deepgram |

The architecture must allow additional providers later **without redesigning the platform**. [§25]

#### 11.2.1 Implementation approach

Every server speaking the OpenAI API shape is served by **one** adapter
(`openai_compatible`) with a per-credential base URL. That covers OpenAI itself
alongside self-hosted stacks — ollama, vLLM, speaches, faster-whisper — so
§25's local/self-hosted requirement needs no second code path, and a tenant
moves between hosted and self-hosted by changing a URL.

Vendors with a genuinely different API shape (Anthropic, Deepgram, ElevenLabs,
Cartesia) each get their own adapter behind the same interfaces. Adding one is
a new module plus one registry entry; nothing in the pipeline changes.

| ID | Requirement |
|---|---|
| PRV-1 | The base URL is a property of the **credential**, not the platform, so two tenants may point the same adapter at different endpoints. |
| PRV-2 | STT, LLM and TTS credentials are independent, so a tenant may combine a hosted model with self-hosted speech. |
| PRV-3 | A provider named in the database but absent from the running build must fail **loudly** at validation, never fall back to a different vendor silently. |
| PRV-4 | Changing an agent's provider must not affect calls in progress. [§19, §45] |

### 11.3 ElevenLabs [§26]

ElevenLabs must be supported as a TTS provider, with **streaming TTS** and integration with the
LiveKit Agents voice pipeline. Provider credentials must be configured securely.

### 11.4 Voice Library [§27]

Fields: Provider, Voice ID, Voice Name, Language, Accent, Description, Status.
Operations: Add, Edit, Delete, Enable, Disable, Test, **Set Default**.

### 11.5 External Provider Failure [§55]

All AI providers must have: **Timeout, Retry, Exponential Backoff, Circuit Breaker,
Fallback Provider**.

**A provider failure must not crash the complete worker pool.** [§55]

---

## 12. Tools, Knowledge and Transfer

### 12.1 Tools / Function Calling [§30]

Agents must support configurable tools. Examples: `get_customer()`, `check_order()`,
`create_order()`, `cancel_order()`, `check_inventory()`, `send_sms()`, `send_email()`,
`transfer_call()`.
**Tools must be assignable to individual agents.** [§30]

### 12.2 API Tool Builder [§31]

UI for HTTP API tools with fields: Tool Name, Description, HTTP Method, URL, Authentication,
Headers, Request Schema, Response Schema, Timeout, Retry Policy.

Support variable substitution such as `{{customer_id}}`, `{{order_id}}`, `{{caller_number}}`.

**Validate tool schemas before publishing an agent.** [§31]

### 12.3 Tool Permissions [§32]

Each agent must **explicitly** specify which tools it can use — e.g. a Sales Agent allowed
Customer API / Inventory API / Order API, and **not allowed** Refund API / Employee API.

### 12.4 Knowledge Base / RAG [§33]

Tenant-specific knowledge bases from sources: PDF, DOCX, TXT, CSV, web content.
Use **PostgreSQL + pgvector** initially unless another vector database is explicitly required.
Knowledge bases must be assignable to agents.

### 12.5 Human Transfer — Warm Transfer Back to the PBX [§35, CR-1]

The platform performs a **warm (attended) transfer** back to the tenant's PBX. The two parties
hear different things at the same time: the caller hears a hold announcement while the human
agent hears the AI's conversation summary, and only then are the two legs bridged.

```
                        AI decides to transfer
                                 |
                                 v
                    Generate conversation summary
                                 |
                                 v
                       Select destination (PBX)
                                 |
              +------------------+------------------+
              |                                     |
       CALLER LEG                            AGENT LEG
              |                                     |
   Play hold announcement                  Dial human agent
   "Your call is being                      via PBX / SIP
    transferred to a human                          |
    agent. Please wait."                            v
              |                            Human agent answers
   Announcement / hold                              |
   media loops while                                v
   waiting                                 Whisper summary to
              |                            the agent only —
              |                            caller cannot hear it
              |                                     |
              |                            Agent accepts the call
              +------------------+------------------+
                                 |
                                 v
                        Bridge caller <-> agent
                                 |
                                 v
                    AI leaves; call continues human-to-human
```

#### 12.5.1 Mandatory behaviours

| ID | Requirement |
|---|---|
| TR-1 | Transfer targets the tenant's **PBX** — the call goes back to the PBX rather than out to an unrelated endpoint. Destinations remain those in §35: SIP Extension, PBX Extension, PBX Queue, External Phone Number, SIP URI. |
| TR-2 | The caller **must** hear a transfer announcement as soon as the transfer begins. Default wording: *"Your call is being transferred to a human agent. Please wait."* |
| TR-3 | The announcement text/audio must be **configurable per tenant and per agent**, and must honour the agent's configured language and voice. [§9, §18] |
| TR-4 | While the human agent is being reached, the caller hears continuing announcement or hold media — never dead silence. |
| TR-5 | The receiving human agent **must** hear the AI conversation summary **before** being bridged to the caller. |
| TR-6 | The summary whisper is audible to the **human agent only**. The caller must never hear it. This is a hard audio-isolation requirement, not a preference. |
| TR-7 | The two legs are bridged only after the human agent has heard the summary and accepted the call. |
| TR-8 | The AI agent leaves the conversation once the bridge is established; the call continues human-to-human and remains recorded/transcribed per the agent's capture settings. [§39, §40] |
| TR-9 | The human agent must be able to **skip** the summary playback (e.g. by DTMF keypress) and be bridged immediately. Configurable per tenant. |
| TR-10 | If the human agent does not answer, is busy, or rejects the call, the configured **fallback chain** applies — secondary agent, PBX queue, or voicemail — and the caller is never dropped silently. [§38] |
| TR-11 | Transfer must respect the tenant's business hours: outside hours the fallback chain is used rather than a doomed transfer attempt. [§37] |
| TR-12 | Summary whisper duration must be bounded by a configurable maximum, so a long conversation cannot leave the caller waiting indefinitely. |

#### 12.5.2 Transfer status

The `transfer_status` field on the call record (§41) tracks this flow:

| Value | Meaning |
|---|---|
| `NOT_REQUESTED` | No transfer was attempted |
| `REQUESTED` | The AI decided to transfer; summary generation started |
| `ANNOUNCING` | Caller is hearing the transfer announcement |
| `DIALING_AGENT` | The human agent leg is being dialled through the PBX |
| `WHISPERING_SUMMARY` | The human agent is hearing the AI summary; legs not yet bridged |
| `BRIDGED` | Caller and human agent are connected; AI has left |
| `AGENT_NO_ANSWER` | The human agent did not answer; fallback applied |
| `AGENT_BUSY` | The human agent was busy; fallback applied |
| `AGENT_REJECTED` | The human agent declined; fallback applied |
| `FAILED` | The transfer failed for a technical reason; fallback applied |
| `ABANDONED` | The caller hung up during the transfer |

The top-level call state stays `TRANSFERRING` throughout, becoming `HUMAN_AGENT` on bridge,
per the fixed state machine in §42. `transfer_status` carries the detail rather than adding
states the spec does not define.

### 12.6 Transfer Summary and Its Delivery [§36, CR-1]

Before transfer, generate: Customer, Reason, Summary, Actions Taken, Order Information,
Sentiment, Required Next Action. The summary must be **configurable**. [§36]

Because the receiving agent hears the summary on a phone rather than reading it, the summary
has **two renderings** from one underlying record:

| Rendering | Delivery | Notes |
|---|---|---|
| **Spoken** | TTS whisper into the agent leg before bridge | Must be short enough to speak. Configurable template; bounded by TR-12. Uses the tenant's TTS provider and voice. |
| **Structured** | Screen pop / API payload / PBX metadata where the PBX supports it | Full seven-field record. Optional — the spoken rendering is the guaranteed path, since it works with any SIP-compatible PBX. |

| ID | Requirement |
|---|---|
| TS-1 | The spoken summary template must be configurable per tenant/agent, with the seven §36 fields available as substitution variables. |
| TS-2 | The spoken summary must be generated in the caller's/agent's configured language. |
| TS-3 | Summary generation must not block the caller announcement — the caller hears the announcement immediately while the summary is being produced. |
| TS-4 | If summary generation fails or times out, the transfer still proceeds with a minimal fallback whisper (e.g. caller number and reason). A failed summary must never cancel a transfer. |
| TS-5 | The structured summary must be delivered to the PBX where the PBX supports it, and its absence must not block the transfer. |

### 12.7 Business Hours [§37]

Business hours must be configurable **per tenant**, and routing must be able to use them.
Example: Mon–Thu 09:00–18:00, Fri 09:00–13:00, Sat/Sun Closed.

### 12.8 Fallback Routing [§38]

```
Primary AI Agent --X--> Secondary AI Agent --X--> PBX Queue --X--> Voicemail
```

**Fallback behavior must be configurable.** [§38]

---

## 13. Data, Recording and Observability

### 13.1 Call Recording [§39]

Recording must be configurable **per tenant/agent**. Recordings are stored in object storage
(S3, MinIO, S3-compatible). **PostgreSQL stores metadata only.**

### 13.2 Transcription [§40]

Store Speaker, Timestamp, Text, Confidence. Speaker types: Caller, AI, Human Agent.

The `Human Agent` speaker type covers the post-bridge portion of a warm transfer, so one
transcript spans the whole call across the AI and human segments. The summary whisper is
recorded as an AI-side event on the agent leg and marked as not audible to the caller —
it is part of the audit trail, not part of the conversation. [CR-1]

### 13.3 Call Database [§41]

Minimum call record: `call_id`, `tenant_id`, `agent_id`, `agent_version_id`, `pbx_id`,
`sip_trunk_id`, `did`, `room_id`, `caller_number`, `destination_number`, `direction`,
`start_time`, `answer_time`, `end_time`, `duration`, `status`, `hangup_reason`,
`recording_id`, `transcript_id`, `transfer_status`.

`transfer_status` takes the values in §12.5.2, so a warm transfer can be diagnosed after the
fact: whether the caller heard the announcement, whether the human agent was reached, whether
the summary was whispered, and whether the legs were bridged. [CR-1]

Warm transfers additionally record, per transfer attempt: announcement started/ended,
destination dialled, agent answer time, summary whisper duration, bridge time, and the
fallback branch taken if any. Without these, "the transfer felt slow" is not investigable.

### 13.4 Redis Usage [§44]

Redis may be used for: active call state, temporary state, distributed locks, rate limiting,
caching, worker coordination, configuration events.
**Persistent business data remains in PostgreSQL.**

### 13.5 Voice Latency Monitoring [§56]

Track: STT latency, LLM first-token latency, TTS first-audio latency, Time to first response,
Time to first audio, End-to-end response latency.
**These metrics must be available for troubleshooting voice quality.** [§56]

### 13.6 Monitoring [§57]

Prometheus + Grafana.

| Domain | Metrics |
|---|---|
| Infrastructure | CPU, Memory, Network, Disk, Containers, Pods, Restarts |
| LiveKit | Rooms, Participants, Active sessions, Packet loss, Jitter, Latency, Media connections |
| AI | Active calls, Agent jobs, STT latency, LLM latency, TTS latency, Errors, Worker utilization |
| Business | Calls, Answered calls, Failed calls, Average duration, Transfers, AI minutes |

### 13.7 Structured Logging [§58]

JSON logs. Every call-related event must contain `tenant_id`, `call_id`, `room_id`, `agent_id`,
`agent_version_id`, `service`, `timestamp`, `event`.

```json
{
  "service": "voice-agent",
  "tenant_id": "tenant_001",
  "call_id": "call_123",
  "agent_id": "sales",
  "event": "tts_started"
}
```

### 13.8 Health Checks [§59]

Every service must expose `/health` and `/ready`. Readiness must verify required dependencies
where appropriate.

### 13.9 Audit Logs [§69]

Record User, Tenant, Timestamp, Action, Resource, Resource ID, Old Value, New Value, IP.
Example actions: `agent.created`, `agent.updated`, `agent.published`, `pbx.created`,
`trunk.updated`, `routing.changed`, `provider.updated`, `credential.rotated`.

---

## 14. Capacity, Scaling and Reliability

### 14.1 Tenant Call Limits [§47]

Each tenant must support **Maximum Concurrent Calls**, **Maximum Daily Calls**,
**Maximum Monthly Minutes**. The platform must **enforce these limits before accepting new
calls**.

### 14.2 Platform Capacity Dashboard [§48]

`SUPER_ADMIN` must see: Total Active Calls, Total Capacity, Available Capacity, LiveKit Nodes,
SIP Nodes, AI Workers, Worker Utilization, CPU, Memory, Network, Provider Health.

### 14.3 AI Worker Scaling [§49]

AI workers must be horizontally scalable (Worker 1..N). **Do not assume a fixed number of
workers.** The safe calls-per-worker figure must be established through **load testing**.

### 14.4 Kubernetes Autoscaling [§50]

Use Kubernetes HPA or equivalent. Scaling signals: Active AI Jobs, Worker Utilization, CPU,
Memory.
**Long-running voice calls must be treated as jobs/sessions rather than ordinary short-lived
HTTP requests.** [§50]

### 14.5 Production Architecture [§5]

Production must support **independent horizontal scaling** of Frontend, API, LiveKit,
LiveKit SIP, and AI Agent Workers.

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

### 14.6 High Availability [§52]

Production must avoid single points of failure. At minimum design for: multiple LiveKit nodes,
multiple SIP nodes, multiple AI workers, multiple API replicas, PostgreSQL HA strategy,
Redis HA strategy, load balancer redundancy.

### 14.7 Containerization [§4]

All services must be containerized. Development runs on **Docker + Docker Compose**;
production is designed for **Docker + Kubernetes + Helm**.

The initial Docker Compose environment must contain at minimum:
`postgresql`, `redis`, `livekit`, `livekit-sip`, `configuration-api`, `ai-agent-worker`,
`frontend`, `minio`, `prometheus`, `grafana`.

### 14.8 Backup and Disaster Recovery [§66]

Back up PostgreSQL, configuration, recording metadata, and object storage. Redis is treated
according to whether each data type is disposable or persistent. **Provide documented disaster
recovery procedures.**

---

## 15. Security Requirements

### 15.1 Security Controls [§53]

HTTPS/TLS · JWT authentication · RBAC · Tenant isolation · SIP authentication · IP allowlists ·
Encrypted credentials · Secret management · Audit logging · Secure object storage.

### 15.2 Secret Management [§54]

Production credentials must use **Kubernetes Secrets**, or preferably **HashiCorp Vault,
AWS Secrets Manager, Google Secret Manager, or Azure Key Vault**.

**Never** put production credentials in: source code, Git, Docker images, logs, frontend code,
plaintext configuration, or agent prompts. [§54]

Secrets must never be exposed to the frontend. [§80]

### 15.3 Import / Export [§65]

Allow tenant configuration export/import covering Agents, Agent Versions, Tools, Routing,
Knowledge Base configuration, Business Rules.
**Never export secrets in plaintext.** [§65]

---

## 16. API and Data Model

### 16.1 API Structure [§67]

Base path `/api/v1/`. Minimum surface:

```
POST /tenants            GET  /tenants
POST /pbxs               GET  /pbxs             PUT /pbxs/{id}
POST /sip-trunks         GET  /sip-trunks       PUT /sip-trunks/{id}
POST /phone-numbers      GET  /phone-numbers
POST /agents             GET  /agents           PUT /agents/{id}
POST /agents/{id}/publish
POST /agents/{id}/rollback
POST /providers          GET  /providers
POST /voices             GET  /voices
POST /tools              GET  /tools
POST /knowledge-bases    GET  /knowledge-bases
POST /routing-rules      GET  /routing-rules
GET  /calls              GET  /calls/{id}
GET  /analytics          GET  /usage
```

**Generate complete OpenAPI documentation.** [§67]

### 16.2 Database Schema (minimum) [§68]

```
tenants
users, roles, permissions, user_roles
pbxs, sip_trunks, sip_credentials, phone_numbers
agents, agent_versions, agent_tools, agent_voices
providers, provider_credentials, models, voices
routing_rules, business_hours, transfer_destinations
knowledge_bases, knowledge_documents
tools, tool_permissions
calls, call_events, call_transcripts, call_transcript_segments, call_recordings
usage, billing, subscriptions
audit_logs
```

**Every tenant-owned table must support tenant isolation.** [§68]

---

## 17. Administration UIs

### 17.1 Platform Console [§60]

Dashboard · Tenants · LiveKit · Infrastructure · AI Providers · Models · Voices ·
System Users · Monitoring · Audit Logs · Settings.

### 17.2 Tenant Console [§61]

Dashboard · AI Agents · Agent Versions · PBXs · SIP Trunks · Phone Numbers · Routing ·
Business Hours · Knowledge Bases · Tools · Calls · Recordings · Transcripts · Analytics ·
Users · Usage · Settings.

---

## 18. Quality Requirements

### 18.1 Testing [§70]

| Level | Coverage |
|---|---|
| Unit | tenant isolation, authorization, routing, configuration, agent logic, provider adapters, tools |
| Integration | PBX → SIP → LiveKit → AI Agent → STT → LLM → TTS |
| End-to-end | actual SIP/phone calls through the complete system |

### 18.2 Load Testing [§71]

Target **1,000 concurrent calls**, tested progressively at 10, 25, 50, 100, 250, 500, 750, 1000.

Measure: CPU, RAM, network, SIP capacity, RTP performance, packet loss, jitter, STT latency,
LLM latency, TTS latency, first-audio latency, call failure rate, worker utilization,
LiveKit utilization.

### 18.3 Sustained Load Testing [§72]

At high concurrency, test for **15, 30, and 60 minutes**.
**Do not validate scalability using only short bursts.** [§72]

### 18.4 Failure Testing [§73]

Test and **document recovery behavior** for: AI worker failure, LiveKit node failure, SIP node
failure, API restart, Redis failure, Database failure, STT failure, LLM timeout, TTS failure,
network packet loss, high CPU, worker exhaustion.

### 18.5 Capacity Report [§74]

Produce a capacity report based on **actual benchmarks**. Illustrative arithmetic only:
if one worker safely handles 15 concurrent calls, then `1000 / 15 = 67 workers`, plus
production headroom. **The final worker count must come from actual benchmarking.** [§74]

### 18.6 No Fake Scalability [§75]

The system **must not** claim "Supports 1,000 concurrent calls" simply because Kubernetes can
start enough containers. The 1,000-call target must be validated using the actual production
architecture and AI providers, and the load test must include SIP, LiveKit, AI workers, STT,
LLM, TTS, recording, tools, and RAG **where those features are enabled in the production
configuration**. [§75]

---

## 19. Acceptance Criteria

### 19.1 Final Acceptance Test [§77]

The end-to-end tenant-administrator journey in §3.4 of this document must be completable
entirely through the UI, with no source-code modification, no direct database modification,
no SSH, and no manual LiveKit CLI configuration.

### 19.2 Definition of Done (platform)

| ID | Criterion | Source |
|---|---|---|
| AC-1 | Control Plane and Voice Execution Plane are separate services; the Control Plane handles no realtime audio. | §2, §80 |
| AC-2 | Every tenant-owned table carries `tenant_id`; cross-tenant access tests pass. | §6, §7, §68 |
| AC-3 | RBAC is enforced at the API layer, independently of the frontend. | §8, §80 |
| AC-4 | No tenant-specific value is hard-coded anywhere in the codebase. | §9, §80 |
| AC-5 | LiveKit SIP trunks, dispatch rules, and agent dispatch are created via LiveKit APIs from the UI; resource IDs stored in PostgreSQL; drift detected and repairable. | §11, §12, §46, §80 |
| AC-6 | An agent can be built, validated, published, rolled back; in-flight calls keep their original version. | §18, §19, §63 |
| AC-7 | The AI worker loads all behavior from configuration at call start and caches it for the call. | §23, §45 |
| AC-8 | STT/LLM/TTS all sit behind provider adapters with timeout, retry, backoff, circuit breaker, and fallback. | §24, §55 |
| AC-8a | Each configured provider is verified by **exercising the adapter and observing real output**, not by the absence of errors on a call. A greeting proves nothing: it is spoken by the session, not the model. | §24, §63 |
| AC-9 | Barge-in, VAD, turn detection, TTS cancellation, and silence timeout work on live calls. | §29 |
| AC-9a | A warm transfer back to the PBX plays the announcement to the caller, whispers the AI summary to the human agent only, and bridges the legs afterwards. Verified by listening to both legs: the caller must not hear the summary. | §35, §36, CR-1 |
| AC-9b | A human agent who does not answer, is busy, or rejects the transfer causes the configured fallback chain to run, and the caller is never dropped silently. | §38, CR-1 |
| AC-10 | Recordings live in object storage; PostgreSQL holds metadata only. | §3, §39 |
| AC-11 | `call_id` is present in every call-related log line and propagated across all services. | §43, §58 |
| AC-12 | `/health` and `/ready` exist on every service; readiness validates dependencies. | §59 |
| AC-13 | Tenant call limits are enforced **before** a call is accepted. | §47 |
| AC-14 | Workers drain on `SIGTERM` without dropping active calls. | §51 |
| AC-15 | Progressive load testing to 1,000 concurrent calls is executed, sustained for 60 minutes, and a benchmark-based capacity report is published. | §71, §72, §74, §75 |
| AC-16 | No production secret appears in source, Git, images, logs, frontend, plaintext config, or prompts. | §54 |
| AC-17 | Complete OpenAPI documentation is generated for `/api/v1`. | §67 |
| AC-18 | Documentation exists for development, deployment, configuration, and operations. | §80 |

---

## 20. Development Constraints (Engineering Rules) [§80]

1. Do **not** start by building a monolithic application.
2. Separate Control Plane and Voice Execution Plane.
3. Design database schemas before implementing business logic.
4. Implement tenant isolation before tenant-facing features.
5. Implement RBAC at the API layer.
6. Build provider abstractions rather than coupling the agent to one AI provider.
7. Build LiveKit integration as a dedicated service/module.
8. Store LiveKit resource IDs in PostgreSQL.
9. Implement synchronization and drift detection.
10. Do not hard-code tenant or agent configuration.
11. Do not expose secrets to the frontend.
12. Use asynchronous processing for long-running operations.
13. Make AI workers horizontally scalable.
14. Make all services containerized.
15. Make the production architecture Kubernetes-ready.
16. Add health/readiness checks from the beginning.
17. Add structured logging and correlation IDs from the beginning.
18. Write automated tests for every major module.
19. Do not claim 1,000-call capacity until load testing proves it.
20. Provide clear documentation for development, deployment, configuration, and operations.

Before implementing each major component, define: **Architecture, Database Model, API Contract,
Security Model, Error Handling, Tests, Scaling Considerations.** [§80]

---

## 21. Open Questions and Assumptions

These items are **not specified** in the source document and need a decision. None of them
block Phase 1.

| # | Question | Working assumption for planning |
|---|---|---|
| Q1 | Which LiveKit deployment model — self-hosted LiveKit Server or LiveKit Cloud? | Self-hosted, since §11/§13 require cluster, TURN/ICE, and port-range administration. |
| Q2 | Outbound calling — in scope for v1? | Out of scope for v1; the `direction` field on trunks/calls keeps the door open. |
| Q3 | Billing — is `billing`/`subscriptions` (§68) a real billing engine or usage records for an external biller? | Usage metering + records only; no payment processing in v1. |
| Q4 | Tenant isolation mechanism — PostgreSQL Row Level Security, or application-layer scoping? | Application-layer scoping enforced in a shared repository layer **plus** RLS as defense in depth. |
| Q5 | Target languages/locales for STT/TTS at launch? | English first; the model is language-agnostic per agent (§18 Language). |
| Q6 | Expected knowledge-base corpus size per tenant? | Assume pgvector is sufficient (§33); revisit only if a tenant exceeds it. |
| Q7 | Data-retention policy for recordings and transcripts (regulatory)? | Configurable per tenant; needs legal input before go-live. |
| Q8 | Call-recording consent/announcement requirements per jurisdiction? | Must be resolved before production traffic — recording is a legal exposure, not just a feature. |
| Q9 | Which cloud/provider for production Kubernetes? | Cloud-agnostic Helm charts; secret backend pluggable per §54. |
| Q10 | Does "campaign" in routing conditions (§20) imply a campaign entity? | Treated as an optional routing attribute in v1, not a first-class entity. |

---

## 22. Success Statement [§80]

> The resulting project must be production-oriented, modular, testable, observable, and capable
> of evolving from a small deployment into a 1,000+ concurrent-call multi-tenant SaaS platform.
