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

// --- Routing (spec 20, 37, 38) -------------------------------------------- //

export type FallbackAction = "SECONDARY_AGENT" | "PBX_QUEUE" | "VOICEMAIL" | "HANGUP";
export type DayOfWeek =
  | "MONDAY" | "TUESDAY" | "WEDNESDAY" | "THURSDAY" | "FRIDAY" | "SATURDAY" | "SUNDAY";

/** Every field is optional: an empty condition set matches every call. */
export interface RoutingConditions {
  pbx_id?: string | null;
  sip_trunk_id?: string | null;
  did?: string | null;
  caller_number?: string | null;
  caller_number_prefix?: string | null;
  destination_number?: string | null;
  campaign?: string | null;
}

export interface RoutingRule {
  id: string;
  name: string;
  description: string | null;
  /** Lower runs first. Ties are broken by creation order server-side. */
  priority: number;
  conditions: RoutingConditions;
  agent_id: string | null;
  business_hours_id: string | null;
  fallback_action: FallbackAction | null;
  fallback_agent_id: string | null;
  fallback_transfer_destination_id: string | null;
  closed_action: FallbackAction | null;
  closed_transfer_destination_id: string | null;
  status: ResourceStatus;
  agent_name: string | null;
  business_hours_name: string | null;
  created_at: string;
  updated_at: string;
}

export interface RoutingRuleInput {
  name: string;
  description?: string | null;
  priority?: number;
  conditions?: RoutingConditions;
  agent_id?: string | null;
  business_hours_id?: string | null;
  fallback_action?: FallbackAction | null;
  fallback_agent_id?: string | null;
  fallback_transfer_destination_id?: string | null;
  closed_action?: FallbackAction | null;
  closed_transfer_destination_id?: string | null;
}

export interface BusinessHoursInterval {
  id: string;
  day_of_week: DayOfWeek;
  /** "HH:MM:SS" in the schedule's own timezone. */
  opens_at: string;
  closes_at: string;
}

export interface Holiday {
  date: string;
  name?: string;
  closed?: boolean;
}

export interface BusinessHours {
  id: string;
  name: string;
  timezone: string | null;
  holidays: Holiday[];
  intervals: BusinessHoursInterval[];
  /** Null when the timezone is unknown — not the same as closed. */
  open_now: boolean | null;
  created_at: string;
  updated_at: string;
}

export interface BusinessHoursInput {
  name: string;
  timezone?: string | null;
  intervals?: { day_of_week: DayOfWeek; opens_at: string; closes_at: string }[];
  holidays?: Holiday[];
}

export type TransferDestinationKind =
  | "SIP_EXTENSION" | "PBX_EXTENSION" | "PBX_QUEUE" | "EXTERNAL_NUMBER" | "SIP_URI";

export interface TransferDestination {
  id: string;
  name: string;
  kind: TransferDestinationKind;
  target: string;
  pbx_id: string | null;
  sip_trunk_id: string | null;
  /** CR-1: whether the human agent hears the AI summary before bridging. */
  whisper_summary: boolean;
  ring_timeout_seconds: number;
  status: ResourceStatus;
  created_at: string;
  updated_at: string;
}

export interface TransferDestinationInput {
  name: string;
  kind: TransferDestinationKind;
  target: string;
  pbx_id?: string | null;
  sip_trunk_id?: string | null;
  whisper_summary?: boolean;
  ring_timeout_seconds?: number;
}

// --- Tools (spec 31, 32, 33) ---------------------------------------------- //

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
export type ToolAuthType =
  | "NONE" | "API_KEY_HEADER" | "BEARER_TOKEN" | "BASIC" | "OAUTH2_CLIENT_CREDENTIALS";

export interface Tool {
  id: string;
  name: string;
  description: string;
  http_method: HttpMethod;
  url_template: string;
  headers: Record<string, string>;
  request_schema: Record<string, unknown>;
  response_schema: Record<string, unknown>;
  auth_type: ToolAuthType;
  auth_header_name: string | null;
  timeout_seconds: number;
  max_retries: number;
  status: ResourceStatus;
  schema_valid: boolean;
  schema_validation_error: string | null;
  /** The secret itself is never returned (spec 26). */
  has_secret: boolean;
  /** {{placeholders}} found in the URL and headers. */
  variables: string[];
  created_at: string;
  updated_at: string;
}

