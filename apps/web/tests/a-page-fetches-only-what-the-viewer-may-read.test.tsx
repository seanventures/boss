// @vitest-environment happy-dom
/**
 * A PAGE FETCHES ONLY WHAT THE VIEWER MAY READ (round 2, R2-WEB; lib/identity/read-gates.ts).
 *
 * The role walk caught pages asking for routes the role lacks on every load (a custom Developer
 * role: /v1/agents/identities, entitlements, deployment targets and hosts, model routes;
 * Operator and Viewer: the Improve BOSS card's /api/improve?view=sharing). The class fix is one
 * gate around the browser's fetch, judged from the API's own matrix. Held here:
 *   1. the gate's table is the matrix's (a stale copy fails), and every proxy it maps reaches a
 *      route the matrix declares;
 *   2. each read the walk caught maps to the permission the API checks;
 *   3. the gate: a refused read never leaves the browser and comes back LOCKED with the reason;
 *      an allowed one, a write, another project's or a step-up-only refusal is sent;
 *   4. end to end: under the real IdentityProvider, a child's FIRST mount effect (before the
 *      provider's own effects run) is already gated;
 *   5. a locked read renders as a locked panel, never as "Can't reach BOSS API".
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import React, { act, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import { LockedPanel, ReadFailure, readFailureLabel } from "../components/locked-panel";
import { apiErrorText } from "../lib/api-error";
import { IMPROVE_READS } from "../lib/improve-ops";
import { SELF_FIX_READS } from "../lib/self-fix-ops";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { installMirror } from "../lib/identity/mirror";
import { ROLE_PERMISSIONS, type Permission } from "../lib/identity/permissions.gen";
import { API_READ_OPS, installReadGate, isLocked, lockedResponse, permissionForRead, permissionForV1, readRefusal } from "../lib/identity/read-gates";
import { READ_ROUTES } from "../lib/identity/read-routes.gen";
// @ts-expect-error a plain .mjs script, shared with the generator
import { readRoutesSource } from "../scripts/read-routes.mjs";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const WEB = process.cwd();
const MATRIX = readFileSync(join(WEB, "..", "..", "tests/identity/matrix.yaml"), "utf8");
const MATRIX_GET = new Map<string, string>();
for (const line of MATRIX.split("\n")) {
  const m = /^"GET (\/v1\/\S+)": \["permit ([a-z_]+:[a-z_]+) @ /.exec(line);
  if (m) MATRIX_GET.set(m[1].replace(/\{[^}]*\}/g, "{}"), m[2]);
}

describe("1. the gate's table is the API's", () => {
  it("read-routes.gen.ts is the matrix, regenerated (node apps/web/scripts/read-routes.mjs)", () => {
    expect(readFileSync(join(WEB, "lib/identity/read-routes.gen.ts"), "utf8")).toBe(readRoutesSource(MATRIX));
    expect(READ_ROUTES.length).toBeGreaterThan(400);
  });
  // Follow-up 2: EVERY GET proxy is mapped, by operation. A new app/api/**/route.ts that exports
  // GET and has no entry fails here, as does an operation whose route the API does not declare.
  function routes(dir: string): string[] {
    return readdirSync(join(WEB, dir)).flatMap((name) => {
      const p = join(dir, name);
      if (statSync(join(WEB, p)).isDirectory()) return routes(p);
      return /^route\.tsx?$/.test(name) ? [p] : [];
    });
  }
  const getProxies = routes("app/api")
    .filter((f) => /export (async function|function|const) GET\b/.test(readFileSync(join(WEB, f), "utf8")))
    .map((f) => "/" + f.replace(/^app\//, "").replace(/\/route\.tsx?$/, "")
      .replace(/\[\[\.\.\.[^\]]+\]\]/g, "{...}").replace(/\[[^\]]+\]/g, "{}"));
  it("finds the GET proxies (the scan is not empty)", () => {
    expect(getProxies.length).toBeGreaterThan(40);
  });
  it.each(getProxies.filter((p) => p !== "/api/improve" && p !== "/api/self-fix"))("%s has an entry in API_READ_OPS", (proxy) => {
    expect(API_READ_OPS[proxy], `map ${proxy} in lib/identity/read-gates.ts API_READ_OPS`).toBeDefined();
  });
  it("names no proxy that does not exist", () => {
    expect(Object.keys(API_READ_OPS).filter((k) => !getProxies.includes(k))).toEqual([]);
  });
  const opRoutes = Object.entries(API_READ_OPS).flatMap(([proxy, read]) => "none" in read ? []
    : Object.entries(read.ops).flatMap(([op, r]) => (typeof r === "string" ? [r] : [...r]).map((route) => [`${proxy} ${op || "(default)"}`, route] as const)));
  it.each(opRoutes)("%s reads /v1/%s, a route the matrix declares", (_op, route) => {
    expect(MATRIX_GET.has(`/v1/${route.replace(/\{[^}]*\}/g, "{}")}`), route).toBe(true);
  });
  it.each(Object.keys(IMPROVE_READS))("/api/improve?view=%s maps to a declared route", (view) => {
    expect(permissionForRead(`/api/improve?view=${view}&project_id=p&id=x`), view).not.toBeNull();
  });
  it.each(Object.keys(SELF_FIX_READS))("/api/self-fix?view=%s maps to a declared route", (view) => {
    expect(permissionForRead(`/api/self-fix?view=${view}&project_id=p&id=x&run_id=r`), view).not.toBeNull();
  });
});

