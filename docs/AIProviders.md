# AI Providers — Design Document
## AI Setup Section · Tenant Console

| | |
|---|---|
| **Document** | AIProviders.md |
| **Version** | 1.0 |
| **Date** | 2026-09-11 |
| **Status** | **Implemented** — Phase 4c complete (2026-09-12) |
| **Related** | [`LiveKitVoiceAgentPRD.md`](./LiveKitVoiceAgentPRD.md) §9, §11, §24–§27 · [`LiveKitVoiceAgentPlan.md`](./LiveKitVoiceAgentPlan.md) Phase 4b |

---

## 1. Purpose and Scope

This document describes the **AI Setup** section — a dedicated area in the tenant console where administrators configure every AI provider credential the platform will use. It replaces the inline key-entry experience that lives inside the agent builder (`ProviderChain.tsx`) and gives provider configuration a permanent, first-class home.

### Problem statement

Today a tenant administrator enters provider API keys *inside* the agent builder, per-agent. This creates two problems:

1. **Discovery** — there is no single screen that shows which providers a tenant has configured. The platform console shows the provider *catalog* (platform-level), not the tenant's own credentials.
2. **Reuse** — a tenant with five agents re-enters the same OpenAI key five times, or discovers the inline experience only when trying to publish an agent.

### Design intent

> Configure your providers once, in one place. Every agent builder then inherits those choices through dropdowns that only offer what is already working.

The approach is **not** a redesign of the underlying data model — the `provider_credentials` table already stores exactly these keys, and the catalog API (`GET /catalog`, `PUT /catalog/credentials`, `POST /catalog/credentials/{id}/verify`, `DELETE /catalog/credentials/{id}`) already exists. This feature is a dedicated surface over that existing backend, plus a new `EMBEDDING` provider kind that the spec did not need until knowledge-base ingestion (Phase 6) requires an embedding model.

---

## 2. AI Setup — Tenant Console Section

### 2.1 Sidebar placement

A new entry **AI Setup** is added to the tenant console sidebar, between **Agents** and **Routing**. It uses the `agents.read` permission to appear; `agents.write` is required for any write operation (consistent with the catalog endpoints it wraps).

```
Sidebar (tenant console)
  Dashboard
  AI Agents
→ AI Setup          ← new
  Routing
  Business Hours
  ...
```

Route: `/ai-setup`

### 2.2 Tab layout

The section has four tabs. Each tab manages one *kind* of provider.

| Tab label | `ProviderKind` | Purpose |
|---|---|---|
| **LLM** | `LLM` | Language models (OpenAI, Anthropic, Gemini, local) |
| **Embedding** | `EMBEDDING` | Embedding models for knowledge-base RAG (Phase 6) |
| **STT** | `STT` | Speech-to-text (Deepgram, OpenAI Whisper, Azure, self-hosted) |
| **TTS** | `TTS` | Text-to-speech (ElevenLabs, Cartesia, OpenAI, Kokoro, Azure) |

All four tabs follow the **same layout and interaction pattern**, described once in §3.

---

## 3. Tab Layout and Interaction Pattern

### 3.1 Provider table

Each tab renders a table of providers for which the tenant has already stored a credential. Each row shows:

| Column | Source | Notes |
|---|---|---|
| Provider | `providers.display_name` | e.g. "OpenAI", "Deepgram" |
| Label | `provider_credentials.label` | `primary` by default |
| Key | `provider_credentials.key_hint` | Last four characters only — `…sk-9f3a`. Never the full key. |
| Base URL | `provider_credentials.base_url` | Shown as the hostname only if set; blank for hosted providers |
| Verified | `provider_credentials.last_verified_at` | Green tick + relative timestamp if verified; amber "Unverified" if not |
| Added | `provider_credentials.created_at` | |
| Actions | — | **Test** · **Rotate** · **Delete** |

