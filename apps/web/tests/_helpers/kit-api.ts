/**
 * A fake control plane for Skills & MCP, answering in the API's own shapes. The fixtures are real
 * GET /v1/kit answers (kit-fresh.json was captured from a freshly seeded install) and their
 * derivations (kit-ready, kit-failing). Every call is recorded, so a test asserts what was SENT.
 */
import { vi } from "vitest";

import fresh from "../fixtures/kit-fresh.json";
import ready from "../fixtures/kit-ready.json";
import failing from "../fixtures/kit-failing.json";
import type { Ability } from "../../lib/kit-types";
import type { KitWithCrew } from "../../lib/kit-api";

export type Call = { url: string; method: string; body: Record<string, unknown> | null };
export type Handler = (url: string, body: Record<string, unknown> | null, method: string) => unknown | undefined;

export const KITS = { fresh, ready, failing } as unknown as Record<"fresh" | "ready" | "failing", KitWithCrew>;

export function clone<T>(x: T): T { return JSON.parse(JSON.stringify(x)) as T; }

export function ability(kit: KitWithCrew, key: string): Ability {
  const a = kit.abilities.find((x) => x.key === key);
  if (!a) throw new Error(`no ${key}`);
  return a;
}

export const SERVERS = [
  { id: "mcp_web", name: "web-intel", url: "http://mcp-tools:8770/mcp", enabled: true, risk_tier: 0, data_boundary: "public",
    description: "Render and study websites", trusted: true, reachable: true, last_error: null,
    tools: [{ name: "fetch_page" }, { name: "study_site" }, { name: "search_web" }, { name: "render_mockup" }],
    tool_policy: {}, tool_risk_overrides: {}, provenance: { source: "builtin" }, publisher: {},
    auth: { type: "none", header_name: null, set: false, last4: null },
    references: { custom_steps: [], skills: [], agents: [{ role_key: "pm", tools: [] }] } },
  { id: "mcp_c7", name: "context7", url: "https://mcp.context7.com/mcp", enabled: true, risk_tier: 0, data_boundary: "public",
    description: "Library docs", trusted: false, reachable: true, last_error: null,
    tools: [{ name: "resolve-library-id" }, { name: "get-library-docs" }],
    tool_policy: {}, tool_risk_overrides: {}, provenance: { source: "catalogue" }, publisher: {},
    auth: { type: "bearer", header_name: null, set: true, last4: "a1f3" },
    references: { custom_steps: [], skills: [], agents: [] } },
];

export const CATALOGUE = [
  { key: "context7", name: "context7", label: "Context7", url: "https://mcp.context7.com/mcp", auth: "none", risk_default: 0,
    provenance: "official", description: "Up-to-date docs for the libraries you use." },
  { key: "linear", name: "linear", label: "Linear", url: "https://mcp.linear.app/mcp", auth: "bearer", risk_default: 2,
    provenance: "official", description: "Issues and projects in Linear.", token_url: "https://linear.app/settings/api" },
];

/** Install the fake. `kit` is what GET /api/kit answers; `on` may answer any call first. */
export function installKitApi(kit: KitWithCrew = clone(KITS.fresh), on?: Handler): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input);
    const method = (init.method ?? "GET").toUpperCase();
    let body: Record<string, unknown> | null = null;
    try { body = init.body ? JSON.parse(String(init.body)) : null; } catch { body = null; }
    calls.push({ url, method, body });
    const answer = (data: unknown, status = 200) => new Response(JSON.stringify(data), {
      status, headers: { "Content-Type": "application/json" } });
    const custom = on?.(url, body, method);
    if (custom instanceof Response) return custom;
    if (custom !== undefined) return answer(custom);
    if (url.startsWith("/api/kit") && method === "GET") {
      const key = new URL(url, "http://x").searchParams.get("ability");
      return answer(key ? ability(kit, key) : kit);
    }
    if (url.startsWith("/api/kit") && method === "POST") {
      const key = String(body?.ability ?? "");
      const action = String(body?.action ?? "");
      if (action === "holders") return answer({ ...ability(kit, key), holders: body?.roles });
      if (action === "recommended") return answer({ granted: [{ role_key: "pm", ability: "email" }], skipped: [] });
      if (action === "test_crew") return answer({ rows: [] });
      if (action === "test") return answer({ ok: true, at: new Date().toISOString(), latency_ms: 5, fact: "ok", error: null, ability: ability(kit, key) });
      return answer(key ? ability(kit, key) : {});
    }
    if (url.startsWith("/api/mcp") && method === "GET") {
      if (url.includes("view=catalogue")) return answer({ items: CATALOGUE });
      if (url.includes("view=grants")) return answer({ assignments: [] });
      return answer({ items: SERVERS });
    }
    if (url.startsWith("/api/mcp")) return answer(body?.action === "test" ? { ok: true, tools: [{ name: "x", description: "" }], latency_ms: 3 } : SERVERS[0]);
    if (url.startsWith("/api/skills")) {
      if (url.includes("view=builtin")) return answer({ items: [
        { id: "github.create_pr_real", source: "builtin", risk_tier: 2, description: "Open a PR", permissions: { allowed_callers: ["software_factory"] } },
        { id: "model.route_via_entitlement_broker", source: "builtin", risk_tier: 1, description: "Route", permissions: { allowed_callers: ["router"] } },
      ] });
      if (url.includes("view=assignments")) return answer({ assignments: [{ role_key: "pm", skill_id: "github.create_pr_real" }] });
      if (method === "GET") return answer({ skills: [] });
      return answer({ ok: true });
    }
    if (url.startsWith("/api/channels")) {
      if (url.includes("view=client")) return answer({ configured: false, redirect_uri: "https://boss.example/v1/channels/youtube/callback" });
      return answer({ channels: [] });
    }
    return answer({});
  }));
  return calls;
}
