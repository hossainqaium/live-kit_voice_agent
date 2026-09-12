"use client";

/**
 * SIP Configuration Wizard (spec 16 / PRD §8.3, Plan 4b.1).
 *
 * Ten numbered steps that compose the APIs the PBX, trunk and DID forms
 * already call. Nothing new is persisted except through those endpoints.
 * After a DID is assigned the trunk is re-synced so LiveKit accepts the
 * number — create-trunk syncs an empty accepted list.
 */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import { Badge, Button, Field, Loading, Notice } from "@/components/ui";
import {
  ApiError, api, FALLBACK_ACTION_LABELS, PBX_TYPE_LABELS,
  type Agent, type BusinessHours, type Pbx, type PbxInput, type PbxTestOutcome,
  type PbxType, type PhoneNumber, type RoutingRule, type SipTransport,
  type SipTrunk, type SipTrunkInput, type TrunkDirection,
} from "@/lib/api";
import { useAuth, useRequireAuth } from "@/lib/auth";

const STEPS = [
  "Select PBX",
  "SIP Configuration",
  "Authentication",
  "Codec",
  "Security",
  "Connection Test",
  "Phone Number",
  "AI Agent",
  "Routing",
  "Complete",
] as const;

const CODECS = ["PCMU", "PCMA", "OPUS", "G722"] as const;
const DTMF_MODES = ["RFC2833", "SIP_INFO", "INBAND"] as const;

const EMPTY_PBX: PbxInput = {
  name: "",
  pbx_type: "FUSIONPBX",
  host: "",
  port: 5060,
  transport: "UDP",
  description: "",
};