Providers that require a credential (`providers.requires_credential = true`) and have no credential stored are **not** shown in this table; they appear only in the Add modal. Self-hosted providers (`providers.requires_credential = false`) are shown with a "No key required" badge.

An **empty state** when no credentials are stored reads:
> No LLM providers configured yet. Add one to start building agents.

### 3.2 Add button and modal

An **Add LLM** button (or **Add Embedding**, **Add STT**, **Add TTS** depending on the tab) sits at the top-right of each tab.

Clicking it opens a modal with these fields:

| Field | Type | Notes |
|---|---|---|
| **Provider** | Dropdown | Populated from `GET /catalog`, filtered to the current tab's `ProviderKind`. Only `ACTIVE` catalog rows appear. Self-hosted providers that need no key appear at the bottom of the list with a `· self-hosted` tag. |
| **API Key** | Password input | Hidden text. Required unless the selected provider has `requires_credential = false`, in which case the field is hidden. |
| **Base URL** *(optional)* | Text | Pre-filled from `providers.default_base_url` if set. Editable so a tenant can point at a regional or self-hosted endpoint. A warning badge appears if a public API hostname is entered while the provider is tagged self-hosted. |
| **Label** | Text | Defaults to `primary`. Allows `fallback` or `local` for the provider fallback chain (§55). |

Modal actions:

| Button | Behaviour |
|---|---|
| **Test Connection** | Calls `POST /catalog/credentials/{id}/verify` (or the draft path described in §5.2). Reports: reachable / key accepted / latency / URL checked. Does not save the key. |
| **Add** | Enabled only after a successful Test Connection, or when the provider requires no key. Calls `PUT /catalog/credentials` to encrypt and store. Closes the modal on success and refreshes the table. |
| **Cancel** | Discards and closes. |

> **Test before Add** is enforced in the UI. The **Add** button is disabled until a test returns `ok: true`. This means every credential in the table has been verified at least once — the `last_verified_at` column is non-null on creation.

### 3.3 Row actions

**Test** re-runs `POST /catalog/credentials/{id}/verify` against the stored (encrypted) key and updates `last_verified_at`. Shows the same result panel as the modal.

**Rotate** opens the same modal pre-filled with the existing label and base URL, but with a blank key field. Submitting calls `PUT /catalog/credentials` with the same `provider_id` and `label`, which updates the ciphertext in place and clears `last_verified_at` (a rotated key is not verified until tested).

**Delete** prompts for confirmation:
> Removing this key stops future calls from using [Provider]. Any agent that lists this provider as its primary will fail to publish until a new key is added. Are you sure?

Calls `DELETE /catalog/credentials/{id}`. The platform does not block deletion even if agents reference this provider — a leaked key must be withdrawable immediately.

### 3.4 Verification result panel

After Test Connection, a small result strip appears below the key field (in the modal) or inline in the row (for the row action):

```
✓  OpenAI accepted this key  ·  api.openai.com/v1/models  ·  341 ms
```
or
```
✗  The provider rejected this key (401)  ·  api.openai.com/v1/models
```

The panel states exactly what was checked and what it does **not** prove:
> This confirms the key is accepted and the endpoint is reachable. It does not confirm a specific model is available to this key.

---

## 4. Embedding Tab — New Provider Kind

### 4.1 `EMBEDDING` kind

The `ProviderKind` enum currently contains `STT`, `LLM`, `TTS`. A fourth value, `EMBEDDING`, must be added.

```python
# services/shared/shared/models/enums.py
class ProviderKind(StrEnum):
    STT = "STT"
    LLM = "LLM"
    TTS = "TTS"
    EMBEDDING = "EMBEDDING"   # ← new
```

Embedding models are used by the RAG retrieval pipeline (Phase 6 — `worker/rag/`). They are distinct from LLMs: the same credential may cover both (e.g. an OpenAI key works for both `gpt-4o` and `text-embedding-3-small`), but the agent builder selects an embedding model separately from the language model.

### 4.2 Seeded providers — full catalog (33 rows)

