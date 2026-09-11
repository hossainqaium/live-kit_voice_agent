/**
 * Configuration API client.
 *
 * Two rules this module exists to enforce structurally:
 *
 *  1. **No call site can send a tenant ID.** There is no parameter for one
 *     anywhere below. Tenant identity comes from the session server-side
 *     (spec 7), and the API rejects a client-supplied one outright — so
 *     leaving it out of the client means the mistake is not expressible.
 *  2. **Permission checks here are cosmetic.** The backend enforces RBAC
 *     independently (spec 8). Hiding a button prevents a confusing 403; it is
 *     not access control.
 */

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8200";

export const API_V1 = `${API_BASE_URL}/api/v1`;

/**
 * Where tokens live.
 *
 * localStorage is a deliberate development-stage choice, and its tradeoff is
 * real: anything that achieves script execution on this origin can read it.
 * The alternative — httpOnly, SameSite cookies with a CSRF token — needs
 * backend cookie support and is the right answer before production. Recorded
 * as Phase 7 work rather than left implicit.
 */
const ACCESS_KEY = "va.access_token";
const REFRESH_KEY = "va.refresh_token";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly requestId: string | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** A 403 means authenticated but not permitted — worth wording differently. */
  get isForbidden(): boolean {
    return this.status === 403;
  }

  get isUnauthenticated(): boolean {
    return this.status === 401;
  }
}

export const tokens = {
  access(): string | null {
    if (typeof window === "undefined") return null;
    try {
      return window.localStorage.getItem(ACCESS_KEY);
    } catch {
      // Storage can throw in a private window or with site data blocked.
      return null;
    }
  },
  refresh(): string | null {
    if (typeof window === "undefined") return null;
    try {
      return window.localStorage.getItem(REFRESH_KEY);
    } catch {
      return null;
    }
  },
  set(access: string, refresh: string): void {
    try {
      window.localStorage.setItem(ACCESS_KEY, access);
      window.localStorage.setItem(REFRESH_KEY, refresh);
    } catch {
      // Non-fatal: the session simply will not survive a reload.
    }
  },
  clear(): void {
    try {
      window.localStorage.removeItem(ACCESS_KEY);
      window.localStorage.removeItem(REFRESH_KEY);
    } catch {
      /* nothing to do */
    }
  },
};

type RequestOptions = Omit<RequestInit, "body"> & { body?: unknown };

/** Set by the auth provider so a failed refresh can return to the login screen. */
let onAuthLost: (() => void) | null = null;

export function setAuthLostHandler(handler: (() => void) | null): void {
  onAuthLost = handler;
}

async function parseError(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as {
      detail?: string | { msg?: string; loc?: string[] }[];
    };
    if (typeof payload.detail === "string") return payload.detail;
    if (Array.isArray(payload.detail)) {
      // FastAPI validation errors: name the field, or the message is useless.
      return payload.detail
        .map((item) => {
          const field = item.loc?.filter((p) => p !== "body").join(".");
          return field ? `${field}: ${item.msg ?? "invalid"}` : (item.msg ?? "invalid");
        })
        .join("; ");
    }
  } catch {
    /* fall through to the status text */
  }
  return response.statusText || `request failed with status ${response.status}`;
}

let refreshInFlight: Promise<boolean> | null = null;

/**
 * Exchange the refresh token for a new pair.
 *
 * Shared across concurrent callers: a page that fires several requests at once
 * would otherwise refresh several times, and each rotation invalidates the
 * last, logging the user out.
 */