export default function SipWizardPage() {
  const { principal, loading: authLoading } = useRequireAuth();
  const { can } = useAuth();
  const canWrite = can("sip_trunks.write");
  const canCreatePbx = can("pbxs.write");

  const [step, setStep] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [pbxs, setPbxs] = useState<Pbx[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [rules, setRules] = useState<RoutingRule[]>([]);
  const [hours, setHours] = useState<BusinessHours[]>([]);

  const [pbxMode, setPbxMode] = useState<"select" | "create">("select");
  const [selectedPbxId, setSelectedPbxId] = useState<string>("");
  const [newPbx, setNewPbx] = useState<PbxInput>(EMPTY_PBX);
  const [pbx, setPbx] = useState<Pbx | null>(null);

  const [trunkDraft, setTrunkDraft] = useState<SipTrunkInput>({
    name: "",
    pbx_id: null,
    direction: "INBOUND",
    sip_host: "",
    port: 5060,
    transport: "UDP",
    allowed_ips: [],
    auth_username: "",
    auth_password: "",
    media_encryption_required: false,
    codecs: ["PCMU", "PCMA", "OPUS"],
    dtmf_mode: "RFC2833",
  });
  const [allowedText, setAllowedText] = useState("");
  const [trunk, setTrunk] = useState<SipTrunk | null>(null);

  const [didNumber, setDidNumber] = useState("");
  const [didLabel, setDidLabel] = useState("");
  const [did, setDid] = useState<PhoneNumber | null>(null);

  const [agentId, setAgentId] = useState<string>("");
  const [ruleId, setRuleId] = useState<string>("");
  const [hoursId, setHoursId] = useState<string>("");

  const [pbxTest, setPbxTest] = useState<PbxTestOutcome | null>(null);
  const [trunkTest, setTrunkTest] = useState<PbxTestOutcome | null>(null);

  const load = useCallback(async () => {
    try {
      const [pbxPage, agentPage, rulePage, hoursPage] = await Promise.all([
        api.pbxs.list(200),
        api.agents.list(200),
        api.routingRules.list(200),
        api.businessHours.list(200),
      ]);
      setPbxs(pbxPage.items);
      setAgents(agentPage.items);
      setRules(rulePage.items);
      setHours(hoursPage.items);
      if (!selectedPbxId && pbxPage.items[0]) setSelectedPbxId(pbxPage.items[0].id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load telephony catalog");
    }
  }, [selectedPbxId]);

  useEffect(() => {
    if (principal) void load();
  }, [principal, load]);

  if (authLoading || !principal) return <div className="auth-screen"><Loading /></div>;

  const currentPbx =
    pbx ?? pbxs.find((row) => row.id === selectedPbxId) ?? null;
  const chosenRule = rules.find((r) => r.id === ruleId);

  async function persistPbx(): Promise<Pbx | null> {
    if (pbx) return pbx;
    if (pbxMode === "select") {
      const existing = pbxs.find((row) => row.id === selectedPbxId) ?? null;
      setPbx(existing);
      return existing;
    }
    const created = await api.pbxs.create({
      ...newPbx,
      name: newPbx.name.trim(),
      host: newPbx.host.trim(),
      description: newPbx.description?.trim() || null,
    });
    setPbx(created);
    setPbxs((rows) => [...rows, created]);
    return created;
  }

  async function persistTrunk(owner: Pbx): Promise<SipTrunk> {
    if (trunk) return trunk;
    const created = await api.sipTrunks.create({
      ...trunkDraft,
      name: trunkDraft.name.trim(),
      sip_host: trunkDraft.sip_host.trim(),
      pbx_id: owner.id,
      auth_username: trunkDraft.auth_username?.trim() || null,
      allowed_ips: allowedText.split(",").map((s) => s.trim()).filter(Boolean),
    });
    setTrunk(created);
    return created;
  }

  async function persistDid(owner: Pbx, ownerTrunk: SipTrunk): Promise<PhoneNumber> {
    if (did) return did;
    const created = await api.phoneNumbers.create({
      number: didNumber.trim(),
      label: didLabel.trim() || null,
      pbx_id: owner.id,
      sip_trunk_id: ownerTrunk.id,
    });
    setDid(created);
    const synced = await api.sipTrunks.sync(ownerTrunk.id);
    setTrunk(synced);
    return created;
  }

  async function goNext() {
    if (!canWrite) return;
    setBusy(true);
    setError(null);
    try {
      if (step === 0) {
        const saved = await persistPbx();
        if (!saved) throw new Error("choose or register a PBX first");
        setTrunkDraft((draft) => ({ ...draft, pbx_id: saved.id }));
      }
      if (step === 4) {
        const owner = await persistPbx();
        if (!owner) throw new Error("a PBX is required before creating the trunk");
        if (!trunkDraft.name.trim() || !trunkDraft.sip_host.trim()) {
          throw new Error("trunk name and SIP host are required");
        }
        await persistTrunk(owner);
      }
      if (step === 6) {
        const owner = pbx ?? currentPbx;
        if (!owner || !trunk) throw new Error("create the trunk before adding a number");
        if (!didNumber.trim()) throw new Error("a phone number is required");
        await persistDid(owner, trunk);
      }
      if (step === 7 && did) {
        const updated = await api.phoneNumbers.update(did.id, {
          inbound_agent_id: agentId || null,
        });
        setDid(updated);
      }
      if (step === 8 && did) {
        const updated = await api.phoneNumbers.update(did.id, {
          routing_rule_id: ruleId || null,
          business_hours_id: hoursId || null,
        });
        setDid(updated);
      }
      setStep((n) => Math.min(n + 1, STEPS.length - 1));
    } catch (err) {
      setError(err instanceof ApiError || err instanceof Error ? err.message : "could not continue");
    } finally {
      setBusy(false);
    }
  }

  async function runPbxTest() {
    const target = pbx ?? currentPbx;
    if (!target) return;
    setBusy(true);
    setError(null);
    try {
      setPbxTest(await api.pbxs.test(target.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "PBX test failed");
    } finally {
      setBusy(false);
    }
  }

  async function runTrunkTest() {
    if (!trunk) return;
    setBusy(true);
    setError(null);
    try {
      setTrunkTest(await api.sipTrunks.test(trunk.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "trunk test failed");
    } finally {
      setBusy(false);
    }
  }

  function toggleCodec(codec: string) {
    const current = trunkDraft.codecs ?? [];
    setTrunkDraft({
      ...trunkDraft,
      codecs: current.includes(codec)
        ? current.filter((c) => c !== codec)
        : [...current, codec],
    });
  }

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>SIP Setup Wizard</h1>
          <p className="page-subtitle">
            Ten steps from a PBX to a number that can ring an agent. The same
            fields as the PBX, trunk and phone-number forms — in order, without
            editing a SIP config file.
          </p>
        </div>
      </div>

      {!canWrite && (
        <Notice tone="warn">You can review the steps, but saving needs sip_trunks.write.</Notice>
      )}
      {error && <Notice tone="err">{error}</Notice>}

      <ol className="wizard-rail">
        {STEPS.map((label, index) => (
          <li key={label}>
            <button
              type="button"
              aria-current={index === step ? "step" : undefined}
              data-done={index < step ? "true" : "false"}
              onClick={() => index <= step && setStep(index)}
            >
              {index + 1}. {label}
            </button>
          </li>
        ))}
      </ol>

      <div className="card">
        <h2 style={{ marginTop: 0, fontSize: 16 }}>
          Step {step + 1} — {STEPS[step]}
        </h2>

        {step === 0 && (
          <>
            <Field label="PBX">
              {(id) => (
                <select
                  id={id}
                  value={pbxMode}
                  onChange={(e) => setPbxMode(e.target.value as "select" | "create")}
                >
                  <option value="select">Use an existing PBX</option>
                  {canCreatePbx && <option value="create">Register a new PBX</option>}
                </select>
              )}
            </Field>
            {pbxMode === "select" ? (
              <Field label="Existing PBX" required>
                {(id) => (
                  <select
                    id={id}
                    value={selectedPbxId}
                    onChange={(e) => { setSelectedPbxId(e.target.value); setPbx(null); }}
                  >
                    <option value="">Choose…</option>
                    {pbxs.map((row) => (
                      <option key={row.id} value={row.id}>
                        {row.name} ({row.host}:{row.port})
                      </option>
                    ))}
                  </select>
                )}
              </Field>
            ) : (
              <>
                <Field label="Name" required>
                  {(id) => (
                    <input id={id} value={newPbx.name}
                      onChange={(e) => setNewPbx({ ...newPbx, name: e.target.value })} />
                  )}
                </Field>
                <Field label="Type" required>
                  {(id) => (
                    <select
                      id={id}
                      value={newPbx.pbx_type}
                      onChange={(e) => setNewPbx({ ...newPbx, pbx_type: e.target.value as PbxType })}
                    >
                      {(Object.keys(PBX_TYPE_LABELS) as PbxType[]).map((type) => (
                        <option key={type} value={type}>{PBX_TYPE_LABELS[type]}</option>
                      ))}
                    </select>
                  )}
                </Field>
                <div className="field-row">
                  <Field label="Host" required>
                    {(id) => (
                      <input id={id} value={newPbx.host}
                        onChange={(e) => setNewPbx({ ...newPbx, host: e.target.value })} />
                    )}
                  </Field>
                  <Field label="Port" required>
                    {(id) => (
                      <input id={id} type="number" min={1} max={65535} value={newPbx.port}
                        onChange={(e) => setNewPbx({ ...newPbx, port: Number(e.target.value) })} />
                    )}
                  </Field>
                  <Field label="Transport">
                    {(id) => (
                      <select
                        id={id}
                        value={newPbx.transport}
                        onChange={(e) => setNewPbx({ ...newPbx, transport: e.target.value as SipTransport })}
                      >
                        <option value="UDP">UDP</option>
                        <option value="TCP">TCP</option>
                        <option value="TLS">TLS</option>
                      </select>
                    )}
                  </Field>
                </div>
              </>
            )}
          </>
        )}

        {step === 1 && (
          <>
            <Field label="Trunk name" required>
              {(id) => (
                <input id={id} value={trunkDraft.name}
                  onChange={(e) => setTrunkDraft({ ...trunkDraft, name: e.target.value })}
                  placeholder="Head Office Trunk" />
              )}
            </Field>
            <Field label="Direction">
              {(id) => (
                <select
                  id={id}
                  value={trunkDraft.direction}
                  onChange={(e) => setTrunkDraft({ ...trunkDraft, direction: e.target.value as TrunkDirection })}
                >
                  <option value="INBOUND">Inbound</option>
                  <option value="OUTBOUND">Outbound</option>
                  <option value="BIDIRECTIONAL">Bidirectional</option>
                </select>
              )}
            </Field>
            <div className="field-row">
              <Field label="SIP host" required>
                {(id) => (
                  <input id={id} value={trunkDraft.sip_host}
                    onChange={(e) => setTrunkDraft({ ...trunkDraft, sip_host: e.target.value })}
                    placeholder="192.168.0.113" />
                )}
              </Field>
              <Field label="Port" required>
                {(id) => (
                  <input id={id} type="number" min={1} max={65535} value={trunkDraft.port}
                    onChange={(e) => setTrunkDraft({ ...trunkDraft, port: Number(e.target.value) })} />
                )}
              </Field>
              <Field label="Transport">
                {(id) => (
                  <select
                    id={id}
                    value={trunkDraft.transport}
                    onChange={(e) => setTrunkDraft({ ...trunkDraft, transport: e.target.value as SipTransport })}
                  >
                    <option value="UDP">UDP</option>
                    <option value="TCP">TCP</option>
                    <option value="TLS">TLS</option>
                  </select>
                )}
              </Field>
            </div>
          </>
        )}

        {step === 2 && (
          <>
            <div className="field-row">
              <Field label="Auth username" hint="For LiveKit's digest challenge.">
                {(id) => (
                  <input id={id} autoComplete="off" value={trunkDraft.auth_username ?? ""}
                    onChange={(e) => setTrunkDraft({ ...trunkDraft, auth_username: e.target.value })} />
                )}
              </Field>
              <Field label="Auth password" hint="Stored encrypted; never shown again.">
                {(id) => (
                  <input id={id} type="password" autoComplete="new-password"
                    value={trunkDraft.auth_password ?? ""}
                    onChange={(e) => setTrunkDraft({ ...trunkDraft, auth_password: e.target.value })} />
                )}
              </Field>
            </div>
            <Field label="Allowed source IPs" hint="Comma-separated. Empty means no IP restriction.">
              {(id) => (
                <input id={id} value={allowedText} onChange={(e) => setAllowedText(e.target.value)}
                  placeholder="192.168.0.113, 10.0.0.0/24" />
              )}
            </Field>
          </>
        )}

        {step === 3 && (
          <>
            <Field label="Codecs" hint="Offer at least one the PBX will accept.">
              {() => (
                <div className="row" style={{ flexWrap: "wrap", gap: 12 }}>
                  {CODECS.map((codec) => (
                    <label key={codec} className="row" style={{ gap: 6 }}>
                      <input
                        type="checkbox"
                        checked={(trunkDraft.codecs ?? []).includes(codec)}
                        onChange={() => toggleCodec(codec)}
                      />
                      <span className="mono small">{codec}</span>
                    </label>
                  ))}
                </div>
              )}
            </Field>
            <Field label="DTMF">
              {(id) => (
                <select
                  id={id}
                  value={trunkDraft.dtmf_mode ?? ""}
                  onChange={(e) => setTrunkDraft({ ...trunkDraft, dtmf_mode: e.target.value || null })}
                >
                  <option value="">Provider default</option>
                  {DTMF_MODES.map((mode) => (
                    <option key={mode} value={mode}>{mode}</option>
                  ))}
                </select>
              )}
            </Field>
          </>
        )}

        {step === 4 && (
          <>
            <Field label="Media encryption">
              {(id) => (
                <label className="row" style={{ gap: 8 }}>
                  <input
                    id={id}
                    type="checkbox"
                    checked={trunkDraft.media_encryption_required ?? false}
                    onChange={(e) => setTrunkDraft({
                      ...trunkDraft,
                      media_encryption_required: e.target.checked,
                    })}
                  />
                  <span className="small">Require SRTP (media encryption)</span>
                </label>
              )}
            </Field>
            <Notice tone="info">
              Next creates the SIP trunk and syncs it to LiveKit. Phone numbers
              are still empty — they are assigned in step 7, then the trunk is
              synced again so LiveKit accepts the DID.
            </Notice>
          </>
        )}

        {step === 5 && (
          <>
            <p className="small muted">
              Test reachability before adding a number. A failed test can still
              continue — some PBXs refuse OPTIONS but accept INVITE.
            </p>
            <div className="row" style={{ gap: 8, marginBottom: 12 }}>
              <Button onClick={() => void runPbxTest()} disabled={busy || !currentPbx}>
                Test PBX
              </Button>
              <Button onClick={() => void runTrunkTest()} disabled={busy || !trunk}>
                Test trunk
              </Button>
            </div>
            {pbxTest && (
              <Notice tone={pbxTest.result === "PASSED" ? "ok" : "warn"}>
                PBX: {pbxTest.result.toLowerCase()} — {pbxTest.detail}
              </Notice>
            )}
            {trunkTest && (
              <Notice tone={trunkTest.result === "PASSED" ? "ok" : "warn"}>
                Trunk: {trunkTest.result === "PASSED" ? "passed" : trunkTest.result.toLowerCase()} — {trunkTest.detail}
              </Notice>
            )}
            {trunk && (
              <p className="small">
                LiveKit sync: <Badge tone={trunk.sync_status === "SYNCED" ? "ok" : "warn"}>{trunk.sync_status}</Badge>
              </p>
            )}
          </>
        )}

        {step === 6 && (
          <>
            <Field label="Number" required hint="E.164 or a PBX extension. Normalised on save.">
              {(id) => (
                <input id={id} className="mono" value={didNumber}
                  onChange={(e) => setDidNumber(e.target.value)}
                  placeholder="+8801700000000 or 1801" />
              )}
            </Field>
            <Field label="Label">
              {(id) => (
                <input id={id} value={didLabel} onChange={(e) => setDidLabel(e.target.value)} />
              )}
            </Field>
            <Notice tone="info">
              Next saves the DID and re-syncs the trunk so LiveKit's accepted
              numbers include it.
            </Notice>
          </>
        )}

        {step === 7 && (
          <>
            <Field label="Answered by" hint="Optional. The published agent that takes this DID.">
              {(id) => (
                <select id={id} value={agentId} onChange={(e) => setAgentId(e.target.value)}>
                  <option value="">Assign later</option>
                  {agents.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.name}
                      {a.published_version_number === null
                        ? " (unpublished)"
                        : ` (v${a.published_version_number})`}
                    </option>
                  ))}
                </select>
              )}
            </Field>
            {agents.length === 0 && (
              <Notice tone="warn">
                No agents yet. Create and publish one on the{" "}
                <Link href="/agents">Agents</Link> page, then return.
              </Notice>
            )}
          </>
        )}

        {step === 8 && (
          <>
            <Field
              label="Routing rule"
              hint="Optional pin. Without one, every matching rule is evaluated."
            >
              {(id) => (
                <select id={id} value={ruleId} onChange={(e) => setRuleId(e.target.value)}>
                  <option value="">Evaluate all matching rules</option>
                  {rules.map((r) => (
                    <option key={r.id} value={r.id}>{r.name}</option>
                  ))}
                </select>
              )}
            </Field>
            <Field label="Business hours">
              {(id) => (
                <select id={id} value={hoursId} onChange={(e) => setHoursId(e.target.value)}>
                  <option value="">None</option>
                  {hours.map((h) => (
                    <option key={h.id} value={h.id}>{h.name}</option>
                  ))}
                </select>
              )}
            </Field>
            {chosenRule && (
              <Notice tone="info">
                Fallback for {chosenRule.name}:{" "}
                {chosenRule.fallback_action
                  ? FALLBACK_ACTION_LABELS[chosenRule.fallback_action]
                  : "none."}{" "}
                Edit the chain on the <Link href="/routing">Routing</Link> page.
              </Notice>
            )}
          </>
        )}

        {step === 9 && (
          <>
            <p className="small" style={{ marginTop: 0 }}>
              This tenant can receive calls on the number below, provided the
              PBX routes it to LiveKit and an agent is published.
            </p>
            <dl className="kv">
              <dt>PBX</dt>
              <dd>{currentPbx?.name ?? "—"}</dd>
              <dt>Trunk</dt>
              <dd>
                {trunk?.name ?? "—"}{" "}
                {trunk && <Badge tone={trunk.sync_status === "SYNCED" ? "ok" : "warn"}>{trunk.sync_status}</Badge>}
              </dd>
              <dt>Number</dt>
              <dd className="mono">{did?.number ?? didNumber || "—"}</dd>
              <dt>Agent</dt>
              <dd>{agents.find((a) => a.id === (did?.inbound_agent_id ?? agentId))?.name ?? "none yet"}</dd>
              <dt>Routing</dt>
              <dd>{chosenRule?.name ?? "all matching rules"}</dd>
            </dl>
            <div className="row" style={{ gap: 8, marginTop: 16 }}>
              <Link className="btn btn-sm" href="/sip-trunks">SIP Trunks</Link>
              <Link className="btn btn-sm" href="/phone-numbers">Phone Numbers</Link>
              {did?.number && did.inbound_agent_id && (
                <Link className="btn btn-sm" href="/phone-numbers">Call Test</Link>
              )}
            </div>
          </>
        )}

        <div className="wizard-actions">
          <Button onClick={() => setStep((n) => Math.max(0, n - 1))} disabled={busy || step === 0}>
            Back
          </Button>
          {step < STEPS.length - 1 ? (
            <Button variant="primary" busy={busy} onClick={() => void goNext()} disabled={!canWrite}>
              {step === 4 ? "Create trunk and continue" : step === 6 ? "Save number and sync" : "Next"}
            </Button>
          ) : (
            <Link className="btn btn-primary" href="/phone-numbers">Done</Link>
          )}
        </div>
      </div>
    </Shell>
  );
}