The development seed registers the following providers. All are seeded by `python -m app.cli seed-platform` (or `make migrate && docker compose exec configuration-api python -m app.cli seed-platform` after applying migrations).

**LLM (10)**

| slug | adapter | display_name | `requires_credential` |
|---|---|---|---|
| `openai_compatible` | `openai_compatible` | Self-Hosted LLM (OpenAI-compatible) | false |
| `echo_dev` | `echo` | Echo (dev/test) | false |
| `openai_hosted` | `openai_compatible` | OpenAI | true |
| `anthropic` | `anthropic` | Anthropic | true |
| `groq` | `openai_compatible` | Groq | true |
| `mistral` | `openai_compatible` | Mistral AI | true |
| `google_gemini` | `openai_compatible` | Google Gemini | true |
| `together` | `openai_compatible` | Together AI | true |
| `deepseek` | `openai_compatible` | DeepSeek | true |
| `ollama` | `openai_compatible` | Ollama (local) | false |

**STT (9)**

| slug | adapter | display_name | `requires_credential` |
|---|---|---|---|
| `openai_compatible` | `openai_compatible` | Self-Hosted STT (OpenAI-compatible) | false |
| `openai_hosted` | `openai_compatible` | OpenAI Whisper | true |
| `deepgram` | `deepgram` | Deepgram | true |
| `assemblyai` | `assemblyai` | AssemblyAI | true |
| `google_stt` | `google_stt` | Google Speech-to-Text | true |
| `speechmatics` | `speechmatics` | Speechmatics | true |
| `gladia` | `gladia` | Gladia | true |
| `elevenlabs_stt` | `elevenlabs` | ElevenLabs STT | true |
| `azure_stt` | `azure` | Azure Speech-to-Text | true |

**TTS (9)**

| slug | adapter | display_name | `requires_credential` |
|---|---|---|---|
| `openai_compatible` | `openai_compatible` | Self-Hosted TTS (OpenAI-compatible) | false |
| `openai_hosted` | `openai_compatible` | OpenAI TTS | true |
| `elevenlabs` | `elevenlabs` | ElevenLabs | true |
| `cartesia` | `cartesia` | Cartesia | true |
| `playht` | `playht` | PlayHT | true |
| `lmnt` | `lmnt` | LMNT | true |
| `deepgram_tts` | `deepgram` | Deepgram TTS | true |
| `google_tts` | `google` | Google Cloud TTS | true |
| `azure_tts` | `azure` | Azure TTS | true |

**Embedding (5)**

| slug | adapter | display_name | `requires_credential` |
|---|---|---|---|
| `openai_compatible` | `openai_compatible` | Self-Hosted Embedding (OpenAI-compatible) | false |
| `openai_hosted` | `openai_compatible` | OpenAI Embeddings | true |
| `cohere` | `cohere` | Cohere | true |
| `voyage` | `voyage` | Voyage AI | true |
| `google_embedding` | `google_embedding` | Google Embedding | true |

The Embedding tab is **shown** from day one. A phase note on the tab reads: *"Embedding is used by knowledge-base ingestion, which lands in Phase 6. You can configure your provider now; it will be used once ingestion is available."* The tab is not disabled — it is honest about what is not yet active (same pattern as Recordings and Knowledge Bases in PRD §17.3).

### 4.3 Alembic migration note — VARCHAR enum, no native type to alter

**Important:** `providers.kind` is stored as **VARCHAR(64)** with client-side validation only (`native_enum=False` in `enum_column`). There is **no** native PostgreSQL enum type named `providerkind`. Adding `EMBEDDING` to the Python `ProviderKind` enum requires no database schema change for the providers table.

The two migrations that must be run are:

| Revision | What it does |
|---|---|
| `a3f8c2e91d47` | No-op — exists only to advance the revision chain (providers table needs no change) |
| `b7d4f1a02c58` | Adds `embedding_provider_id` and `embedding_model_id` nullable FK columns to `agent_versions` |

