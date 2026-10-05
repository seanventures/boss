/**
 * Organization fixtures: an org board as the server sends it (org-view spec §6.1), built on the
 * same teams as the Live board fixtures, so a test can say "Engineering hands Quality 31".
 */
import { TEAM_WIRE } from "./agents-home-fixtures";

type Wire = Record<string, unknown>;

export const TEAM_LABEL: Record<string, string> = {
  leadership: "Leadership", architecture: "Architecture", engineering: "Engineering", quality: "Quality",
  operations: "Operations", cost: "Cost", media: "Media", thinkers: "Thinkers", yours: "Yours",
};
const COL: Record<string, number | null> = {
  leadership: 0, architecture: 1, engineering: 2, quality: 3, operations: 4, cost: null, media: 3, thinkers: null, yours: null,
};

export const NOW_ISO = new Date(Date.now() - 72 * 60_000).toISOString();

export function teamWire(key: string, over: Wire = {}): Wire {
  const members = TEAM_WIRE[key] ?? [];
  return {
    key, label: TEAM_LABEL[key] ?? key, tip: key === "quality" ? "Quality and security" : null, members,
    needs_you: 0, waiting_you: 0, flow_col: COL[key] ?? null, self_handoffs: 0,
    work: { done: 0, failed: 0, open: 0 }, waiting: [], waiting_total: 0,
    members_load: members.map((r) => ({ role_key: r, busy_seconds: 0 })),
    spend: { metered_usd: 0, plan_calls: 0 }, models: [], configured_models: [],
    gaps: { abilities: [], step_gaps: [], missing_roles: 0, kit_ok: true, kit_reason: null, roles_ok: true },
    ...over,
  };
}

export function agentWire(roleKey: string, team: string, over: Wire = {}): Wire {
  return {
    role_key: roleKey, team, registered: true, work: { done: 0, failed: 0, open: 0 }, busy_seconds: 0,
    spend: { metered_usd: 0, plan_calls: 0 }, models: [], configured_model: null, open: [], open_total: 0,
    named_on: [], gaps: { abilities: [], step_gaps: [] }, ...over,
  };
}

export function edgeWire(from: string, to: string, handoffs: number, over: Wire = {}): Wire {
  return { from, to, handoffs, failed: 0, waiting: 0, stale: 0, last_at: NOW_ISO, ...over };
}

/** The engineering story: Forge (software_engineer) hands Rook (qa) work, two failed. */
export function orgWire(over: { teams?: Record<string, Wire>; agents?: Record<string, Wire>; [k: string]: unknown } = {}): Wire {
  const { teams: teamOver = {}, agents: agentOver = {}, ...rest } = over;
  const teams = Object.keys(TEAM_WIRE).map((k) => teamWire(k, teamOver[k] ?? {}));
  const agents = Object.entries(TEAM_WIRE).flatMap(([t, rs]) => rs.map((r) => agentWire(r, t, agentOver[r] ?? {})));
  return {
    project_id: "p1", days: 7, generated_at: new Date().toISOString(), etag: "\"ob-1\"",
    totals: { metered_usd: 12.4, plan_calls: 310, untied_usd: 0.84, untied_calls: 9, spend_ok: true, spend_reason: null },
    teams, agents,
    team_edges: [
      edgeWire("engineering", "quality", 31, { failed: 2, waiting: 1 }),
      edgeWire("leadership", "engineering", 19),
      edgeWire("engineering", "operations", 6),
    ],
    agent_edges: [
      edgeWire("software_engineer", "qa", 18, { failed: 2, how: "model_pick", label: "Picked by BOSS" }),
      edgeWire("backend_engineer", "adversarial_reviewer", 13, { waiting: 1, how: "next_stage", label: "Next stage" }),
      edgeWire("pm", "software_engineer", 19, { how: "named", label: "Named in workflow" }),
      edgeWire("software_engineer", "sre", 6, { how: "named", label: "Named in workflow" }),
    ],
    truncated: false, missing_roles: [], failures: [],
    declared_vs_observed: { planned_unused: [], unplanned_pairs: [], lines: 0 },
    ...rest,
  };
}

/** A fetch stub that answers the org board (and anything else the test passes). */
export function orgFetch(wire: () => Wire, extra: (url: string, init?: RequestInit) => Response | undefined = () => undefined) {
  const calls: { url: string; init?: RequestInit }[] = [];
  const fn = async (input: unknown, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init });
    const hit = extra(url, init);
    if (hit) return hit;
    if (url.startsWith("/v1/agents/org-board/handoffs")) return Response.json({ items: [] });
    if (url.startsWith("/v1/agents/org-board")) {
      const body = wire();
      return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json", etag: String(body.etag) } });
    }
    return Response.json({});
  };
  return { fn, calls };
}