async function refreshTokens(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight;

  const refresh = tokens.refresh();
  if (!refresh) return false;

  refreshInFlight = (async () => {
    try {
      const response = await fetch(`${API_V1}/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refresh }),
      });
      if (!response.ok) return false;
      const pair = (await response.json()) as TokenResponse;
      tokens.set(pair.access_token, pair.refresh_token);
      return true;
    } catch {
      return false;
    } finally {
      refreshInFlight = null;
    }
  })();

  return refreshInFlight;
}

async function request<T>(
  path: string,
  options: RequestOptions = {},
  { retry = true }: { retry?: boolean } = {},
): Promise<T> {
  const { body, headers, ...rest } = options;
  const access = tokens.access();

  const response = await fetch(`${API_V1}${path}`, {
    ...rest,
    headers: {
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      ...(access ? { Authorization: `Bearer ${access}` } : {}),
      ...headers,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  // Echoed by the API's correlation middleware. Surfacing it makes a bug
  // report traceable to the exact server-side request.
  const requestId = response.headers.get("X-Request-ID");

  if (response.status === 401 && retry && tokens.refresh()) {
    // An expired access token is the common case and should be invisible.
    if (await refreshTokens()) {
      return request<T>(path, options, { retry: false });
    }
    tokens.clear();
    onAuthLost?.();
    throw new ApiError("your session has expired; sign in again", 401, requestId);
  }

  if (!response.ok) {
    throw new ApiError(await parseError(response), response.status, requestId);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

// --------------------------------------------------------------------------- //
// Types mirroring the API schemas
// --------------------------------------------------------------------------- //

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

export interface Principal {
  user_id: string;
  email: string;
  tenant_id: string | null;
  is_platform_user: boolean;
  roles: string[];
  permissions: string[];
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export type PbxType =
  | "ASTERISK"
  | "FREEPBX"
  | "FREESWITCH"
  | "FUSIONPBX"
  | "KAMAILIO"
  | "THREE_CX"
  | "CISCO"
  | "MITEL"
  | "OTHER_SIP";

export type SipTransport = "UDP" | "TCP" | "TLS";
export type ResourceStatus = "ACTIVE" | "DISABLED" | "ARCHIVED";
export type ConnectionTestResult = "UNTESTED" | "PASSED" | "FAILED";

export interface Pbx {
  id: string;
  name: string;
  pbx_type: PbxType;
  host: string;
  port: number;
  transport: SipTransport;
  status: ResourceStatus;
  description: string | null;
  last_test_result: ConnectionTestResult;
  last_tested_at: string | null;
  last_test_detail: string | null;
  created_at: string;
  updated_at: string;
}

export interface PbxInput {
  name: string;
  pbx_type: PbxType;
  host: string;
  port: number;
  transport: SipTransport;
  description?: string | null;
}

export interface PbxTestOutcome {
  result: ConnectionTestResult;
  detail: string;
  tested_at: string;
  latency_ms: number | null;
}


// --- SIP trunks (spec 15) -------------------------------------------------- //

export type TrunkDirection = "INBOUND" | "OUTBOUND" | "BIDIRECTIONAL";
export type SyncStatus = "SYNCED" | "PENDING" | "FAILED" | "DRIFTED";

export interface SipTrunk {
  id: string;
  name: string;
  pbx_id: string | null;
  direction: TrunkDirection;
  sip_host: string;
  port: number;
  transport: SipTransport;
  allowed_ips: string[];
  auth_username: string | null;
  media_encryption_required: boolean;
  codecs: string[];
  dtmf_mode: string | null;
  status: ResourceStatus;
  has_credential: boolean;
  /** Derived from the DIDs assigned to this trunk, not stored on it. */
  accepted_numbers: string[];
  livekit_resource_id: string | null;
  sync_status: SyncStatus;
  last_synced_at: string | null;
  sync_error: string | null;
  sync_attempts: number;
  last_test_result: ConnectionTestResult;
  last_tested_at: string | null;
  last_test_detail: string | null;
  created_at: string;
  updated_at: string;
}

export interface SipTrunkInput {
  name: string;
  pbx_id?: string | null;
  direction: TrunkDirection;
  sip_host: string;
  port: number;
  transport: SipTransport;
  allowed_ips?: string[];
  auth_username?: string | null;
  auth_password?: string | null;
  media_encryption_required?: boolean;
  codecs?: string[];
  dtmf_mode?: string | null;
}

// --- Phone numbers (spec 17) ---------------------------------------------- //

export interface PhoneNumber {
  id: string;
  number: string;
  label: string | null;
  pbx_id: string | null;
  sip_trunk_id: string | null;
  inbound_agent_id: string | null;
  routing_rule_id: string | null;
  business_hours_id: string | null;
  status: ResourceStatus;
  pbx_name: string | null;
  sip_trunk_name: string | null;
  agent_name: string | null;
  created_at: string;
  updated_at: string;
}

export interface PhoneNumberInput {
  number: string;
  label?: string | null;
  pbx_id?: string | null;
  sip_trunk_id?: string | null;
  inbound_agent_id?: string | null;
}

// --- Agents (spec 18, 19) ------------------------------------------------- //

export type AgentVersionState = "DRAFT" | "TESTING" | "PUBLISHED" | "ARCHIVED";

export interface Agent {
  id: string;
  name: string;
  description: string | null;
  status: ResourceStatus;
  published_version_id: string | null;
  version_count: number;
  published_version_number: number | null;
  latest_draft_version_number: number | null;
  created_at: string;
  updated_at: string;
}

export interface AgentVersion {
  id: string;
  agent_id: string;
  version_number: number;
  state: AgentVersionState;
  language: string;
  greeting: string | null;
  system_prompt: string;
  stt_provider_id: string | null;
  stt_model_id: string | null;
  llm_provider_id: string | null;
  llm_model_id: string | null;
  tts_provider_id: string | null;
  tts_model_id: string | null;
  voice_id: string | null;
  temperature: number | null;
  interruption_enabled: boolean;
  interruption_min_words: number;
  silence_timeout_seconds: number | null;
  max_call_duration_seconds: number | null;
  recording_enabled: boolean;
  transcription_enabled: boolean;
  transfer_enabled: boolean;
  transfer_announcement_text: string | null;
  transfer_summary_template: string | null;
  transfer_summary_max_seconds: number;
  transfer_skip_dtmf: string | null;
  knowledge_base_id: string | null;
  published_at: string | null;
  change_note: string | null;
  validation_errors: ValidationIssue[];
  stt_label: string | null;
  llm_label: string | null;
  tts_label: string | null;
  voice_label: string | null;
  created_at: string;
  updated_at: string;
}

export interface ValidationIssue {
  field: string;
  message: string;
  severity: string;
}

export interface ValidationReport {
  publishable: boolean;
  issues: ValidationIssue[];
  dependencies: ValidationIssue[];
}

// --- Calls (spec 41) ------------------------------------------------------ //

export type CallState =
  | "NEW" | "RINGING" | "ANSWERED" | "AI_CONNECTED" | "IN_PROGRESS"
  | "TRANSFERRING" | "HUMAN_AGENT" | "COMPLETED"
  | "FAILED" | "TIMEOUT" | "CANCELLED" | "BUSY" | "NO_ANSWER";

export interface Call {
  id: string;
  call_id: string;
  agent_id: string | null;
  agent_version_id: string | null;
  did: string | null;
  room_id: string | null;
  caller_number: string | null;
  destination_number: string | null;
  direction: string;
  start_time: string | null;
  answer_time: string | null;
  end_time: string | null;
  duration_seconds: number | null;
  state: CallState;
  hangup_reason: string | null;
  failure_detail: string | null;
  transfer_status: string;
  agent_name: string | null;
  agent_version_number: number | null;
  has_transcript: boolean;
  has_recording: boolean;
}

export interface TranscriptSegment {
  sequence: number;
  speaker: "CALLER" | "AI" | "HUMAN_AGENT";
  spoken_at: string;
  text: string;
  confidence: number | null;
  offset_seconds: number | null;
  is_private_to_agent: boolean;
}

export interface CallEvent {
  event_type: string;
  occurred_at: string;
  from_state: CallState | null;
  to_state: CallState | null;
  payload: Record<string, unknown>;
}

export interface CallDetail extends Call {
  segments: TranscriptSegment[];
  events: CallEvent[];
  summary: string | null;
}

export interface HealthResponse {
  status: string;
  service: string;
  environment: string;
}

// --------------------------------------------------------------------------- //
// Endpoints
// --------------------------------------------------------------------------- //

export const api = {
  async login(email: string, password: string): Promise<TokenResponse> {
    // Deliberately not via request(): there is no token yet, and a 401 here
    // means bad credentials rather than an expired session, so the refresh
    // path must not run.
    const response = await fetch(`${API_V1}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    if (!response.ok) {
      throw new ApiError(
        await parseError(response),
        response.status,
        response.headers.get("X-Request-ID"),
      );
    }
    return (await response.json()) as TokenResponse;
  },

  me(): Promise<Principal> {
    return request<Principal>("/auth/me");
  },

  pbxs: {
    list(limit = 50, offset = 0): Promise<Page<Pbx>> {
      return request<Page<Pbx>>(`/pbxs?limit=${limit}&offset=${offset}`);
    },
    get(id: string): Promise<Pbx> {
      return request<Pbx>(`/pbxs/${id}`);
    },
    create(input: PbxInput): Promise<Pbx> {
      return request<Pbx>("/pbxs", { method: "POST", body: input });
    },
    update(id: string, input: Partial<PbxInput> & { status?: ResourceStatus }): Promise<Pbx> {
      return request<Pbx>(`/pbxs/${id}`, { method: "PUT", body: input });
    },
    remove(id: string): Promise<void> {
      return request<void>(`/pbxs/${id}`, { method: "DELETE" });
    },
    test(id: string): Promise<PbxTestOutcome> {
      return request<PbxTestOutcome>(`/pbxs/${id}/test`, { method: "POST" });
    },
    enable(id: string): Promise<Pbx> {
      return request<Pbx>(`/pbxs/${id}/enable`, { method: "POST" });
    },
    disable(id: string): Promise<Pbx> {
      return request<Pbx>(`/pbxs/${id}/disable`, { method: "POST" });
    },
  },

  sipTrunks: {
    list(limit = 100, offset = 0): Promise<Page<SipTrunk>> {
      return request<Page<SipTrunk>>(`/sip-trunks?limit=${limit}&offset=${offset}`);
    },
    create(input: SipTrunkInput): Promise<SipTrunk> {
      return request<SipTrunk>("/sip-trunks", { method: "POST", body: input });
    },
    update(id: string, input: Partial<SipTrunkInput>): Promise<SipTrunk> {
      return request<SipTrunk>(`/sip-trunks/${id}`, { method: "PUT", body: input });
    },
    remove(id: string): Promise<void> {
      return request<void>(`/sip-trunks/${id}`, { method: "DELETE" });
    },
    test(id: string): Promise<PbxTestOutcome> {
      return request<PbxTestOutcome>(`/sip-trunks/${id}/test`, { method: "POST" });
    },
    sync(id: string): Promise<SipTrunk> {
      return request<SipTrunk>(`/sip-trunks/${id}/sync`, { method: "POST" });
    },
    enable(id: string): Promise<SipTrunk> {
      return request<SipTrunk>(`/sip-trunks/${id}/enable`, { method: "POST" });
    },
    disable(id: string): Promise<SipTrunk> {
      return request<SipTrunk>(`/sip-trunks/${id}/disable`, { method: "POST" });
    },
  },

  phoneNumbers: {
    list(limit = 100, offset = 0): Promise<Page<PhoneNumber>> {
      return request<Page<PhoneNumber>>(`/phone-numbers?limit=${limit}&offset=${offset}`);
    },
    create(input: PhoneNumberInput): Promise<PhoneNumber> {
      return request<PhoneNumber>("/phone-numbers", { method: "POST", body: input });
    },
    update(id: string, input: Partial<PhoneNumberInput>): Promise<PhoneNumber> {
      return request<PhoneNumber>(`/phone-numbers/${id}`, { method: "PUT", body: input });
    },
    remove(id: string): Promise<void> {
      return request<void>(`/phone-numbers/${id}`, { method: "DELETE" });
    },
    enable(id: string): Promise<PhoneNumber> {
      return request<PhoneNumber>(`/phone-numbers/${id}/enable`, { method: "POST" });
    },
    disable(id: string): Promise<PhoneNumber> {
      return request<PhoneNumber>(`/phone-numbers/${id}/disable`, { method: "POST" });
    },
  },

  agents: {
    list(limit = 100, offset = 0): Promise<Page<Agent>> {
      return request<Page<Agent>>(`/agents?limit=${limit}&offset=${offset}`);
    },
    get(id: string): Promise<Agent> {
      return request<Agent>(`/agents/${id}`);
    },
    create(input: { name: string; description?: string | null }): Promise<Agent> {
      return request<Agent>("/agents", { method: "POST", body: input });
    },
    update(id: string, input: { name?: string; description?: string | null }): Promise<Agent> {
      return request<Agent>(`/agents/${id}`, { method: "PUT", body: input });
    },
    remove(id: string): Promise<void> {
      return request<void>(`/agents/${id}`, { method: "DELETE" });
    },
    versions(id: string): Promise<AgentVersion[]> {
      return request<AgentVersion[]>(`/agents/${id}/versions`);
    },
    saveDraft(id: string, config: Partial<AgentVersion>): Promise<AgentVersion> {
      return request<AgentVersion>(`/agents/${id}/draft`, { method: "PUT", body: config });
    },
    validate(id: string, versionNumber: number): Promise<ValidationReport> {
      return request<ValidationReport>(`/agents/${id}/versions/${versionNumber}/validate`);
    },
    publish(id: string, changeNote?: string): Promise<AgentVersion> {
      return request<AgentVersion>(`/agents/${id}/publish`, {
        method: "POST",
        body: { change_note: changeNote ?? null },
      });
    },
    rollback(id: string, versionNumber: number): Promise<AgentVersion> {
      return request<AgentVersion>(`/agents/${id}/rollback`, {
        method: "POST",
        body: { version_number: versionNumber },
      });
    },
  },

  calls: {
    list(limit = 50, offset = 0, state?: string): Promise<Page<Call>> {
      const query = new URLSearchParams({ limit: String(limit), offset: String(offset) });
      if (state) query.set("state", state);
      return request<Page<Call>>(`/calls?${query.toString()}`);
    },
    get(id: string): Promise<CallDetail> {
      return request<CallDetail>(`/calls/${id}`);
    },
  },
};


/** Liveness of the API, for the status indicator. Needs no token. */
export async function fetchApiHealth(): Promise<HealthResponse> {
  const response = await fetch(`${API_BASE_URL}/health`, { cache: "no-store" });
  if (!response.ok) throw new ApiError("health check failed", response.status);
  return (await response.json()) as HealthResponse;
}

/** Human-readable labels. The API returns enum values; these are for display. */
export const PBX_TYPE_LABELS: Record<PbxType, string> = {
  ASTERISK: "Asterisk",
  FREEPBX: "FreePBX",
  FREESWITCH: "FreeSWITCH",
  FUSIONPBX: "FusionPBX",
  KAMAILIO: "Kamailio",
  THREE_CX: "3CX",
  CISCO: "Cisco",
  MITEL: "Mitel",
  OTHER_SIP: "Other SIP-compatible",
};
