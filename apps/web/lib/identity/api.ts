/**
 * Calling the identity API from the browser: same-origin through app/v1/[...path], with the
 * session's CSRF token on every write (IDENTITY.md §4.3), and every refusal turned into an
 * IdentityError the UI can act on — open the step-up sheet, name the missing permission —
 * instead of a generic failure.
 */
import { CSRF_HEADER, ME_PATH } from "./contract";
import { isPermission, type Permission } from "./permissions.gen";
import { apiWrite, isAborted, stepUpNotConfirmedSentence, type WriteMethod } from "../api-write";
import { currentMirror, liveMirror } from "./mirror";
import { sendToEnrolment } from "./enrolment";
import { humanise, needsWords, permissionName } from "./words";
import { refusalCode } from "./refusal-code";
import { detailText } from "../api-error";

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
    // A seat refusal names who holds the seat and where seats are changed: its own sentence.
    super(code === "seat_required" && typeof extra.body?.message === "string" && extra.body.message
      ? extra.body.message
      : (status === 422 && fieldSentence(extra.body?.detail)) || errorSentence(status, code, extra.permission, extra.body ?? {}));
    this.name = "IdentityError";
    this.status = status;
    this.code = code;
    this.permission = typeof extra.permission === "string" && isPermission(extra.permission) ? extra.permission : null;
    this.attemptsLeft = typeof extra.attempts_left === "number" ? extra.attempts_left : null;
    this.retryAfter = extra.retryAfter ?? null;
    this.body = extra.body ?? {};
    // An internal error's `detail` is the exception class for the log reader (X-73): never shown.
    this.detail = typeof this.body.detail === "string" && typeof this.body.error === "string" && this.body.error !== "internal_error" ? this.body.detail : null;
  }
  get stepUp(): boolean { return this.status === 401 && this.code === "step_up_required"; }
}

/** A rejected form names its field and why: "Note: at most 20000 characters", never only
 *  "Check the fields". A 422 whose body is FastAPI's validation list carries both. */
export function fieldSentence(detail: unknown): string | null {
  if (!Array.isArray(detail) || !detail.length) return null;
  const first = detail[0] as { loc?: unknown[]; msg?: unknown };
  const loc = Array.isArray(first?.loc) ? first.loc.filter((x) => x !== "body") : [];
  const field = String(loc[loc.length - 1] ?? "").replace(/_/g, " ");
  const msg = String(first?.msg ?? "").replace(/^String should have /, "").replace(/^Value error, /, "");
  if (!field && !msg) return null;
  const name = field ? field.charAt(0).toUpperCase() + field.slice(1) : "A field";
  return msg ? `${name}: ${msg}` : `${name} is not valid`;
}