export interface ToolInput {
  name: string;
  description: string;
  http_method?: HttpMethod;
  url_template: string;
  headers?: Record<string, string>;
  request_schema?: Record<string, unknown>;
  response_schema?: Record<string, unknown>;
  auth_type?: ToolAuthType;
  auth_header_name?: string | null;
  timeout_seconds?: number;
  max_retries?: number;
  /** Write-only. */
  auth_secret?: string | null;
}

// --- Knowledge bases (spec 34) -------------------------------------------- //

export type KnowledgeSourceType = "PDF" | "DOCX" | "TXT" | "CSV" | "WEB";
export type DocumentStatus = "PENDING" | "PROCESSING" | "INDEXED" | "FAILED";

export interface KnowledgeBase {
  id: string;
  name: string;
  description: string | null;
  embedding_provider_id: string | null;
  embedding_model_slug: string | null;
  embedding_dimensions: number;
  top_k: number;
  status: ResourceStatus;
  document_count: number;
  indexed_count: number;
  chunk_count: number;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeBaseInput {
  name: string;
  description?: string | null;
  embedding_model_slug?: string | null;
  embedding_dimensions?: number;
  top_k?: number;
}

export interface KnowledgeDocument {
  id: string;
  knowledge_base_id: string;
  title: string;
  source_type: KnowledgeSourceType;
  source_url: string | null;
  status: DocumentStatus;
  ingest_error: string | null;
  indexed_at: string | null;
  chunk_count: number;
  byte_size: number | null;
  created_at: string;
  updated_at: string;
}

// --- Users and settings (spec 8, 47) -------------------------------------- //

export type TenantRole = "TENANT_ADMIN" | "MANAGER" | "AGENT_MANAGER" | "ANALYST" | "VIEWER";

export interface ConsoleUser {
  id: string;
  email: string;
  full_name: string;
  is_active: boolean;
  last_login_at: string | null;
  roles: string[];
  sessions_revoked: boolean;
  created_at: string;
  updated_at: string;
}

export interface ConsoleUserInput {
  email: string;
  full_name: string;
  password: string;
  role: TenantRole;
}

export type TenantStatus = "ACTIVE" | "SUSPENDED" | "ARCHIVED";

export interface TenantSettings {
  id: string;
  name: string;
  slug: string;
  status: TenantStatus;
  timezone: string;
  default_language: string;
  /** Read-only here: the platform sets the limits (spec 47). */
  max_concurrent_calls: number | null;
  max_daily_calls: number | null;
  max_monthly_minutes: number | null;
  notes: string | null;
}

export interface TenantSettingsInput {
  name?: string;
  timezone?: string;
  default_language?: string;
  notes?: string | null;
}

// --- Analytics, usage, recordings (spec 39, 47, 57) ----------------------- //

export interface Analytics {
  window_days: number;
  total_calls: number;
  answered_calls: number;
  failed_calls: number;
  transferred_calls: number;
  total_seconds: number;
  average_duration_seconds: number | null;
  answer_rate: number | null;
  by_state: Record<string, number>;
  by_agent: { agent: string; calls: number; seconds: number }[];
  by_day: { date: string; calls: number; answered: number; failed: number }[];
  latency_note: string;
}

export interface UsageDay {
  usage_date: string;
  call_count: number;
  answered_count: number;
  failed_count: number;
  transferred_count: number;
  total_seconds: number;
  ai_seconds: number;
}

export interface UsageSummary {
  max_concurrent_calls: number | null;
  max_daily_calls: number | null;
  max_monthly_minutes: number | null;
  calls_today: number;
  minutes_this_month: number;
  active_calls: number;
  /** Reported honestly: only the concurrency limit is enforced today. */
  daily_limit_enforced: boolean;
  monthly_limit_enforced: boolean;
  concurrent_limit_enforced: boolean;
  days: UsageDay[];
}

export interface Recording {
  id: string;
  call_id: string;
  bucket: string;
  object_key: string;
  content_type: string | null;
  byte_size: number | null;
  duration_seconds: number | null;
  livekit_egress_id: string | null;
  started_at: string | null;
  ended_at: string | null;
  delete_after: string | null;
  created_at: string;
  updated_at: string;
}

// --- Platform console (spec 60) ------------------------------------------- //

export interface TenantSummary {
  id: string;
  name: string;
  slug: string;
  status: TenantStatus;
  timezone: string;
  default_language: string;
  max_concurrent_calls: number | null;
  max_daily_calls: number | null;
  max_monthly_minutes: number | null;
  notes: string | null;
  user_count: number;
  agent_count: number;
  pbx_count: number;
  phone_number_count: number;
  calls_last_30_days: number;
  active_calls: number;
  admin_email: string | null;
  created_at: string;
  updated_at: string;
}

export interface TenantInput {
  name: string;
  slug: string;
  timezone?: string;
  default_language?: string;
  max_concurrent_calls?: number | null;
  max_daily_calls?: number | null;
  max_monthly_minutes?: number | null;
  notes?: string | null;
  admin_email?: string | null;
  admin_full_name?: string | null;
  admin_password?: string | null;
}

export type ProviderKind = "STT" | "LLM" | "TTS";

export interface Provider {
  id: string;
  kind: ProviderKind;
  slug: string;
  display_name: string;
  status: ResourceStatus;
  supports_streaming: boolean;
  default_base_url: string | null;
  /** False for a self-hosted provider reached over the LAN (spec 25). */
  requires_credential: boolean;
  notes: string | null;
  model_count: number;
  voice_count: number;
  credential_count: number;
  created_at: string;
  updated_at: string;
}

export interface ProviderInput {
  kind: ProviderKind;
  slug: string;
  display_name: string;
  supports_streaming?: boolean;
  default_base_url?: string | null;
  requires_credential?: boolean;
  notes?: string | null;
}

export interface CatalogModel {
  id: string;
  provider_id: string;
  provider_slug: string | null;
  provider_kind: ProviderKind | null;
  slug: string;
  display_name: string;
  languages: string[];
  status: ResourceStatus;
  is_default: boolean;
  created_at: string;
  updated_at: string;
}

export interface CatalogModelInput {
  provider_id: string;
  slug: string;
  display_name: string;
  languages?: string[];
  is_default?: boolean;
}

export interface CatalogVoice {
  id: string;
  provider_id: string;
  provider_slug: string | null;
  voice_id: string;
  name: string;
  language: string | null;
  accent: string | null;
  description: string | null;
  status: ResourceStatus;
  is_default: boolean;
  sample_object_key: string | null;
  created_at: string;
  updated_at: string;
}

export interface CatalogVoiceInput {
  provider_id: string;
  voice_id: string;
  name: string;
  language?: string | null;
  accent?: string | null;
  description?: string | null;
  is_default?: boolean;
}

export interface AuditEntry {
  id: string;
  occurred_at: string;
  tenant_id: string | null;
  user_id: string | null;
  user_email: string | null;
  action: string;
  resource_type: string;
  resource_id: string | null;
  old_value: Record<string, unknown> | null;
  new_value: Record<string, unknown> | null;
  ip_address: string | null;
  request_id: string | null;
  tenant_name: string | null;
}

export interface ComponentHealth {
  name: string;
  reachable: boolean;
  latency_ms: number | null;
  detail: string | null;
}

export interface Capacity {
  tenant_count: number;
  active_tenant_count: number;
  agent_count: number;
  published_agent_count: number;
  phone_number_count: number;
  sip_trunk_count: number;
  active_calls: number;
  calls_last_24h: number;
  /** Null when a tenant is uncapped: the sum would be a floor, not a ceiling. */
  licensed_concurrent_calls: number | null;
  livekit_rooms: number | null;
  components: ComponentHealth[];
}

export interface DriftedResource {
  kind: string;
  id: string;
  tenant_id: string | null;
  tenant_name: string | null;
  name: string;
  sync_status: SyncStatus;
  livekit_resource_id: string | null;
  sync_error: string | null;
  sync_attempts: number;
  last_synced_at: string | null;
}

export interface LiveKitOverview {
  url: string;
  sip_uri: string;
  reachable: boolean;
  room_count: number | null;
  detail: string | null;
  trunk_sync: Record<string, number>;
  dispatch_rule_sync: Record<string, number>;
  /** Empty on a healthy platform, which is the point of the list. */
  needs_attention: DriftedResource[];
}

export type PlatformRole = "SUPER_ADMIN" | "PLATFORM_OPERATOR";

export interface PlatformUser {
  id: string;
  email: string;
  full_name: string;
  is_active: boolean;
  last_login_at: string | null;
  roles: string[];
  sessions_revoked: boolean;
  created_at: string;
  updated_at: string;
}

export interface PlatformUserInput {
  email: string;
  full_name: string;
  password: string;
  role: PlatformRole;
}

export interface SettingEntry {
  name: string;
  /** Null for a secret: only whether it is set leaves the process. */
  value: string | null;
  is_secret: boolean;
  is_set: boolean;
  note: string | null;
}

export interface PlatformSettings {
  environment: string;
  editable: boolean;
  source: string;
  groups: Record<string, SettingEntry[]>;
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
  routingRules: {
    list(limit = 100, offset = 0): Promise<Page<RoutingRule>> {
      return request<Page<RoutingRule>>(`/routing-rules?limit=${limit}&offset=${offset}`);
    },
    create(input: RoutingRuleInput): Promise<RoutingRule> {
      return request<RoutingRule>("/routing-rules", { method: "POST", body: input });
    },
    update(
      id: string,
      input: Partial<RoutingRuleInput> & { status?: ResourceStatus },
    ): Promise<RoutingRule> {
      return request<RoutingRule>(`/routing-rules/${id}`, { method: "PUT", body: input });
    },
    remove(id: string): Promise<void> {
      return request<void>(`/routing-rules/${id}`, { method: "DELETE" });
    },
  },

