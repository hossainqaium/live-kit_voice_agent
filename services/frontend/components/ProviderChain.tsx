"use client";

/**
 * Provider selection for the agent builder (spec 18, 24, 25, 26, 55, 62).
 *
 * Only providers with a stored credential (or no key requirement) are shown.
 * All credential management now lives in AI Setup — this component's job is
 * purely selection, not configuration.
 */

import Link from "next/link";

import { Badge, Field, Notice } from "@/components/ui";
import {
  type AgentVersion,
  type Catalog,
  type CatalogProvider,
  type ProviderCredential,
  type ProviderKind,
} from "@/lib/api";

/** Which version columns one tier writes. */
interface TierFields {
  provider: keyof AgentVersion;
  model: keyof AgentVersion;
  voice?: keyof AgentVersion;
}

interface Tier extends TierFields {
  label: string;
  hint: string;
}

interface Stage {
  kind: ProviderKind;
  title: string;
  blurb: string;
  tiers: Tier[];
}

const STAGES: Stage[] = [
  {
    kind: "STT",
    title: "Speech to text",
    blurb: "Transcribes the caller. The slowest stage in most deployments.",
    tiers: [
      {
        label: "Primary",
        hint: "Used for every call unless it fails.",
        provider: "stt_provider_id",
        model: "stt_model_id",
      },
      {
        label: "Fallback",
        hint: "Tried when the primary errors or times out.",
        provider: "stt_fallback_provider_id",
        model: "stt_fallback_model_id",
      },
      {
        label: "Local",
        hint: "Last resort. Pick a self-hosted provider here, or this tier adds nothing.",
        provider: "stt_local_provider_id",
        model: "stt_local_model_id",
      },
    ],
  },
  {
    kind: "LLM",
    title: "Language model",
    blurb: "Decides what the agent says.",
    tiers: [
      {
        label: "Primary",
        hint: "Used for every call unless it fails.",
        provider: "llm_provider_id",
        model: "llm_model_id",
      },
      {
        label: "Fallback",
        hint: "Tried when the primary errors or times out.",
        provider: "llm_fallback_provider_id",
        model: "llm_fallback_model_id",
      },
    ],
  },
  {
    kind: "TTS",
    title: "Text to speech",
    blurb: "Speaks the reply, and the transfer announcement.",
    tiers: [
      {
        label: "Primary",
        hint: "Used for every call unless it fails.",
        provider: "tts_provider_id",
        model: "tts_model_id",
        voice: "voice_id",
      },
      {
        label: "Fallback",
        hint: "Tried when the primary errors or times out. A different voice is normal here.",
        provider: "tts_fallback_provider_id",
        model: "tts_fallback_model_id",
        voice: "tts_fallback_voice_id",
      },
      {
        label: "Local",
        hint: "Last resort. Pick a self-hosted provider here, or this tier adds nothing.",
        provider: "tts_local_provider_id",
        model: "tts_local_model_id",
        voice: "tts_local_voice_id",
      },
    ],
  },
];

/** A provider is selectable when it either needs no key or has one stored. */
function isSelectable(provider: CatalogProvider): boolean {
  return !provider.requires_credential || provider.credential_set;
}

export function ProviderChain({
  draft,
  catalog,
  credentials,
  disabled,
  onSet,
}: {
  draft: Partial<AgentVersion>;
  catalog: Catalog | null;
  credentials: ProviderCredential[];
  disabled: boolean;
  onSet<K extends keyof AgentVersion>(key: K, value: AgentVersion[K]): void;
  /** @deprecated kept for backwards-compatibility, no longer used */
  onCredentialsChanged?(): void | Promise<void>;
  /** @deprecated kept for backwards-compatibility, no longer used */
  onError?(message: string): void;
}) {
  if (!catalog) {
    return (
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="stat-label">Speech and reasoning</div>
        <p className="subtle small" style={{ margin: "8px 0 0" }}>Loading the catalog…</p>
      </div>
    );
  }

  const selectableCount = catalog.providers.filter(isSelectable).length;

  if (selectableCount === 0) {
    return (
      <Notice tone="warn">
        No configured AI providers found.{" "}
        <Link href="/ai-setup" style={{ color: "var(--accent)" }}>
          Go to AI Setup
        </Link>{" "}
        to add your LLM, STT, and TTS provider keys. Agents can only use providers
        configured there.
      </Notice>
    );
  }

  return (
    <>
      <Notice tone="info" style={{ marginBottom: 14 }}>
        Only providers you have already configured in{" "}
        <Link href="/ai-setup" style={{ color: "var(--accent)" }}>
          AI Setup
        </Link>{" "}
        appear in the dropdowns below.
      </Notice>

      {STAGES.map((stage) => (
        <StageCard
          key={stage.kind}
          stage={stage}
          draft={draft}
          catalog={catalog}
          credentials={credentials}
          disabled={disabled}
          onSet={onSet}
        />
      ))}
    </>
  );
}

function StageCard({
  stage, draft, catalog, credentials, disabled, onSet,
}: {
  stage: Stage;
  draft: Partial<AgentVersion>;
  catalog: Catalog;
  credentials: ProviderCredential[];
  disabled: boolean;
  onSet<K extends keyof AgentVersion>(key: K, value: AgentVersion[K]): void;
}) {
  const providers = catalog.providers.filter(
    (p) => p.kind === stage.kind && isSelectable(p),
  );

  return (
    <div className="card" style={{ marginBottom: 14, padding: "12px 14px" }}>
      <div className="stat-label">{stage.title}</div>
      <p className="subtle small" style={{ margin: "2px 0 10px" }}>{stage.blurb}</p>

      {providers.length === 0 ? (
        <p className="subtle small" style={{ margin: 0 }}>
          No {stage.kind} provider configured in{" "}
          <Link href="/ai-setup" style={{ color: "var(--accent)" }}>AI Setup</Link>.
        </p>
      ) : (
        stage.tiers.map((tier) => (
          <TierRow
            key={tier.label}
            stage={stage}
            tier={tier}
            providers={providers}
            draft={draft}
            catalog={catalog}
            credentials={credentials}
            disabled={disabled}
            onSet={onSet}
          />
        ))
      )}
    </div>
  );
}

