/**
 * Calling the identity API from the browser: same-origin through app/v1/[...path], with the
 * session's CSRF token on every write (IDENTITY.md §4.3), and every refusal turned into an
 * IdentityError the UI can act on — open the step-up sheet, name the missing permission —
 * instead of a generic failure.
 */
import { CSRF_HEADER } from "./contract";
import { isPermission, type Permission } from "./permissions.gen";

export type IdentityErrorCode =
  | "unauthenticated" | "step_up_required" | "forbidden" | "csrf_failed" | "not_found" | "invalid_credentials"
  | "invalid_code" | "identity_unavailable" | "rate_limited" | string;

export class IdentityError extends Error {
  readonly status: number;
  readonly code: IdentityErrorCode;
  readonly permission: Permission | null;
  readonly attemptsLeft: number | null;
  readonly retryAfter: number | null;
  constructor(status: number, code: IdentityErrorCode, extra: { permission?: unknown; attempts_left?: unknown; retryAfter?: number | null } = {}) {
    super(errorSentence(status, code, extra.permission));
    this.name = "IdentityError";
    this.status = status;
    this.code = code;
    this.permission = typeof extra.permission === "string" && isPermission(extra.permission) ? extra.permission : null;
    this.attemptsLeft = typeof extra.attempts_left === "number" ? extra.attempts_left : null;
    this.retryAfter = extra.retryAfter ?? null;
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
  if (code === "invalid_code") return "That code didn't work";
  if (code === "last_owner") return "Add another Owner first";
  if (code === "identity_unavailable") return "Identity is unavailable";
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
    res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin", cache: "no-store", signal: init.signal });
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
    throw new IdentityError(res.status, code, { permission: d.permission, attempts_left: d.attempts_left, retryAfter: Number.isFinite(retry) && retry > 0 ? retry : null });
  }
  return data as T;
}

export const idGet = <T>(path: string, init?: { signal?: AbortSignal }) => idFetch<T>("GET", path, undefined, init);
export const idPost = <T>(path: string, body: unknown = {}) => idFetch<T>("POST", path, body);
export const idPatch = <T>(path: string, body: unknown) => idFetch<T>("PATCH", path, body);
export const idPut = <T>(path: string, body: unknown) => idFetch<T>("PUT", path, body);
export const idDelete = <T>(path: string) => idFetch<T>("DELETE", path);

export function enc(value: string): string { return encodeURIComponent(value); }