  businessHours: {
    list(limit = 100, offset = 0): Promise<Page<BusinessHours>> {
      return request<Page<BusinessHours>>(`/business-hours?limit=${limit}&offset=${offset}`);
    },
    create(input: BusinessHoursInput): Promise<BusinessHours> {
      return request<BusinessHours>("/business-hours", { method: "POST", body: input });
    },
    update(id: string, input: Partial<BusinessHoursInput>): Promise<BusinessHours> {
      return request<BusinessHours>(`/business-hours/${id}`, { method: "PUT", body: input });
    },
    remove(id: string): Promise<void> {
      return request<void>(`/business-hours/${id}`, { method: "DELETE" });
    },
  },

  transferDestinations: {
    list(limit = 100, offset = 0): Promise<Page<TransferDestination>> {
      return request<Page<TransferDestination>>(
        `/transfer-destinations?limit=${limit}&offset=${offset}`,
      );
    },
    create(input: TransferDestinationInput): Promise<TransferDestination> {
      return request<TransferDestination>("/transfer-destinations", {
        method: "POST",
        body: input,
      });
    },
    update(
      id: string,
      input: Partial<TransferDestinationInput> & { status?: ResourceStatus },
    ): Promise<TransferDestination> {
      return request<TransferDestination>(`/transfer-destinations/${id}`, {
        method: "PUT",
        body: input,
      });
    },
    remove(id: string): Promise<void> {
      return request<void>(`/transfer-destinations/${id}`, { method: "DELETE" });
    },
  },

