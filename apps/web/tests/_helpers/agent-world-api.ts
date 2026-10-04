/**
 * A fake control plane for the Agent World screens, shaped like the frozen contract (spec §5.4).
 *
 * The other streams (identity, acts, converse) serve these endpoints; until they land the web is
 * tested against this, which answers in their wire shapes (snake_case) and records every call so
 * a test can assert what was SENT, not just what was drawn.
 */
import { vi } from "vitest";

export type Call = { url: string; method: string; body: Record<string, unknown> | null };
type Handler = (url: string, init: RequestInit, body: Record<string, unknown> | null) => Response | Promise<Response> | undefined;

export const PASSPORT = {
  agent_id: "agt_7f3k2q4m9x", role_key: "qa", display_name: "QA Lead",
  persona: {
    version: 1, archetype: null, callsign: "Vera", tagline: "Show me the test.",
    about_me: "I check what was claimed against what ran. I ask for the failing case first.",
    voice: { blunt: 3, thorough: 2, bold: 1, casual: 1 }, phrases: ["Show me the run."], never: ["Call it tested without a run"],
    avatar: { kit: "boss-v1", seed: "agt_7f3k2q4m9x", hat: "magnifier", emblem: "check", palette: "green" },
  },
  born: { at: "2026-10-03T09:12:00Z", by: "ceo", from: { kind: "pack", module_id: "boss", module_version: "1.4" } },
  sponsor: "ceo", lifecycle: "active", version: 4, fingerprint: "9f3ac21e77aa", reports_to: "engineering_captain",
  vitals: { trust: 82, runs: 41, clean_rate: 0.96, cost_7d: 3.1, panels: 2, apps: 3 },
  presence: { state: "idle", line: "Last: reviewed PR #412" },
  identity: { mode: "local", tokens: false },
};

export const DETAIL = {
  role_key: "qa", name: "QA Lead", registered: true, autonomy_level: 2, trust_score: 82, successful_runs: 41,
  violations: 0, hidden_failures: 0, reports_to: null, spec: { mission: "Prove it works." }, owns_stages: [],
  apps: [], panels: [], assigned_to: [], window_hours: 168, named_on_steps: [],
  skills: { role_key: "qa", holds: [], available: [] }, mcp: { role_key: "qa", holds: [], available: [] },
};

export const ACTS = [
  { act_id: "act_1", role_key: "qa", kind: "step", status: "complete", root_act_id: "act_0", parent_act_id: "act_0",
    handed_by_kind: "boss", how: "model_pick", edge_label: "Picked by BOSS over 3", why: "Holds qa.run_tests",
    considered: ["qa", "software_engineer", "adversarial_reviewer"], scope: { skills: ["qa.run_tests"], mcp: [] },
    scope_ok: true, accountable: "ceo", label: "Test · Ship it #182", workflow_run_id: "wfr_182", step_id: "test",
    artifacts: [], tokens_in: 1200, tokens_out: 300, cost_usd: 0.042, reconstructed: false,
    started_at: "2026-10-03T10:00:00Z", ended_at: "2026-10-03T10:04:00Z" },
  { act_id: "act_2", role_key: "qa", kind: "seat", status: "complete", root_act_id: "act_9", handed_by_kind: "person",
    handed_by_id: "ceo", how: "seated", edge_label: "Seated on panel", scope: { skills: [], mcp: [] },
    accountable: "ceo", label: "PRD review · Billing", meeting_run_id: "mtr_1", artifacts: [], tokens_in: 0,
    tokens_out: 0, cost_usd: 0, reconstructed: true, started_at: "2026-10-01T10:00:00Z", ended_at: "2026-10-01T10:02:00Z" },
];

export const LINEAGE = {
  nodes: [{ id: "boss", kind: "boss" }, { id: "qa", kind: "agent", role_key: "qa" },
          { id: "ceo", kind: "person" }, { id: "release_manager", kind: "agent", role_key: "release_manager" }],
  edges: [
    { from: "boss", to: "qa", acts: 14, how_top: "model_pick", label: "Picked by BOSS ×14", last_at: "2026-10-03T10:00:00Z", reconstructed: false },
    { from: "ceo", to: "qa", acts: 2, how_top: "seated", label: "Seated on panel ×2", last_at: "2026-10-01T10:00:00Z", reconstructed: true },
    { from: "qa", to: "release_manager", acts: 5, how_top: "next_stage", label: "Next stage ×5", last_at: "2026-10-03T10:05:00Z", reconstructed: false },
  ],
};

/** A text/event-stream body. */
export function sse(events: Array<{ event: string; data: unknown }>): Response {
  const text = events.map((e) => `event: ${e.event}\ndata: ${JSON.stringify(e.data)}\n\n`).join("");
  const bytes = new TextEncoder().encode(text);
  const stream = new ReadableStream<Uint8Array>({
    start(c) { c.enqueue(bytes.slice(0, 20)); c.enqueue(bytes.slice(20)); c.close(); },
  });
  return new Response(stream, { headers: { "content-type": "text/event-stream" } });
}

/** Install the fake. `extra` handlers run first; return undefined to fall through. */
export function installAgentWorldApi(extra: Handler[] = []): { calls: Call[] } {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init: RequestInit = {}) => {
    const url = String(input);
    const method = (init.method ?? "GET").toUpperCase();
    let body: Record<string, unknown> | null = null;
    if (typeof init.body === "string") { try { body = JSON.parse(init.body); } catch { body = null; } }
    calls.push({ url, method, body });
    for (const h of extra) {
      const r = await h(url, init, body);
      if (r) return r;
    }
    if (url.startsWith("/api/agents/qa")) return Response.json(DETAIL);
    if (url.includes("/v1/agents/qa/passport")) return Response.json(PASSPORT);
    if (url.includes("/v1/agents/qa/acts")) return Response.json({ items: ACTS, next: null });
    if (url.includes("/v1/agents/qa/lineage")) return Response.json(LINEAGE);
    if (url.includes("/v1/agents/live")) return Response.json({ items: [] });
    if (url.includes("/v1/memory")) {
      return Response.json({ memories: [
        { id: "mem_1", kind: "lesson", scope: "role", scope_ref: "qa", body: "Flaky e2e on Safari", status: "active" },
        { id: "mem_2", kind: "lesson", scope: "project", scope_ref: "", body: "Not mine", status: "active" },
      ] });
    }
    if (url.includes("/v1/agents/qa/card")) return Response.json({ name: "Vera", skills: [{ id: "qa.run_tests", name: "Run tests" }] });
    if (url.includes("/v1/agents/grantable")) return Response.json({ skills: [], mcp_servers: [], agents: [] });
    if (url.includes("/v1/agent-studio/templates")) return Response.json({ items: [] });
    return Response.json({});
  }));
  return { calls };
}
