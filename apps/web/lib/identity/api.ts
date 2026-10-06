/**
 * Calling the identity API from the browser: same-origin through app/v1/[...path], with the
 * session's CSRF token on every write (IDENTITY.md §4.3), and every refusal turned into an
 * IdentityError the UI can act on — open the step-up sheet, name the missing permission —
 * instead of a generic failure.
 */
import { CSRF_HEADER } from "./contract";
import { isPermission, type Permission } from "./permissions.gen";
import { apiWrite, type WriteMethod } from "../api-write";

export type IdentityErrorCode =
  | "unauthenticated" | "step_up_required" | "forbidden" | "csrf_failed" | "not_found" | "invalid_credentials"
  | "invalid_code" | "identity_unavailable" | "rate_limited" | string;

export class IdentityError extends Error {
  readonly status: number;
  readonly code: IdentityErrorCode;
  readonly permission: Permission | null;
  readonly attemptsLeft: number | null;
  readonly retryAfter: number | null;
  /** The API's own sentence for this refusal (`detail`), when it gave one. */
  readonly detail: string | null;
  /** The whole refusal body as the API sent it (e.g. a 409 consent_required's fields). */
  readonly body: Record<string, unknown>;
  constructor(status: number, code: IdentityErrorCode, extra: { permission?: unknown; attempts_left?: unknown; retryAfter?: number | null; body?: Record<string, unknown> } = {}) {
    super(errorSentence(status, code, extra.permission));
    this.name = "IdentityError";
    this.status = status;
    this.code = code;
    this.permission = typeof extra.permission === "string" && isPermission(extra.permission) ? extra.permission : null;
    this.attemptsLeft = typeof extra.attempts_left === "number" ? extra.attempts_left : null;
    this.retryAfter = extra.retryAfter ?? null;
    this.body = extra.body ?? {};
    this.detail = typeof this.body.detail === "string" && typeof this.body.error === "string" ? this.body.detail : null;
  }
  get stepUp(): boolean { return this.status === 401 && this.code === "step_up_required"; }
}

/** The words for a refusal. Short: they land in toasts (≤ 4 words) and inline lines. */
export function errorSentence(status: number, code: string, permission?: unknown): string {
  if (code === "step_up_required") return "Confirm it's you";
  if (code === "forbidden") return typeof permission === "string" ? `Needs ${permission}` : "Not allowed";
  if (code === "unauthenticated") return "Sign in again";
  if (code === "csrf_failed") return "Reload and retry";
  if (code === "invalid_credentials") return "Wrong email or password";
  if (code === "last_owner") return "Add another Owner first";
  if (code === "identity_unavailable") return "Identity is unavailable";
  if (code === "invalid_code" || code === "code_invalid") return "That code didn't work";
  if (code === "policy_refused") return "Not allowed by policy";
  if (code === "invalid_policy") return "Check the fields";
  if (code === "confirmation_required") return "Confirm critical changes";
  if (code === "consent_required") return "Needs your consent";
  if (code === "no_second_factor") return "Add a second factor first";
  if (code === "task_grants_end_with_their_run") return "Ends with its run";
  if (code === "revoked_is_terminal") return "Already ended";
  if (code === "too_long") return "Longer than policy allows";
  if (code === "role_exists") return "That key is taken";
  if (status === 429) return "Too many tries";
  if (status === 404) return "Not found";
  if (status === 409) return "Changed meanwhile — reload";
  if (status === 422) return "Check the fields";
  return "That didn't work";
}

let csrfToken = "";
/** Set by IdentityProvider from /v1/identity/me; sent on every write. */
export function setCsrfToken(token: string): void { csrfToken = token; }

const WRITE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export async function idFetch<T>(method: string, path: string, body?: unknown, init: { signal?: AbortSignal } = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (WRITE.has(method) && csrfToken) headers[CSRF_HEADER] = csrfToken;
  let res: Response;
  try {
    const payload = body === undefined ? undefined : JSON.stringify(body);
    // A write is one press (lib/api-write.ts): an Idempotency-Key, and a double click sends once.
    res = WRITE.has(method)
      ? await apiWrite(path, { method: method as WriteMethod, headers, body: payload, credentials: "same-origin", cache: "no-store", signal: init.signal })
      : await fetch(path, { headers, credentials: "same-origin", cache: "no-store", signal: init.signal });
  } catch {
    throw new IdentityError(0, "identity_unavailable");
  }
  if (res.status === 204) return undefined as T;
  let data: unknown = null;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok) {
    const d = (data ?? {}) as { error?: unknown; detail?: unknown; permission?: unknown; attempts_left?: unknown };
    const code = typeof d.error === "string" ? d.error : typeof d.detail === "string" ? d.detail : res.status === 429 ? "rate_limited" : "error";
    const retry = Number(res.headers.get("retry-after"));
    const body = data && typeof data === "object" && !Array.isArray(data) ? data as Record<string, unknown> : {};
    throw new IdentityError(res.status, code, { permission: d.permission, attempts_left: d.attempts_left, retryAfter: Number.isFinite(retry) && retry > 0 ? retry : null, body });
  }
  return data as T;
}

export const idGet = <T>(path: string, init?: { signal?: AbortSignal }) => idFetch<T>("GET", path, undefined, init);
export const idPost = <T>(path: string, body: unknown = {}) => idFetch<T>("POST", path, body);
export const idPatch = <T>(path: string, body: unknown) => idFetch<T>("PATCH", path, body);
export const idPut = <T>(path: string, body: unknown) => idFetch<T>("PUT", path, body);
export const idDelete = <T>(path: string) => idFetch<T>("DELETE", path);

export function enc(value: string): string { return encodeURIComponent(value); }
