/**
 * Build L: resolve a deep-linked object to ITS OWN project BEFORE anything renders.
 *
 * The reported bug: opening `?area=code_tasks&build=crun_…` (or a meeting, run, decision,
 * self-fix, escalation or thread link) while a DIFFERENT project is selected rendered the panel
 * against the selected project — the wrong one — and then flipped to "Can't reach BOSS" or
 * "This panel isn't here any more" when the object's own feeds 404'd under that project.
 *
 * The fix is resolve-before-fetch: read the object id out of the URL, ask the control plane
 * `GET /v1/console/resolve-owner` which project the object actually belongs to, switch to that
 * project and stamp `project_id=` onto the URL so every later poll keeps it. The server does
 * this before its fan-out (app/page.tsx) and the shell re-stamps the URL on the client and keeps
 * it through navigation (app/ConsoleShell.tsx).
 *
 * This module is the pure, testable core: which URL parameter names which object kind, how the
 * query string is rewritten, and the single fetch — all injectable so each object type is pinned
 * by a test without a browser or a live API. See tests/console-owner.test.ts.
 */

/** The object kinds the control plane's resolver understands, keyed by the URL parameter the
 *  console opens them with. Order is precedence: the first present wins when a URL names more
 *  than one (a build link that still carries a stale `run=`, say). */
export const DEEP_LINK_PARAMS: ReadonlyArray<{ param: string; type: string }> = [
  { param: "build", type: "build" },
  { param: "meeting", type: "meeting" },
  { param: "wfr", type: "workflow_run" },
  { param: "run", type: "workflow_run" },
  { param: "decision", type: "decision" },
  { param: "self_fix", type: "self_fix" },
  { param: "escalation", type: "shepherd_escalation" },
  { param: "thread", type: "agent_thread" },
];

export type DeepLinkTarget = { param: string; type: string; id: string };

/** The object a URL is asking the console to open, or null when it names none. */
export function readDeepLinkTarget(search: string): DeepLinkTarget | null {
  const params = new URLSearchParams(search);
  for (const { param, type } of DEEP_LINK_PARAMS) {
    const id = params.get(param)?.trim();
    if (id) return { param, type, id };
  }
  return null;
}

/** The query string with `project_id` set to `projectId`, everything else kept. Returns a string
 *  starting with `?` (or empty when nothing is left), ready for replaceState or a server fetch. */
export function searchWithProject(search: string, projectId: string): string {
  const params = new URLSearchParams(search);
  params.set("project_id", projectId);
  const rest = params.toString();
  return rest ? `?${rest}` : "";
}

/** The current `project_id` the URL carries, or null. */
export function projectIdFromSearch(search: string): string | null {
  const value = new URLSearchParams(search).get("project_id")?.trim();
  return value || null;
}

/** The resolver endpoint for a target, as a project-selecting caller asks it (the caller's
 *  current project rides along so the API can decide visibility; it is never trusted as the
 *  answer). Returns a root-relative path so the browser hits its own origin proxy and the server
 *  hits `${API_URL}` when prefixed by the caller. */
export function resolveOwnerPath(target: DeepLinkTarget, currentProjectId?: string | null): string {
  const query = new URLSearchParams({ type: target.type, id: target.id });
  if (currentProjectId) query.set("project_id", currentProjectId);
  return `/v1/console/resolve-owner?${query.toString()}`;
}

export type OwnerResolution = {
  target: DeepLinkTarget | null;
  /** The project the console should land on: the object's own when resolved, else the one it
   *  started with (fail-open — the panel then shows its own not-found/denied, never the flip). */
  projectId: string | null;
  /** Did resolving move us to a different project than we started on? */
  switched: boolean;
  /** Whether the caller may view the object at all (false is answered identically to not-found). */
  canView: boolean;
};

type ResolveOk = { project_id?: unknown; can_view?: unknown };

/**
 * Resolve the URL's deep-link target to its owning project.
 *
 * `fetchImpl` is injected so this is unit-testable; callers pass a fetch that reaches the control
 * plane (the origin proxy in the browser, `${API_URL}` on the server). Any failure fails OPEN:
 * we keep the project we started on rather than block the render on a resolver hiccup.
 */
export async function resolveDeepLinkOwner(opts: {
  search: string;
  projectId: string | null;
  fetchImpl: (path: string) => Promise<{ ok: boolean; status: number; json: () => Promise<unknown> }>;
  /** Prefix for the resolver path (the server passes `${API_URL}`; the browser passes ""). */
  base?: string;
}): Promise<OwnerResolution> {
  const target = readDeepLinkTarget(opts.search);
  if (!target) {
    return { target: null, projectId: opts.projectId, switched: false, canView: false };
  }
  try {
    const res = await opts.fetchImpl((opts.base ?? "") + resolveOwnerPath(target, opts.projectId));
    if (!res.ok) {
      return { target, projectId: opts.projectId, switched: false, canView: false };
    }
    const body = (await res.json()) as ResolveOk;
    const canView = body.can_view === true;
    const owner = typeof body.project_id === "string" ? body.project_id.trim() : "";
    if (!canView || !owner) {
      return { target, projectId: opts.projectId, switched: false, canView };
    }
    return { target, projectId: owner, switched: owner !== opts.projectId, canView: true };
  } catch {
    return { target, projectId: opts.projectId, switched: false, canView: false };
  }
}