Run them together:

```bash
make migrate
# or directly:
docker compose -f deploy/docker-compose.yml --env-file .env exec -T configuration-api alembic upgrade head
```

Then re-seed to populate the new EMBEDDING provider rows (and any other providers added since the last seed):

```bash
docker compose -f deploy/docker-compose.yml --env-file .env exec -T configuration-api python -m app.cli seed-platform
```

> **Do not** write `ALTER TYPE providerkind ADD VALUE IF NOT EXISTS 'EMBEDDING'` in a migration — it will fail with `type "providerkind" does not exist`.

---

## 5. API Surface

### 5.1 Existing endpoints (unchanged)

The AI Setup section is built entirely on the existing catalog API. No new backend routes are required for the LLM, STT, and TTS tabs.

| Endpoint | Method | Used by |
|---|---|---|
| `/catalog` | `GET` | Populate provider dropdowns in the Add modal |
| `/catalog/credentials` | `GET` | Populate the credential table on each tab |
| `/catalog/credentials` | `PUT` | Store or rotate a key (Add / Rotate) |
| `/catalog/credentials/{id}/verify` | `POST` | Test Connection (row action) |
| `/catalog/credentials/{id}` | `DELETE` | Delete a credential |

All five require `agents.read` (GET) or `agents.write` (write operations), matching their existing permission declarations.

### 5.2 Test before save — draft verification path

The modal tests a key *before* saving it. The existing `/catalog/credentials/{id}/verify` endpoint operates on a stored (already-encrypted) row. A key typed into the modal is not yet stored.

Two approaches:

**Option A (preferred):** Add a lightweight `POST /catalog/credentials/verify-draft` endpoint that accepts `{provider_id, api_key, base_url}`, performs the same HTTP probe as the existing verify endpoint, and returns the same `CredentialVerifyResponse` — without persisting anything. The key never appears in a log or response; it is used for the probe and discarded.

**Option B:** Store the credential immediately on clicking Test, then call the existing verify endpoint, then delete it if the test fails. Avoids a new endpoint but leaves an unverified row in the table during testing.

Option A is cleaner and is the recommended approach. The endpoint must be protected by `agents.write`.

```
POST /api/v1/catalog/credentials/verify-draft
Permission: agents.write
Body: { provider_id, api_key, base_url? }
Response: CredentialVerifyResponse (same schema as the existing verify endpoint)
```

The plaintext key must not appear in the audit trail, a log line, or any response — same constraint as the store endpoint (§53, §54).

### 5.3 Catalog filter by kind

`GET /catalog` currently returns all provider kinds together. The AI Setup tabs filter client-side by `kind`. No backend change is needed; the single endpoint remains the source of truth for the agent builder too.

---

## 6. Agent Builder Integration

### 6.1 Current behaviour

The agent builder's `ProviderChain.tsx` shows every `ACTIVE` provider from the catalog, regardless of whether the tenant has a stored credential. If a required provider has no credential, publish validation fails with an error.

### 6.2 New behaviour

After AI Setup lands, the agent builder's provider dropdowns change in one way:

> **Only providers where the tenant has a stored credential (or `requires_credential = false`) appear in the dropdown.**

This is a UI-side filter on `credential_set: true` (already returned by `GET /catalog`). Providers without a key are hidden from the dropdown, not shown with a disabled state. A note at the bottom of the provider section reads:

> Don't see a provider? Add it in [AI Setup](/ai-setup).

This replaces the inline **Set key** flow inside the agent builder. The agent builder no longer has a key-entry field; it only has provider and model selection.

### 6.3 Provider dropdown

For each of the three AI stages (STT, LLM, TTS), the agent builder renders:

```
STT Provider    [ Deepgram          ▾ ]
STT Model       [ nova-2            ▾ ]  ← filtered to selected provider

LLM Provider    [ OpenAI            ▾ ]
LLM Model       [ gpt-4o-mini       ▾ ]

TTS Provider    [ Self-hosted (Kokoro) ▾ ]
TTS Model       [ kokoro-v1.0       ▾ ]
TTS Voice       [ af_sarah          ▾ ]  ← TTS only
```