function TierRow({
  stage, tier, providers, draft, catalog, credentials, disabled, onSet,
}: {
  stage: Stage;
  tier: Tier;
  providers: CatalogProvider[];
  draft: Partial<AgentVersion>;
  catalog: Catalog;
  credentials: ProviderCredential[];
  disabled: boolean;
  onSet<K extends keyof AgentVersion>(key: K, value: AgentVersion[K]): void;
}) {
  const providerId = (draft[tier.provider] as string | null | undefined) ?? "";
  const modelId = (draft[tier.model] as string | null | undefined) ?? "";
  const voiceId = tier.voice ? ((draft[tier.voice] as string | null | undefined) ?? "") : "";

  const provider = providers.find((p) => p.id === providerId) ?? null;
  const models = catalog.models.filter((m) => m.provider_id === providerId);
  const voices = catalog.voices.filter((v) => v.provider_id === providerId);
  const credential = credentials.find((c) => c.provider_id === providerId) ?? null;

  function chooseProvider(next: string) {
    onSet(tier.provider, (next || null) as AgentVersion[typeof tier.provider]);
    onSet(tier.model, null as AgentVersion[typeof tier.model]);
    if (tier.voice) onSet(tier.voice, null as AgentVersion[typeof tier.voice]);

    const fallbackModel = catalog.models.find((m) => m.provider_id === next && m.is_default);
    if (fallbackModel) {
      onSet(tier.model, fallbackModel.id as AgentVersion[typeof tier.model]);
    }
    if (tier.voice) {
      const defaultVoice = catalog.voices.find((v) => v.provider_id === next && v.is_default);
      if (defaultVoice) {
        onSet(tier.voice, defaultVoice.id as AgentVersion[typeof tier.voice]);
      }
    }
  }

  return (
    <div
      style={{
        display: "grid",
        gap: 8,
        padding: "9px 0",
        borderTop: "1px solid var(--border)",
      }}
    >
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        <span className="small" style={{ fontWeight: 600, minWidth: 62 }}>{tier.label}</span>
        {tier.label !== "Primary" && !providerId && (
          <span className="subtle small">not configured</span>
        )}
        {tier.label === "Local" && provider && !provider.self_hosted && (
          <Badge
            tone="warn"
            title={
              "This provider is a public API, so it fails for the same reasons " +
              "the tiers above it do. A local tier only helps if it points somewhere " +
              "that does not depend on the internet."
            }
          >
            not self-hosted
          </Badge>
        )}
        {provider && !provider.requires_credential && (
          <Badge tone="neutral" title="This provider needs no API key.">
            no key needed
          </Badge>
        )}
        {provider?.requires_credential && credential && (
          <Badge
            tone={credential.last_verified_at ? "ok" : "warn"}
            title={
              credential.last_verified_at
                ? `Checked against the provider on ${new Date(credential.last_verified_at).toLocaleString()}`
                : "A key is stored but has not been checked against the provider"
            }
          >
            key ···{credential.key_hint} {credential.last_verified_at ? "verified" : "untested"}
          </Badge>
        )}
      </div>

      <div className="form-grid">
        <Field label="Provider" hint={tier.hint}>
          {(id) => (
            <select
              id={id}
              value={providerId}
              disabled={disabled}
              onChange={(e) => chooseProvider(e.target.value)}
            >
              <option value="">— none —</option>
              {providers.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.display_name}
                  {p.self_hosted ? " · self-hosted" : ""}
                </option>
              ))}
            </select>
          )}
        </Field>

        <Field label="Model">
          {(id) => (
            <select
              id={id}
              value={modelId}
              disabled={disabled || !providerId}
              onChange={(e) =>
                onSet(tier.model, (e.target.value || null) as AgentVersion[typeof tier.model])
              }
            >
              <option value="">{providerId ? "— none —" : "select a provider first"}</option>
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.slug}
                  {m.is_default ? " (default)" : ""}
                </option>
              ))}
            </select>
          )}
        </Field>

        {tier.voice && (
          <Field
            label="Voice"
            hint={
              providerId && voices.length === 0
                ? "No voices registered for this provider. A platform operator can add them under Platform → Voices."
                : undefined
            }
          >
            {(id) => (
              <select
                id={id}
                value={voiceId}
                disabled={disabled || !providerId}
                onChange={(e) =>
                  onSet(
                    tier.voice as keyof AgentVersion,
                    (e.target.value || null) as AgentVersion[keyof AgentVersion],
                  )
                }
              >
                <option value="">
                  {!providerId
                    ? "select a provider first"
                    : voices.length === 0
                    ? "— no voices registered (provider default) —"
                    : "— provider default —"}
                </option>
                {voices.map((v) => (
                  <option key={v.id} value={v.id}>
                    {v.name}
                    {v.language ? ` · ${v.language}` : ""}
                    {v.is_default ? " (default)" : ""}
                  </option>
                ))}
              </select>
            )}
          </Field>
        )}
      </div>
    </div>
  );
}
