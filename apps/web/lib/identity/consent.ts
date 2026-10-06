/**
 * JUST-IN-TIME CONSENT, as the API asks for it (IDENTITY.md §7.3 2; engine identity/consent.py).
 *
 * Starting work a person's standing grants do not cover answers `409` with this body, flat (the
 * API's install_consent_handler):
 *
 *   { error: "consent_required", scope?: "workflow_run" | "schedule", task, task_run_id,
 *     agents: [{id, name, role_key}], permissions, projects,
 *     github: {repo, repos, permissions: {contents, pull_requests, …}, summary} | null,
 *     durations: [{kind: "task"|"standing", label, days}], max_days }
 *
 * The console shows the consent sheet, creates the grant the person picks with
 * POST /v1/identity/me/delegations, and re-sends the ORIGINAL start request unchanged: a task
 * grant's `pending:` run id is claimed by the new run, a standing grant simply covers it.
 * Pure functions here; the sheet (components/identity/consent-sheet.tsx) is the UI.
 */
export type ConsentDuration = { kind: "task" | "standing"; label: string; days: number | null };

export type ConsentGithub = {
  repo: string;
  repos: string[];
  /** GitHub App permission levels: {contents: "write", pull_requests: "write", …}. */
  permissions: Record<string, string>;
  summary: string;
};

export type ConsentRequest = {
  /** "workflow_run" | "schedule" for a workflow or a schedule; absent for a build. */
  scope: string | null;
  /** What the work is ("Interview Scorecard"). */
  task: string;
  /** The task grant's run id (`pending:…`), null for a schedule (standing grants only). */
  task_run_id: string | null;
  agents: { id: string; name: string; role_key: string | null }[];
  permissions: string[];
  projects: string[];
  github: ConsentGithub | null;
  durations: ConsentDuration[];
  max_days: number | null;
  /** Called with the new grant id, or null when the person cancels. */
  onDecided?: (grantId: string | null) => void;
};

export type GrantCreateBody = {
  agents: string[];
  permissions: string[];
  projects: string[];
  kind: "task" | "standing";
  task_run_id?: string;
  days?: number;
  github?: Record<string, unknown>;
  purpose: string;
};

const str = (v: unknown): string => (typeof v === "string" ? v : "");
const strs = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);

/** The sheet's request from a 409 body, or null when the body is not a consent request. */
export function consentFromBody(raw: unknown): ConsentRequest | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const b = raw as Record<string, unknown>;
  // A body wrapped by a proxy ({detail: {...}} or {consent: {...}}) is unwrapped once.
  if (b.error !== "consent_required") {
    for (const k of ["consent", "detail"]) {
      const inner = b[k];
      if (inner && typeof inner === "object" && (inner as Record<string, unknown>).error === "consent_required") return consentFromBody(inner);
    }
    return null;
  }
  const agents = (Array.isArray(b.agents) ? b.agents : []).flatMap((a) => {
    if (!a || typeof a !== "object") return [];
    const x = a as Record<string, unknown>;
    const id = str(x.id);
    return id ? [{ id, name: str(x.name) || id, role_key: str(x.role_key ?? x.roleKey) || null }] : [];
  });
  const gh = b.github && typeof b.github === "object" ? b.github as Record<string, unknown> : null;
  const github: ConsentGithub | null = gh ? {
    repo: str(gh.repo),
    repos: strs(gh.repos).length ? strs(gh.repos) : (str(gh.repo) ? [str(gh.repo)] : []),
    permissions: Object.fromEntries(Object.entries(gh.permissions && typeof gh.permissions === "object" ? gh.permissions as Record<string, unknown> : {})
      .filter(([, v]) => typeof v === "string")) as Record<string, string>,
    summary: str(gh.summary),
  } : null;
  const durations = (Array.isArray(b.durations) ? b.durations : []).flatMap((d): ConsentDuration[] => {
    if (!d || typeof d !== "object") return [];
    const x = d as Record<string, unknown>;
    if (x.kind !== "task" && x.kind !== "standing") return [];
    return [{ kind: x.kind, label: str(x.label) || (x.kind === "task" ? "For this run only" : "Standing"), days: typeof x.days === "number" ? x.days : null }];
  });
  const taskRunId = str(b.task_run_id) || null;
  return {
    scope: str(b.scope) || null,
    task: str(b.task) || "this work",
    task_run_id: taskRunId,
    agents,
    permissions: strs(b.permissions),
    projects: strs(b.projects),
    github,
    // A task grant needs a run id to be claimed by; without one, only the standing choice works.
    durations: durations.filter((d) => d.kind === "standing" || taskRunId),
    max_days: typeof b.max_days === "number" ? b.max_days : null,
  };
}

/** From an IdentityError (lib/identity/api.ts keeps the refusal's body). */
export function consentFromError(e: unknown): ConsentRequest | null {
  if (!e || typeof e !== "object") return null;
  const x = e as { status?: unknown; code?: unknown; body?: unknown };
  if (x.status !== 409 || x.code !== "consent_required") return null;
  return consentFromBody(x.body);
}

/**
 * The grant the person picked, as POST /v1/identity/me/delegations takes it. Permissions are
 * sent as asked (the API already kept only delegable ones; dropping one would leave the grant
 * short and the retry would 409 again). The GitHub scope is `{repos, ...levels}` — never the
 * 409's own object, whose `repo`/`summary` keys the API refuses.
 */
export function grantBodyFor(request: ConsentRequest, duration: ConsentDuration): GrantCreateBody {
  const body: GrantCreateBody = {
    agents: request.agents.map((a) => a.id),
    permissions: [...request.permissions],
    projects: [...request.projects],
    kind: duration.kind,
    purpose: request.task,
  };
  if (duration.kind === "task" && request.task_run_id) body.task_run_id = request.task_run_id;
  if (duration.kind === "standing" && typeof duration.days === "number") body.days = duration.days;
  if (request.github && request.github.repos.length) body.github = { repos: [...request.github.repos], ...request.github.permissions };
  return body;
}

/** Browser only: open the sheet and resolve with the grant id, or null when cancelled. */
export function askConsent(request: ConsentRequest): Promise<string | null> {
  if (typeof window === "undefined") return Promise.resolve(null);
  return new Promise((resolve) => {
    window.dispatchEvent(new CustomEvent<ConsentRequest>(CONSENT_EVENT, { detail: { ...request, onDecided: resolve } }));
  });
}

export const CONSENT_EVENT = "boss:consent-required";

/**
 * Run a start; when the API answers 409 consent_required, ask the person and, once they grant,
 * re-send the same start unchanged — once. Cancelling rethrows the original refusal.
 */
export async function withConsent<T>(start: () => Promise<T>): Promise<T> {
  try {
    return await start();
  } catch (e) {
    const request = consentFromError(e);
    if (!request) throw e;
    const granted = await askConsent(request);
    if (!granted) throw e;
    return start();
  }
}
