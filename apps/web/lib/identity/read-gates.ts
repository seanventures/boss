/**
 * A PAGE FETCHES ONLY WHAT THE VIEWER MAY READ (round 2, R2-WEB; IDENTITY.md §6.5).
 *
 * The role walk found pages asking for routes the role lacks, on every load: the agent-face
 * resolver (GET /v1/agents/identities, agent_passport:read) on every page for a custom role,
 * Setup's model capacity (entitlement:read), the Workflows deploy step and the timeline
 * (deployment:read, host:read, model_provider:read), the Improve BOSS card (telemetry:read) for
 * Operators and Viewers. Each answered 403 and some painted an error where a panel should be.
 *
 * ONE GATE, IN THE BROWSER, FOR EVERY READ. The identity provider installs `installReadGate`
 * once: a GET from the page to `/v1/…` or to a console `/api/…` proxy is matched to the
 * permission the API checks for it (READ_ROUTES, generated from the API's role matrix; the
 * proxies through API_READS) and, when the viewer's mirrored permissions refuse it, it is not
 * sent. The caller gets a 403 marked LOCKED (`isLocked`) carrying the reason in the words useCan
 * gives, and a panel renders <LockedPanel> for it instead of an error.
 *
 * The API still decides every call. Unmirrored (no provider, identity unreadable), a call whose
 * project differs from the mirrored one, or a refusal that only wants a fresh second factor is
 * sent as before. tests/a-page-fetches-only-what-the-viewer-may-read.test.ts.
 */
import type { CanResult } from "./contract";
import { currentMirror } from "./mirror";
import type { Permission } from "./permissions.gen";
import { READ_ROUTES } from "./read-routes.gen";
import { IMPROVE_READS } from "../improve-ops";
import { WRITING_GET_TWIN_SUFFIX } from "../writing-get";

export const LOCKED_HEADER = "x-boss-locked";

/**
 * The console's GET proxies that read one upstream route: `/api/<name>` (or `/api/<name>?view=v`)
 * → the /v1 route it forwards to (written after "/v1/", like READ_ROUTES). /api/improve's views come from its own table (IMPROVE_READS).
 * A proxy not listed is sent as before; the API answers it.
 */
export const API_READS: Readonly<Record<string, string>> = {
  "/api/deployment-targets": "deployment-targets",
  "/api/deployment-hosts": "deployment-hosts",
  "/api/model-routes": "model-routes",
  "/api/run-acts?view=chain": "workflow-runs/{}/chain",
  "/api/run-acts?view=act": "acts/{}",
  "/api/code-tasks": "code-tasks",
  "/api/coordination": "coordination",
  "/api/activity": "activity",
  "/api/activity/histogram": "activity/histogram",
  "/api/activity/facets": "activity/facets",
  "/api/activity/kinds": "activity/kinds",
  "/api/activity/context": "activity/context",
  "/api/email-config": "email/config",
  "/api/kit": "kit",
  "/api/mcp": "mcp/servers",
  "/api/mcp?view=catalogue": "mcp/catalogue",
  "/api/mcp?view=grants": "role-mcp-servers",
};

const BY_SEGMENTS = READ_ROUTES.map(([pattern, permission]) => {
  const segs = pattern.split("/").filter(Boolean);
  return { segs, permission, literal: segs.filter((s) => s !== "{}").length };
}).sort((a, b) => b.literal - a.literal);

/** The permission GET `/v1/…` checks (a `{}` matches one segment; the most literal pattern wins). */
export function permissionForV1(path: string): Permission | null {
  const segs = path.split("?")[0].split("/").filter(Boolean);
  if (segs[0] !== "v1") return null;
  segs.shift();
  for (const r of BY_SEGMENTS) {
    if (r.segs.length === segs.length && r.segs.every((s, i) => s === "{}" || s === segs[i])) return r.permission;
  }
  return null;
}

/** The /v1 route a same-origin GET reaches: itself, or the upstream of a listed /api proxy. */
export function upstreamOf(url: URL): string | null {
  const path = url.pathname.replace(/\/+$/, "");
  if (path.startsWith("/v1/")) return path;
  if (!path.startsWith("/api/")) return null;
  const view = url.searchParams.get("view");
  if (path === "/api/improve") {
    const read = view ? IMPROVE_READS[view] : undefined;
    if (!read) return null;
    const target = read.path(url.searchParams).split("?")[0];
    return target.endsWith(WRITING_GET_TWIN_SUFFIX) ? target.slice(0, -WRITING_GET_TWIN_SUFFIX.length) : target;
  }
  const upstream = (view ? API_READS[`${path}?view=${view}`] : undefined) ?? API_READS[path];
  return upstream ? `/v1/${upstream}` : null;
}

/** The permission a same-origin GET needs, or null when it is not one the gate knows. */
export function permissionForRead(input: string, base = "http://console.invalid"): Permission | null {
  let url: URL;
  try { url = new URL(input, base); } catch { return null; }
  const upstream = upstreamOf(url);
  return upstream ? permissionForV1(upstream) : null;
}

type Check = (permission: Permission, projectId?: string | null) => CanResult;

/**
 * Why this GET would be refused, or null to send it. `check` is the mirror's (the active
 * project's snapshot); `activeProject` is the project it mirrors. A call naming another
 * project is not judged here.
 */
export function readRefusal(input: string, check: Check | null, activeProject: string | null,
  base = "http://console.invalid"): { permission: Permission; reason: string } | null {
  if (!check) return null;
  let url: URL;
  try { url = new URL(input, base); } catch { return null; }
  if (url.origin !== new URL(base).origin) return null;
  const permission = permissionForRead(url.toString(), base);
  if (!permission) return null;
  const asked = url.searchParams.get("project_id");
  if (asked && activeProject && asked !== activeProject) return null;
  const r = check(permission);
  if (r.allowed || r.needsStepUp) return null;
  return { permission, reason: r.reason };
}

/** The 403 a refused read gets, without a request: `{error: "locked", permission, reason}`. */
export function lockedResponse(permission: Permission, reason: string): Response {
  return new Response(JSON.stringify({ error: "locked", code: "locked", permission, reason }), {
    status: 403, headers: { "content-type": "application/json", [LOCKED_HEADER]: "1" },
  });
}

export function isLocked(response: Pick<Response, "status" | "headers"> | null | undefined): boolean {
  return Boolean(response && response.status === 403 && response.headers.get(LOCKED_HEADER) === "1");
}

/** Install the gate around the browser's fetch, once. In the browser only. */
export function installReadGate(): void {
  if (typeof window === "undefined") return;
  const w = window as unknown as { __bossReadGate?: true; fetch: typeof fetch };
  if (w.__bossReadGate) return;
  w.__bossReadGate = true;
  const inner = w.fetch.bind(window);
  w.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
    if (method === "GET") {
      const raw = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      const mirror = currentMirror();
      const refused = mirror ? readRefusal(raw, mirror.check, mirror.projectId ?? null, window.location.origin) : null;
      if (refused) return Promise.resolve(lockedResponse(refused.permission, refused.reason));
    }
    return inner(input as RequestInfo, init);
  }) as typeof fetch;
}