  tools: {
    list(limit = 100, offset = 0): Promise<Page<Tool>> {
      return request<Page<Tool>>(`/tools?limit=${limit}&offset=${offset}`);
    },
    create(input: ToolInput): Promise<Tool> {
      return request<Tool>("/tools", { method: "POST", body: input });
    },
    update(id: string, input: Partial<ToolInput> & { status?: ResourceStatus }): Promise<Tool> {
      return request<Tool>(`/tools/${id}`, { method: "PUT", body: input });
    },
    remove(id: string): Promise<void> {
      return request<void>(`/tools/${id}`, { method: "DELETE" });
    },
  },

  knowledgeBases: {
    list(limit = 100, offset = 0): Promise<Page<KnowledgeBase>> {
      return request<Page<KnowledgeBase>>(`/knowledge-bases?limit=${limit}&offset=${offset}`);
    },
    create(input: KnowledgeBaseInput): Promise<KnowledgeBase> {
      return request<KnowledgeBase>("/knowledge-bases", { method: "POST", body: input });
    },
    update(
      id: string,
      input: Partial<KnowledgeBaseInput> & { status?: ResourceStatus },
    ): Promise<KnowledgeBase> {
      return request<KnowledgeBase>(`/knowledge-bases/${id}`, { method: "PUT", body: input });
    },
    remove(id: string): Promise<void> {
      return request<void>(`/knowledge-bases/${id}`, { method: "DELETE" });
    },
    documents(id: string): Promise<Page<KnowledgeDocument>> {
      return request<Page<KnowledgeDocument>>(`/knowledge-bases/${id}/documents`);
    },
  },

