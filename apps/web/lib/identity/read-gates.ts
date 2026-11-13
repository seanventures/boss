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
import { SELF_FIX_READS } from "../self-fix-ops";
import { WRITING_GET_TWIN_SUFFIX } from "../writing-get";

export const LOCKED_HEADER = "x-boss-locked";

/**
 * EVERY CONSOLE GET PROXY, BY OPERATION (round 2 follow-up 2). Keyed by the proxy's route as its
 * folder spells it (`[id]` → `{}`, `[[...path]]` → `{...}`). `op` names the operation a request
 * asks for (its `view`, its sub-path …) and `ops` maps each operation to the /v1 route(s) it
 * reads, written after "/v1/" like READ_ROUTES. Several routes (a composite view): it is sent
 * when ANY of them is readable, since each part degrades on its own. `none` says why a proxy is
 * not judged here. tests/a-page-fetches-only-what-the-viewer-may-read.test.tsx fails for a GET
 * proxy with no entry, and for an operation whose route the API does not declare.
 */
export type ApiRead =
  | { readonly op: (url: URL, sub: string) => string; readonly ops: Readonly<Record<string, string | readonly string[]>> }
  | { readonly none: string };

const one = (route: string): ApiRead => ({ op: () => "", ops: { "": route } });
const byView = (ops: Record<string, string | readonly string[]>, fallback = ""): ApiRead =>
  ({ op: (u) => u.searchParams.get("view") ?? fallback, ops });
const bySub = (ops: Record<string, string | readonly string[]>): ApiRead => ({ op: (_u, sub) => sub, ops });

export const API_READ_OPS: Readonly<Record<string, ApiRead>> = {
  "/api/activity": one("activity"),
  "/api/activity/context": one("activity/context"),
  "/api/activity/export": one("activity/export"),
  "/api/activity/facets": one("activity/facets"),
  "/api/activity/histogram": one("activity/histogram"),
  "/api/activity/kinds": one("activity/kinds"),
  "/api/activity/row/{}": one("activity/row/{}"),
  "/api/agent": byView({
    workspace: ["tasks", "agent/threads"], messages: "agent/threads/{}/messages", roles: "roles",
    starters: "agent/role-starters", briefing: "agent/briefing", progress: "agent/threads/{}/progress",
    "task-context": ["tasks/{}", "code-tasks"],
  }),
  "/api/agent-card/{}": one("agents/{}/passport"),
  "/api/agent-thread": one("agent/threads/{}/ledger"),
  "/api/agents/{}": one("console/v2/agents/{}"),
  "/api/autonomy": one("console/v2/autonomy"),
  "/api/channels": byView({ "": "channels", client: "channels/{}/client" }),
  "/api/code-tasks": one("code-tasks"),
  "/api/conversations/{...}": { op: (_u, sub) => (sub ? (sub === "presets" ? "presets" : "one") : ""),
    ops: { "": "conversations", presets: "conversations/presets", one: "conversations/{}" } },
  "/api/coordination": one("coordination"),
  "/api/delivery-workflows": byView({
    catalogue: "delivery-workflows/catalogue", capabilities: "delivery-workflows/capabilities",
    custom_steps: "delivery-workflows/custom-steps", installed: "delivery-workflows", library: "delivery-workflows",
    runs: "delivery-workflows/runs", schedules: "delivery-workflows/schedules", repositories: "projects/{}/repositories",
    costs: "delivery-workflows/costs", asks: "delivery-workflows/asks", spec: "delivery-workflows/{}/spec",
    plan: "delivery-workflows/{}/plan", readiness: "delivery-workflows/{}/readiness",
  }, "runs"),
  "/api/deployment-hosts": one("deployment-hosts"),
  "/api/deployment-targets": one("deployment-targets"),
  "/api/govern": byView({ limits: "govern", spend: "govern/spend", envelopes: "govern/envelopes", daily: "budget/daily" }, "limits"),
  "/api/hardware/{...}": bySub({ places: "hardware/places", resolve: "hardware/resolve" }),
  "/api/host-advice": one("projects/{}/host-advice"),
  "/api/kit": { op: (u) => (u.searchParams.get("ability") ? "ability" : ""), ops: { "": "kit", ability: "kit/{}" } },
  "/api/mcp": byView({ "": "mcp/servers", catalogue: "mcp/catalogue", grants: "role-mcp-servers" }),
  "/api/memory": one("memory"),
  "/api/model-routes": one("model-routes"),
  "/api/org": one("console/v2/org"),
  "/api/preview-gates": one("previews/{}/gate"),
  "/api/preview-logs": one("previews/{}/logs"),
  "/api/project-agents": one("projects/{}/agents"),
  "/api/projects-strip": one("console/v2/overview"),
  "/api/render-timing": { none: "answers from this console's own memory, gated in the handler on system:read" },
  "/api/run-acts": { op: (u) => (u.searchParams.get("view") === "act" ? "act"
      : (u.searchParams.get("run_id") ?? "").startsWith("crun_") ? "build" : "chain"),
    ops: { chain: "workflow-runs/{}/chain", build: "code-tasks/{}/chain", act: "acts/{}" } },
  "/api/run-credits/{}": { op: (_u, sub) => (sub.startsWith("crun_") ? "build" : "chain"),
    ops: { chain: "workflow-runs/{}/chain", build: "code-tasks/{}/chain" } },
  "/api/run-deployments": one("code-tasks/{}/deployments"),
  "/api/sandboxes/helpers": one("sandboxes/helpers"),
  "/api/sandboxes/{}/file": one("sandboxes/{}/file"),
  "/api/sandboxes/{}/logs": one("sandboxes/{}/logs"),
  "/api/sandboxes/{}/stats": one("sandboxes/{}/stats"),
  "/api/sandboxes/{}/tree": one("sandboxes/{}/tree"),
  "/api/setup": { op: (u) => (u.searchParams.get("ci_workflows") ? "ci_workflows"
      : u.searchParams.get("repos") ? "repos" : u.searchParams.get("available") === "1" ? "available"
      : u.searchParams.get("catalogue") ? "catalogue" : ""),
    ops: { "": "setup", repos: "setup/repository/repos", available: "setup/repository/available", catalogue: "setup/models/{}/catalogue",
           ci_workflows: "setup/repository/{}/ci-workflows" } },
  "/api/skill-supply": byView({ versions: "skill-definitions/{}/versions", diff: "skill-definitions/{}/diff",
                                tests: "skill-definitions/{}/tests", sources: "skill-sources",
                                consents: "skill-consents" }, "versions"),
  "/api/skills": byView({ "": "skill-definitions", builtin: "skills", assignments: "role-skills" }),
  "/api/workflow-report": one("workflow-runs/{}/report"),
};