describe("2. each read the role walk caught maps to the permission the API checks", () => {
  it.each([
    ["/v1/agents/identities?project_id=p", "agent_passport:read"],
    ["/v1/entitlements/sources?project_id=p", "entitlement:read"],
    ["/v1/entitlement-connection-sessions?project_id=p&limit=20", "entitlement:read"],
    ["/api/deployment-targets?project_id=p", "deployment:read"],
    ["/api/deployment-hosts?project_id=p", "host:read"],
    ["/api/model-routes?project_id=p&unit_id=u", "model_provider:read"],
    ["/api/improve?view=sharing&project_id=p", "telemetry:read"],
    ["/api/run-acts?view=chain&run_id=wfr_1&project_id=p", "workflow_run:read"],
    ["/api/kit?project_id=p", "skill:read"],
    ["/api/agent?view=briefing&project_id=p", "assistant:chat"],
    ["/api/delivery-workflows?project_id=p", "workflow:read"],
    ["/api/delivery-workflows?view=repositories&project_id=p", "repository:read"],
    ["/api/govern?view=spend&project_id=p", "budget:read"],
    ["/api/sandboxes/sb_1/tree", "sandbox:read"],
    ["/api/hardware/places?project_id=p", "host:read"],
    ["/api/run-credits/crun_1?project_id=p", "code_task:read"],
    ["/api/setup?project_id=p&repos=c1", "github:read"],
    ["/api/mcp?project_id=p", "mcp_server:read"],
  ])("%s → %s", (url, permission) => {
    expect(permissionForRead(url)).toBe(permission);
  });
  it("a literal route beats a parameter one (/v1/decisions/inbox is not /v1/decisions/{id})", () => {
    for (const [pattern, permission] of READ_ROUTES) {
      if (pattern.includes("{}")) continue;
      expect(permissionForV1(`/v1/${pattern}`), pattern).toBe(permission);
    }
  });
  it("an unknown path or a page is not judged", () => {
    expect(permissionForRead("/?area=setup")).toBeNull();
    expect(permissionForRead("/api/nothing-here")).toBeNull();
    expect(permissionForRead("/v1/not/a/route/at/all")).toBeNull();
  });
});

const refusing = (held: Permission[], stepUp: Permission[] = []) => (p: Permission) => held.includes(p)
  ? { allowed: true, reason: "", needsStepUp: false }
  : { allowed: false, reason: `Needs ${p} · ask an Admin`, needsStepUp: stepUp.includes(p) };