  users: {
    list(limit = 100, offset = 0): Promise<Page<ConsoleUser>> {
      return request<Page<ConsoleUser>>(`/users?limit=${limit}&offset=${offset}`);
    },
    create(input: ConsoleUserInput): Promise<ConsoleUser> {
      return request<ConsoleUser>("/users", { method: "POST", body: input });
    },
    update(
      id: string,
      input: { full_name?: string; role?: TenantRole; is_active?: boolean },
    ): Promise<ConsoleUser> {
      return request<ConsoleUser>(`/users/${id}`, { method: "PUT", body: input });
    },
    setPassword(id: string, password: string): Promise<ConsoleUser> {
      return request<ConsoleUser>(`/users/${id}/password`, {
        method: "POST",
        body: { password },
      });
    },
    remove(id: string): Promise<void> {
      return request<void>(`/users/${id}`, { method: "DELETE" });
    },
  },

  settings: {
    get(): Promise<TenantSettings> {
      return request<TenantSettings>("/settings");
    },
    update(input: TenantSettingsInput): Promise<TenantSettings> {
      return request<TenantSettings>("/settings", { method: "PUT", body: input });
    },
  },

  analytics(days = 30): Promise<Analytics> {
    return request<Analytics>(`/analytics?days=${days}`);
  },

  usage(days = 30): Promise<UsageSummary> {
    return request<UsageSummary>(`/usage?days=${days}`);
  },

  recordings(limit = 50, offset = 0): Promise<Page<Recording>> {
    return request<Page<Recording>>(`/recordings?limit=${limit}&offset=${offset}`);
  },