type Body = Record<string, unknown>;
const strOf = (v: unknown): string => (typeof v === "string" ? v : "");
const listOf = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : strOf(v) ? [strOf(v)] : []);
function namesOf(perms: string[]): string {
  const names = perms.map((p) => `“${permissionName(p)}”`);
  return names.length <= 1 ? (names[0] ?? "") : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/**
 * THE WORDS FOR EVERY REFUSAL THE IDENTITY API GIVES, by code (engine identity/grants.py
 * GrantRefused, identity/authn.py AuthnError, routers/identity_agents.py _err). Each reads the
 * refusal's own body where it names something (the project, the permissions, the repo), so the
 * person learns what to change, never only that it failed. A code with no entry here still gets
 * the API's sentence or its humanised code (errorSentence). Enforced by
 * tests/every-identity-refusal-has-words.test.ts.
 */
export const REFUSAL_WORDS: Readonly<Record<string, (body: Body) => string>> = {
  step_up_required: () => "Confirm it's you",
  step_up_cancelled: () => "Not confirmed — press it again and confirm it's you",
  unauthenticated: () => "Sign in again",
  csrf_failed: () => "Security check failed. Try again.",
  invalid_credentials: () => "Wrong email or password",
  account_disabled: () => "This account is disabled",
  ambiguous_credentials: () => "Sign out, then sign in once",
  last_owner: () => "Add another Owner first",
  identity_unavailable: () => "Identity is unavailable",
  invalid_code: () => "That code didn't work",
  code_invalid: () => "That code didn't work",
  policy_refused: () => "Not allowed by policy",
  invalid_policy: () => "Check the fields",
  invalid_request: () => "Check the fields",
  unknown_fields: () => "Check the fields",
  confirmation_required: () => "Confirm critical changes",
  consent_required: () => "Needs your consent",
  no_second_factor: () => "Add a second factor first",
  mfa_enrolment_required: () => "Add a second factor first",
  enrolment_expired: () => "That key expired — start again",
  enrolment_grant_required: () => "Ask an admin to reset your second factor",
  task_grants_end_with_their_run: () => "Ends with its run",
  captain_authority_has_no_expiry: () => "This authority has no end date. Revoke it to end it.",
  revoked_is_terminal: () => "Already ended",
  too_long: (b) => (typeof b.max_days === "number" ? `At most ${b.max_days} days` : "Longer than policy allows"),
  expired: () => "Pick an expiry in the future",
  role_exists: () => "That key is taken",
  system_passport: () => "BOSS manages this agent",
  people_only: () => "Only a person signed in to the console can do this",
  person_inactive: () => "Your account isn't active",
  not_found: () => "Not found",
  no_agents: () => "Pick at least one agent",
  no_projects: () => "Pick at least one project",
  empty_permissions: () => "Pick at least one ability",
  unknown_permission: (b) => (strOf(b.permission) ? `BOSS has no permission “${strOf(b.permission)}”` : "Unknown permission"),
  not_delegable: (b) => (strOf(b.permission) ? `${namesOf([strOf(b.permission)])} can't be given to an agent` : "That can't be given to an agent"),
  narrowing_only: (b) => (strOf(b.permission) ? `A grant only narrows: it can't add ${namesOf([strOf(b.permission)])}` : "A grant only narrows"),
  bad_max_depth: () => "Choose a depth the policy allows",
  children_without_subdelegation: () => "Allow sub-delegation to name child agents",
  bad_child_name: () => "Check the child agent's name",
  unknown_agent: (b) => (strOf(b.agent) ? `${strOf(b.agent)} isn't an agent here` : "That agent isn't here"),
  agent_inactive: (b) => (strOf(b.agent) ? `${strOf(b.agent)} is paused or revoked` : "That agent is paused or revoked"),
  exceeds_ceiling: (b) => {
    const perms = listOf(b.permissions);
    return perms.length ? `No agent picked may ever hold ${namesOf(perms)}` : "Beyond what the agent may ever hold";
  },
  exceeds_your_access: (b) => {
    const perms = listOf(b.permissions);
    const where = strOf(b.project_name) || strOf(b.project);
    return perms.length
      ? `You don't hold ${namesOf(perms)}${where ? ` in ${where}` : ""}, so you can't let an agent do it. Leave it out, or ask an Admin.`
      : `That is more than you hold${where ? ` in ${where}` : ""}`;
  },
  bad_resources: () => "Check the repositories and their access",
  bad_repos: () => "Check the repositories: each is owner/name",
  bad_github_permission: (b) => (strOf(b.key) ? `GitHub access “${strOf(b.key)}” isn't one BOSS grants` : "Check the GitHub access"),
  github_access_lacking: (b) => (strOf(b.repo)
    ? `Your own GitHub account can't push to ${strOf(b.repo)}, so you can't let an agent do it. Connect your GitHub in My account › Agents, then try again.`
    : "Connect your GitHub first"),
  public_origin_required: () => "Set BOSS's public address first",
  run_token_required: () => "Only a running agent can do this",
  unknown_provider: () => "BOSS doesn't know that provider",
  vault_error: () => "The vault couldn't do that",
  // A 500 names its exception class in `detail` for the log reader; a person reads this instead.
  internal_error: (b) => `BOSS hit an error it didn't expect${strOf(b.request_id) ? ` (request ${strOf(b.request_id)}, in BOSS's API log)` : ""}. Try again in a moment.`,
  rate_limited: () => "Too many tries",
};

/** The words for a refusal. Short where they can be: they land in toasts and inline lines. */
export function errorSentence(status: number, code: string, permission?: unknown, body: Body = {}): string {
  if (code === "forbidden") return typeof permission === "string" ? needsWords(permission) : "Not allowed";
  const words = REFUSAL_WORDS[code];
  if (words) return words({ ...(typeof permission === "string" && !("permission" in body) ? { permission } : {}), ...body });
  // The API's own sentence, when it wrote one (refusalCode takes a bare `detail` as the code).
  if (/\s/.test(code)) return code;
  if (strOf(body.detail) && /\s/.test(strOf(body.detail))) return strOf(body.detail);
  // A refusal said as an object ({headline, action}, {message}): its words, never "Check the fields".
  const said = body.detail && typeof body.detail === "object" && !Array.isArray(body.detail) ? detailText(body.detail) : "";
  if (said && /\s/.test(said) && !said.startsWith("{")) return said;
  if (strOf(body.message)) return strOf(body.message);
  if (status === 429) return "Too many tries";
  if (status === 404) return "Not found";
  if (status === 409 && (!code || code === "error")) return "Changed meanwhile — reload";
  if (status === 422 && (!code || code === "error")) return "Check the fields";
  // An unworded code still says which refusal it was, never only that one happened.
  if (code && code !== "error") return humanise(code);
  return status ? `Refused (HTTP ${status})` : "No answer from BOSS";
}

/**
 * THE ONE FALLBACK for an identity screen's error line: an IdentityError's sentence (above), the
 * error's own text, and only with neither the bare words. Every screen's local `said()` ends here,
 * so no screen ever says only "That didn't work" when the API gave a reason
 * (tests/every-identity-refusal-has-words.test.ts keeps the literal in this function alone).
 */
export type RefusalFix = { label: string; href?: string; command?: string };

/** The next step a refusal names (`fix` in the body: a console place, or a command for the BOSS
 *  host), so an error line carries its fix action (X-73). */
export function refusalFix(e: unknown): RefusalFix | null {
  if (!(e instanceof IdentityError)) return null;
  const f = e.body.fix;
  if (!f || typeof f !== "object") return null;
  const r = f as Record<string, unknown>;
  const label = strOf(r.label);
  if (!label) return null;
  return { label, ...(strOf(r.href) ? { href: strOf(r.href) } : {}), ...(strOf(r.command) ? { command: strOf(r.command) } : {}) };
}

export function refusalSentence(e: unknown): string {
  // The API's own sentence when it wrote one (a bare code in `detail` is never shown).
  if (e instanceof IdentityError) return e.detail && /\s/.test(e.detail) ? e.detail : e.message;
  const why = e instanceof Error && e.message ? e.message : "";
  return `That didn't work: ${why || "no answer"}`;
}

let csrfToken = "";
/** Set by IdentityProvider from /v1/identity/me; sent on every write. */
export function setCsrfToken(token: string): void { csrfToken = token; }
export function currentCsrfToken(): string { return csrfToken; }

const WRITE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/**
 * THE TOKEN FOLLOWS THE SESSION. The CSRF token is an HMAC of the session id, and the API rotates
 * the session on step-up, on enrolling a passkey and on confirming an authenticator (S1) without
 * sending a new token. A write refused `403 csrf_failed` therefore re-reads the token from
 * /v1/identity/me (same-origin only) and is sent once more — the refusal happened before the API
 * did anything, so the retry is the first time it runs.
 */
async function refreshCsrf(): Promise<void> {
  try {
    const res = await fetch(ME_PATH, { headers: { Accept: "application/json" }, credentials: "same-origin", cache: "no-store" });
    if (!res.ok) return;
    const token = String(((await res.json()) as { csrf_token?: unknown }).csrf_token ?? "");
    if (token) csrfToken = token;
  } catch { /* the retry is skipped: the refusal stands */ }
}

/** The reason the step-up sheet gives for an identity write sent inside useGuardedCall. Read
 *  SYNCHRONOUSLY when idFetch is called (a guarded call's write is sent in the same tick). */
let guardedReason: string | null = null;
/** Run `call` with `reason` as the step-up sheet's words for the identity writes it sends. */
export function withStepUpReason<T>(reason: string, call: () => Promise<T>): Promise<T> {
  const was = guardedReason;
  guardedReason = reason;
  try { return call(); } finally { guardedReason = was; }
}

/**
 * EVERY IDENTITY WRITE GETS "CONFIRM IT'S YOU", never a silent refusal. A write the API refuses
 * `401 step_up_required` (a step-up permission, a second factor over ten minutes old) opens the
 * one step-up sheet of the provider on the page and is sent once more when the person confirms;
 * cancelled, or no press started it (lib/user-gesture.ts), it is refused as `step_up_cancelled`
 * with words. Before this, idFetch told the one write path "the caller handles it" for every
 * call, so a write no caller wrapped in useGuardedCall (Add a project's Enter on an idea,
 * 2026-11-08) came back as a bare refusal and the screen just waited. `background` writes (an
 * effect, a timer) opt out and keep the refusal.
 */
export async function idFetch<T>(method: string, path: string, body?: unknown, init: { signal?: AbortSignal; background?: boolean } = {}): Promise<T> {
  const reason = guardedReason;
  try {
    return await idFetchFresh<T>(method, path, body, init);
  } catch (e) {
    if (!(e instanceof IdentityError && e.stepUp && WRITE.has(method) && !init.background)) throw e;
    const mirror = currentMirror() ?? liveMirror();
    if (!mirror) throw e;  // no console on the page to ask through: the caller's own handling
    if (await mirror.stepUp(reason ?? "Confirm it's you to continue.")) return idFetchFresh<T>(method, path, body, init);
    throw new IdentityError(401, "step_up_cancelled", { body: { error: "step_up_cancelled", detail: stepUpNotConfirmedSentence() } });
  }
}

async function idFetchFresh<T>(method: string, path: string, body?: unknown, init: { signal?: AbortSignal } = {}): Promise<T> {
  // COMPARED WITH WHAT WAS SENT, not with what is held now. After a step-up the provider re-reads
  // /me on its own (the sheet closing, the tab coming back), so by the time the refusal arrives the
  // held token is often already the new one; comparing the fresh token with it said "unchanged"
  // and the first press after "Confirm it's you" showed "Reload and retry" (BOSSAPP-897).
  const sent = csrfToken;
  try {
    return await idFetchOnce<T>(method, path, body, init);
  } catch (e) {
    if (e instanceof IdentityError && e.status === 403 && e.code === "csrf_failed" && WRITE.has(method)) {
      await refreshCsrf();
      if (csrfToken && csrfToken !== sent) return idFetchOnce<T>(method, path, body, init);
    }
    throw e;
  }
}

async function idFetchOnce<T>(method: string, path: string, body?: unknown, init: { signal?: AbortSignal } = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (WRITE.has(method) && csrfToken) headers[CSRF_HEADER] = csrfToken;
  let res: Response;
  try {
    const payload = body === undefined ? undefined : JSON.stringify(body);
    // A write is one press (lib/api-write.ts): an Idempotency-Key, and a double click sends once.
    res = WRITE.has(method)
      // idFetch answers step_up_required for identity writes (with useGuardedCall's reason).
      ? await apiWrite(path, { method: method as WriteMethod, headers, body: payload, credentials: "same-origin", cache: "no-store", signal: init.signal }, { noStepUp: true })
      : await fetch(path, { headers, credentials: "same-origin", cache: "no-store", signal: init.signal });
  } catch (error) {
    // An abandoned request (the caller unmounted, or asked again) is not "identity unavailable".
    if (isAborted(error) || init.signal?.aborted) throw error;
    throw new IdentityError(0, "identity_unavailable");
  }
  if (res.status === 204) return undefined as T;
  let data: unknown = null;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok) {
    const d = (data ?? {}) as { error?: unknown; detail?: unknown; permission?: unknown; attempts_left?: unknown };
    const code = refusalCode(res.status, d);
    const retry = Number(res.headers.get("retry-after"));
    const body = data && typeof data === "object" && !Array.isArray(data) ? data as Record<string, unknown> : {};
    // An enrolment-only session: the one thing to do is enrol (lib/identity/enrolment.ts).
    if (res.status === 403) sendToEnrolment(code);
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