/** The proxy entry a path names, and the part of the path past its fixed prefix. */
function proxyFor(path: string): { read: ApiRead; sub: string } | null {
  const segs = path.split("/").filter(Boolean);
  for (const [pattern, read] of Object.entries(API_READ_OPS)) {
    const ps = pattern.split("/").filter(Boolean);
    const rest = ps[ps.length - 1] === "{...}";
    const fixed = rest ? ps.slice(0, -1) : ps;
    if (rest ? segs.length < fixed.length : segs.length !== fixed.length) continue;
    if (!fixed.every((p, i) => p === "{}" || p === segs[i])) continue;
    const dyn = fixed.map((p, i) => (p === "{}" ? segs[i] : null)).filter((x): x is string => x !== null);
    return { read, sub: rest ? segs.slice(fixed.length).join("/") : dyn.join("/") };
  }
  return null;
}

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

/** The /v1 route(s) a same-origin GET reaches: itself, or what its /api proxy's operation reads.
 *  Null when the gate does not judge it. */
export function upstreamsOf(url: URL): string[] | null {
  const path = url.pathname.replace(/\/+$/, "");
  if (path.startsWith("/v1/")) return [path];
  if (!path.startsWith("/api/")) return null;
  const view = url.searchParams.get("view");
  // The two named-view proxies (Improve BOSS, BOSS fixes itself): each view IS one /v1 path.
  if (path === "/api/improve" || path === "/api/self-fix") {
    const reads = path === "/api/improve" ? IMPROVE_READS : SELF_FIX_READS;
    const read = view ? reads[view] : undefined;
    if (!read) return null;
    const target = read.path(url.searchParams).split("?")[0];
    return [target.endsWith(WRITING_GET_TWIN_SUFFIX) ? target.slice(0, -WRITING_GET_TWIN_SUFFIX.length) : target];
  }
  const found = proxyFor(path);
  if (!found || "none" in found.read) return null;
  const routes = found.read.ops[found.read.op(url, found.sub)];
  if (!routes) return null;
  return (typeof routes === "string" ? [routes] : [...routes]).map((r) => `/v1/${r}`);
}

/** The permissions a same-origin GET needs (any one suffices), or null when the gate does not know it. */
export function permissionsForRead(input: string, base = "http://console.invalid"): Permission[] | null {
  let url: URL;
  try { url = new URL(input, base); } catch { return null; }
  const upstreams = upstreamsOf(url);
  if (!upstreams) return null;
  const perms = upstreams.map(permissionForV1);
  return perms.every((p): p is Permission => p !== null) ? perms : null;
}

/** The permission a same-origin GET needs (the first, for a composite read). */
export function permissionForRead(input: string, base = "http://console.invalid"): Permission | null {
  return permissionsForRead(input, base)?.[0] ?? null;
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
  const permissions = permissionsForRead(url.toString(), base);
  if (!permissions?.length) return null;
  const asked = url.searchParams.get("project_id");
  if (asked && activeProject && asked !== activeProject) return null;
  let first: { permission: Permission; reason: string } | null = null;
  for (const permission of permissions) {
    const r = check(permission);
    if (r.allowed || r.needsStepUp) return null;
    first ??= { permission, reason: r.reason };
  }
  return first;
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