  platform: {
    tenants: {
      list(q?: string, limit = 50, offset = 0): Promise<Page<TenantSummary>> {
        const query = new URLSearchParams({ limit: String(limit), offset: String(offset) });
        if (q) query.set("q", q);
        return request<Page<TenantSummary>>(`/platform/tenants?${query.toString()}`);
      },
      get(id: string): Promise<TenantSummary> {
        return request<TenantSummary>(`/platform/tenants/${id}`);
      },
      create(input: TenantInput): Promise<TenantSummary> {
        return request<TenantSummary>("/platform/tenants", { method: "POST", body: input });
      },
      update(
        id: string,
        input: Partial<TenantInput> & { status?: TenantStatus },
      ): Promise<TenantSummary> {
        return request<TenantSummary>(`/platform/tenants/${id}`, { method: "PUT", body: input });
      },
    },
    providers: {
      list(kind?: ProviderKind): Promise<Page<Provider>> {
        return request<Page<Provider>>(`/platform/providers${kind ? `?kind=${kind}` : ""}`);
      },
      create(input: ProviderInput): Promise<Provider> {
        return request<Provider>("/platform/providers", { method: "POST", body: input });
      },
      update(
        id: string,
        input: Partial<Omit<ProviderInput, "kind" | "slug">> & { status?: ResourceStatus },
      ): Promise<Provider> {
        return request<Provider>(`/platform/providers/${id}`, { method: "PUT", body: input });
      },
    },
    models: {
      list(providerId?: string): Promise<Page<CatalogModel>> {
        return request<Page<CatalogModel>>(
          `/platform/models${providerId ? `?provider_id=${providerId}` : ""}`,
        );
      },
      create(input: CatalogModelInput): Promise<CatalogModel> {
        return request<CatalogModel>("/platform/models", { method: "POST", body: input });
      },
      update(
        id: string,
        input: {
          display_name?: string;
          languages?: string[];
          status?: ResourceStatus;
          is_default?: boolean;
        },
      ): Promise<CatalogModel> {
        return request<CatalogModel>(`/platform/models/${id}`, { method: "PUT", body: input });
      },
      remove(id: string): Promise<void> {
        return request<void>(`/platform/models/${id}`, { method: "DELETE" });
      },
    },
    voices: {
      list(providerId?: string): Promise<Page<CatalogVoice>> {
        return request<Page<CatalogVoice>>(
          `/platform/voices${providerId ? `?provider_id=${providerId}` : ""}`,
        );
      },
      create(input: CatalogVoiceInput): Promise<CatalogVoice> {
        return request<CatalogVoice>("/platform/voices", { method: "POST", body: input });
      },
      update(
        id: string,
        input: Partial<Omit<CatalogVoiceInput, "provider_id" | "voice_id">> & {
          status?: ResourceStatus;
        },
      ): Promise<CatalogVoice> {
        return request<CatalogVoice>(`/platform/voices/${id}`, { method: "PUT", body: input });
      },
    },
    auditLogs(params: {
      /** Named ``for_tenant`` server-side: a client may never send tenant_id. */
      forTenant?: string;
      action?: string;
      userEmail?: string;
      days?: number;
      limit?: number;
      offset?: number;
    } = {}): Promise<Page<AuditEntry>> {
      const query = new URLSearchParams({
        days: String(params.days ?? 30),
        limit: String(params.limit ?? 50),
        offset: String(params.offset ?? 0),
      });
      if (params.forTenant) query.set("for_tenant", params.forTenant);
      if (params.action) query.set("action", params.action);
      if (params.userEmail) query.set("user_email", params.userEmail);
      return request<Page<AuditEntry>>(`/platform/audit-logs?${query.toString()}`);
    },
    capacity(): Promise<Capacity> {
      return request<Capacity>("/platform/capacity");
    },
    livekit(): Promise<LiveKitOverview> {
      return request<LiveKitOverview>("/platform/livekit");
    },
    users: {
      list(): Promise<Page<PlatformUser>> {
        return request<Page<PlatformUser>>("/platform/users");
      },
      create(input: PlatformUserInput): Promise<PlatformUser> {
        return request<PlatformUser>("/platform/users", { method: "POST", body: input });
      },
      update(
        id: string,
        input: {
          full_name?: string;
          role?: PlatformRole;
          is_active?: boolean;
          password?: string;
        },
      ): Promise<PlatformUser> {
        return request<PlatformUser>(`/platform/users/${id}`, { method: "PUT", body: input });
      },
    },
    settings(): Promise<PlatformSettings> {
      return request<PlatformSettings>("/platform/settings");
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

export const FALLBACK_ACTION_LABELS: Record<FallbackAction, string> = {
  SECONDARY_AGENT: "Hand to another agent",
  PBX_QUEUE: "Send to a PBX queue",
  VOICEMAIL: "Send to voicemail",
  HANGUP: "Hang up",
};

export const TRANSFER_KIND_LABELS: Record<TransferDestinationKind, string> = {
  SIP_EXTENSION: "SIP extension",
  PBX_EXTENSION: "PBX extension",
  PBX_QUEUE: "PBX queue",
  EXTERNAL_NUMBER: "External number",
  SIP_URI: "SIP URI",
};

export const TOOL_AUTH_LABELS: Record<ToolAuthType, string> = {
  NONE: "No authentication",
  API_KEY_HEADER: "API key in a header",
  BEARER_TOKEN: "Bearer token",
  BASIC: "HTTP basic",
  OAUTH2_CLIENT_CREDENTIALS: "OAuth2 client credentials",
};

export const TENANT_ROLE_LABELS: Record<TenantRole, string> = {
  TENANT_ADMIN: "Tenant administrator",
  MANAGER: "Manager",
  AGENT_MANAGER: "Agent manager",
  ANALYST: "Analyst",
  VIEWER: "Viewer",
};

export const DAY_LABELS: Record<DayOfWeek, string> = {
  MONDAY: "Monday",
  TUESDAY: "Tuesday",
  WEDNESDAY: "Wednesday",
  THURSDAY: "Thursday",
  FRIDAY: "Friday",
  SATURDAY: "Saturday",
  SUNDAY: "Sunday",
};

/** Monday-first, matching how a week is read rather than the enum's order. */
export const DAY_ORDER: DayOfWeek[] = [
  "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY",
];

export const PLATFORM_ROLE_LABELS: Record<PlatformRole, string> = {
  SUPER_ADMIN: "Super administrator",
  PLATFORM_OPERATOR: "Platform operator",
};