Model dropdown population:
- On provider change, filter `catalog.models` by `provider_id` matching the selected provider.
- Pre-select the `is_default = true` model for that provider.
- If the previously saved model is no longer in the filtered list (provider changed), clear the selection and require a new pick.

Fallback and local tier dropdowns follow the same pattern — each tier independently shows only providers with a credential (or keyless self-hosted providers).

### 6.4 Embedding model in the agent builder

A fourth section appears in the agent builder **Capability** tab (where Knowledge Base selection lives), not in the main pipeline section:

```
Knowledge Base
  Base          [ Marketing KB      ▾ ]
  Embedding     [ OpenAI            ▾ ]
  Model         [ text-embedding-3-small ▾ ]
```

This section is visible only when a knowledge base is selected. If no embedding provider is configured in AI Setup, a message reads:
> Add an embedding provider in AI Setup to enable knowledge base retrieval.

The embedding selection is only required when `knowledge_base_id` is set. Pre-publish validation must not reject an agent with no knowledge base for missing an embedding provider.

---

## 7. Data Model

### 7.1 Existing tables (no schema change)

| Table | Role |
|---|---|
| `providers` | Platform catalog — provider rows with `kind`, `slug`, `adapter`, `requires_credential`. No tenant data. |
| `models` | Platform catalog — model slugs per provider. No tenant data. |
| `voices` | Platform catalog — TTS voices. No tenant data. |
| `provider_credentials` | Tenant-owned — encrypted keys, one per `(tenant_id, provider_id, label)`. |

The only schema change required is the `EMBEDDING` value in the `providerkind` PostgreSQL enum (§4.3) and the corresponding seeded `providers` rows.

### 7.2 `agent_versions` embedding fields

The agent version must store the tenant's embedding choice when a knowledge base is assigned:

```
agent_versions.embedding_provider_id   FK → providers.id, nullable
agent_versions.embedding_model_id      FK → models.id, nullable
```

If these columns do not yet exist, a migration adds them as nullable. Pre-publish validation requires both to be non-null when `knowledge_base_id` is non-null.

---

## 8. Security

| ID | Requirement | Source |
|---|---|---|
| AIS-S1 | The full API key is **never** returned by any endpoint, stored in a log, or present in any response model. Only the last four characters (`key_hint`) are returned. | §54 |
| AIS-S2 | Keys are encrypted at rest using Fernet with key versioning (`CredentialCipher`). The encryption key comes from `CREDENTIAL_ENCRYPTION_KEY` in the environment, never from code. | §53 |
| AIS-S3 | `POST /catalog/credentials/verify-draft` must not log the submitted key at any level. | §54, §58 |
| AIS-S4 | Tenant isolation: a tenant may only read, write, verify, and delete its own credentials. `TenantRepository.scoped()` enforces this. The response to a cross-tenant lookup is 404, not 403 — a 403 would confirm the credential exists. | §7 |
| AIS-S5 | Deleting a credential does not require confirming that no agent references it. A compromised key must be revocable immediately. | §54 |
| AIS-S6 | The audit trail records that a credential was created, rotated, or deleted — including the provider and key hint — but never the plaintext. | §69 |
| AIS-S7 | The **Add** button is disabled until Test Connection returns `ok: true`. This prevents storing a key that cannot be verified as accepted. | §63 |

---

## 9. Acceptance Criteria

