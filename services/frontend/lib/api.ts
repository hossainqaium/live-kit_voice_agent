/**
 * Configuration API client.
 *
 * Two rules this module exists to enforce:
 *
 *  1. The browser never sends a tenant ID. Tenant identity is derived
 *     server-side from the authenticated session (spec 7), so no call site can
 *     pass one even by accident.
 *  2. Permission checks in the UI are cosmetic. The backend enforces RBAC
 *     independently (spec 8); hiding a control is never the access control.
 */

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export const API_V1 = `${API_BASE_URL}/api/v1`;

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly requestId: string | null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

type RequestOptions = Omit<RequestInit, "body"> & { body?: unknown };

export async function apiFetch<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const { body, headers, ...rest } = options;

  const response = await fetch(`${API_V1}${path}`, {
    ...rest,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...headers,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  // Echoed by the API's correlation middleware; quoting it in a bug report is
  // what makes the server-side trace findable.
  const requestId = response.headers.get("X-Request-ID");

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload.detail) detail = payload.detail;
    } catch {
      // Non-JSON error body; the status text stands on its own.
    }
    throw new ApiError(detail, response.status, requestId);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export interface HealthResponse {
  status: string;
  service: string;
  environment: string;
}

export async function fetchApiHealth(): Promise<HealthResponse> {
  const response = await fetch(`${API_BASE_URL}/health`, {
    cache: "no-store",
  });
  if (!response.ok) {
    throw new ApiError("health check failed", response.status, null);
  }
  return (await response.json()) as HealthResponse;
}