describe("3. the gate", () => {
  const sent: string[] = [];
  beforeEach(() => {
    sent.length = 0;
    delete (window as { __bossReadGate?: true }).__bossReadGate;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      sent.push(String(input instanceof Request ? input.url : input));
      return new Response("{}", { status: 200 });
    }));
    window.fetch = globalThis.fetch;
    installReadGate();
  });
  afterEach(() => { installMirror(null); vi.unstubAllGlobals(); delete (window as { __bossReadGate?: true }).__bossReadGate; });
  const mirror = (held: Permission[], stepUp: Permission[] = []) =>
    installMirror({ check: refusing(held, stepUp), stepUp: async () => false, refresh: async () => undefined, projectId: "p" });

  it("a refused read is not sent; it comes back LOCKED with the reason", async () => {
    mirror(["project:read"]);
    const r = await window.fetch("/api/deployment-hosts?project_id=p");
    expect(sent).toEqual([]);
    expect(isLocked(r)).toBe(true);
    expect(await r.json()).toMatchObject({ error: "locked", permission: "host:read", reason: "Needs host:read · ask an Admin" });
  });
  it("an allowed read, a write, another project's read and a step-up-only refusal are sent", async () => {
    mirror(["host:read"], ["audit:read"]);
    await window.fetch("/api/deployment-hosts?project_id=p");
    await window.fetch("/v1/agents/identities?project_id=p", { method: "POST" });
    await window.fetch("/v1/agents/identities?project_id=other");
    await window.fetch("/v1/identity/audit?project_id=p");
    expect(sent).toHaveLength(4);
  });
  it("a composite read is sent when any part is readable, and locked only when none is", async () => {
    mirror(["work_item:read"]);
    await window.fetch("/api/agent?view=workspace&project_id=p");
    expect(sent).toHaveLength(1);
    mirror([]);
    expect(isLocked(await window.fetch("/api/agent?view=workspace&project_id=p"))).toBe(true);
    expect(sent).toHaveLength(1);
  });
  it("with no mirror (no provider, identity unreadable) everything is sent", async () => {
    installMirror(null);
    await window.fetch("/v1/agents/identities?project_id=p");
    expect(sent).toHaveLength(1);
  });
  it("another origin is never judged", () => {
    expect(readRefusal("https://elsewhere.example/v1/agents/identities", refusing([]), "p", "http://console.test")).toBeNull();
  });
});

describe("4. under the real IdentityProvider, the first mount effect is already gated", () => {
  let root: Root | null = null;
  let host: HTMLElement | null = null;
  afterEach(() => { act(() => root?.unmount()); host?.remove(); vi.unstubAllGlobals(); installMirror(null); delete (window as { __bossReadGate?: true }).__bossReadGate; });

  // A custom role like the walk's Developer: an Operator without these reads.
  const WITHOUT: Permission[] = ["agent_passport:read", "entitlement:read", "deployment:read", "host:read", "model_provider:read", "telemetry:read"];
  const developer: MePermissionsResponse = {
    epoch: 1, scope: "org", step_up_fresh_until: null, catalogue_fingerprint: "x",
    permissions: [...ROLE_PERMISSIONS.operator].filter((p) => !WITHOUT.includes(p as Permission)),
  };
  const ME = { principal_id: "usr_d", kind: "user", display_name: "Dev", email: "d@acme.dev", org: { id: "org_1", name: "Acme" },
    status: "active", access_roles: ["developer"], sign_in: { password: true, totp: false, passkeys: 0, recovery_codes_remaining: 0, sso_links: [] },
    mfa_required: false, mfa_at: null, session_id: "s", session_expires_at: null, csrf_token: "c", epoch: 1, network_bound: false,
    banners: [], avatar_seed: null } as MeResponse;

  function Reader({ urls, out }: { urls: string[]; out: Record<string, boolean> }) {
    useEffect(() => {
      for (const u of urls) void window.fetch(u).then((r) => { out[u] = isLocked(r); });
    }, []); // eslint-disable-line react-hooks/exhaustive-deps
    return null;
  }

  it("the walk's Developer reads none of the routes it lacks, and still reads the rest", async () => {
    const sent: string[] = [];
    const net = vi.fn(async (input: RequestInfo | URL) => {
      sent.push(String(input instanceof Request ? input.url : input));
      return new Response("{}", { status: 200 });
    });
    vi.stubGlobal("fetch", net);
    window.fetch = net as unknown as typeof fetch;
    const out: Record<string, boolean> = {};
    const urls = [
      "/v1/agents/identities?project_id=p1", "/v1/entitlements/sources?project_id=p1",
      "/api/deployment-targets?project_id=p1", "/api/deployment-hosts?project_id=p1",
      "/api/model-routes?project_id=p1&unit_id=u", "/api/improve?view=sharing&project_id=p1",
      "/api/code-tasks?project_id=p1",
    ];
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => root!.render(
      <IdentityProvider me={ME} org={developer} project={{ ...developer, scope: "project:p1" }} projectId="p1">
        <Reader urls={urls} out={out} />
      </IdentityProvider>));
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    const reads = sent.filter((u) => !u.includes("/v1/identity/"));
    expect(reads).toEqual(["/api/code-tasks?project_id=p1"]);
    expect(Object.entries(out).filter(([, locked]) => locked).map(([u]) => u).sort()).toEqual(urls.slice(0, 6).sort());
  });
});