| ID | Criterion |
|---|---|
| AIS-1 | **AI Setup** appears in the tenant console sidebar and is accessible to roles with `agents.read`. Write operations require `agents.write`. |
| AIS-2 | Each of the four tabs shows a table of stored credentials for that kind, with key hint, verified state, and row actions. |
| AIS-3 | The Add modal populates its provider dropdown from `GET /catalog`, filtered to the tab's `ProviderKind`. Only `ACTIVE` providers appear. Self-hosted providers are tagged. |
| AIS-4 | **Test Connection** in the Add modal verifies a key without storing it, reports the URL checked and the latency, and makes clear what it does and does not prove. |
| AIS-5 | The **Add** button is disabled until Test Connection succeeds. After saving, the new credential appears in the table with a green verified tick. |
| AIS-6 | **Rotate** clears `last_verified_at` immediately. The row shows "Unverified" until the rotated key is tested. |
| AIS-7 | **Delete** succeeds immediately even when agents reference the provider; the audit trail records the deletion including the key hint but not the key itself. |
| AIS-8 | The agent builder's provider dropdowns show only providers for which the tenant has a stored credential (or `requires_credential = false`). A link to AI Setup appears when the filtered list is empty. |
| AIS-9 | Selecting a provider in the agent builder immediately filters the model dropdown to that provider's `ACTIVE` models and pre-selects the default. |
| AIS-10 | The `EMBEDDING` kind exists in `ProviderKind`, the migration adds it to the PostgreSQL enum, and the Embedding tab renders correctly — including the "not yet active" note if Phase 6 has not landed. |
| AIS-11 | The embedding model selection appears in the agent builder only when a knowledge base is selected, and pre-publish validation requires it when `knowledge_base_id` is non-null. |
| AIS-12 | A full API key is never returned by any endpoint, present in any log, or visible in the browser's network tab. Asserted across every route, not only the credential ones. |

---

## 10. Implementation Order — **All steps complete (Phase 4c)**

| Step | What | Status |
|---|---|---|
| 1 | Add `EMBEDDING` to `ProviderKind` enum; write migration | **Done** — `shared/models/enums.py`; migration `a3f8c2e91d47` (no-op for VARCHAR schema) |
| 2 | Seed EMBEDDING providers (and expand all kinds to 29 popular providers) | **Done** — `seed.py` includes 10 LLM, 7 STT, 7 TTS, 5 Embedding providers |
| 3 | Implement `POST /catalog/credentials/verify-draft` | **Done** — `catalog.py`; key discarded after probe, not logged |
| 4 | Build `/ai-setup` page with four tabs | **Done** — `app/ai-setup/page.tsx`; grouped dropdown (Cloud vs Self-hosted), verify-before-save |
| 5 | Update agent builder — filter to credentialed providers, link to AI Setup | **Done** — `ProviderChain.tsx` rewritten; inline key-entry removed |
| 6 | Add `embedding_provider_id` / `embedding_model_id` to `agent_versions` | **Done** — migration `b7d4f1a02c58`; `EmbeddingPicker` in agent builder Capability tab |
| 7 | Extend pre-publish validation for embedding fields when KB is set | **Done** — `agents.py` `_validate_version()` emits severity=warning |

---

## 11. Open Questions

| # | Question | Working assumption |
|---|---|---|
| Q1 | Should a tenant be able to store more than one credential per provider (e.g. two different OpenAI keys with different billing accounts)? | Yes — the `label` field already supports this (`primary`, `fallback`). The Add modal exposes the label field; the UI must prevent two credentials with identical `(provider_id, label)` pairs. |
| Q2 | Should the Embedding tab be hidden entirely until Phase 6 lands, or shown with a note? | Shown with a note — same pattern as Recordings and Knowledge Bases (PRD §17.3). Hiding it would prevent a tenant from pre-configuring their embedding provider before Phase 6. |
| Q3 | Should the agent builder *warn* (rather than block) when selecting a provider whose credential is unverified (rotated but not yet tested)? | Warn inline; the publish validator already checks `requires_credential`. An unverified credential is still a stored credential — the agent may publish. The warning makes the state visible without making a rotated-but-not-yet-tested key impossible to use. |
| Q4 | Does `verify-draft` need to appear in the audit log? | No. It does not persist anything and is analogous to the connection-test on a PBX, which is also not audited. |