describe("5. a locked read is a locked panel, never an error", () => {
  it("ReadFailure: 403 → locked; nothing answered → can't reach; anything else → couldn't load", () => {
    expect(readFailureLabel(403)).toBe("locked");
    expect(readFailureLabel(0)).toBe("unreachable");
    expect(readFailureLabel(502)).toBe("unreachable");
    expect(readFailureLabel(500)).toBe("failed");
    expect(readFailureLabel(422)).toBe("failed");
  });
  it("renders the lock with the reason, not an alert", async () => {
    const host = document.createElement("div");
    const root = createRoot(host);
    await act(async () => root.render(<ReadFailure problem={{ text: "Needs “Read skills” · ask an Admin", status: 403 }} onRetry={() => undefined} />));
    expect(host.querySelector("[data-locked]")).not.toBeNull();
    expect(host.querySelector("[role=alert]")).toBeNull();
    expect(host.textContent).toContain("Locked");
    expect(host.textContent).not.toContain("Can't reach");
    expect(host.querySelector("[title]")!.getAttribute("title")).toBe("Needs “Read skills” · ask an Admin");
    await act(async () => root.render(<LockedPanel reason="x" inline />));
    act(() => root.unmount());
  });
  it("every reader that turns a refusal into words says the lock's reason (lib/api-error.ts)", async () => {
    const body = await lockedResponse("host:read", "Needs “Read hosts” · ask an Admin").json();
    expect(apiErrorText(body, 403)).toBe("Needs “Read hosts” · ask an Admin");
  });
  it("Skills & MCP and Setup's capacity row render a locked read through it", () => {
    for (const f of ["app/skills/kit-home.tsx", "app/skills/library.tsx"]) {
      const src = readFileSync(join(WEB, f), "utf8");
      expect(src, f).toMatch(/<ReadFailure\b/);
      expect(src, f).not.toMatch(/Can&apos;t reach BOSS API/);
    }
    expect(readFileSync(join(WEB, "app/setup/capacity-row.tsx"), "utf8")).toMatch(/isLocked\(a\)[\s\S]*<LockedPanel/);
  });
  it("the identity provider installs the gate during render, not only in an effect", () => {
    const src = readFileSync(join(WEB, "components/identity/identity-provider.tsx"), "utf8");
    expect(src).toMatch(/if \(typeof window !== "undefined" && currentMirror\(\) !== mirror\) \{\s*installMirror\(mirror\);\s*installReadGate\(\);/);
  });
});

