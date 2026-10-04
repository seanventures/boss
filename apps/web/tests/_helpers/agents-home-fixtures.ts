/**
 * Agents home fixtures: a Live board, as the server sends it, parsed by the frozen parser.
 * Shared by the Organization / Directory / Activity tests (stream S3).
 */
import { parseLiveBoard, type LiveBoard } from "../../lib/agents-live-board";

export const TEAM_WIRE: Record<string, string[]> = {
  leadership: ["coo", "cto", "pm", "program_manager", "metatron", "vp_agents"],
  architecture: ["chief_architect", "cao", "design_critic"],
  engineering: ["engineering_captain", "software_engineer", "backend_engineer", "frontend_engineer"],
  quality: ["qa", "adversarial_reviewer", "security_reviewer", "integration_validator", "secrets_steward"],
  operations: ["sre", "deployment_manager", "infra_steward", "operations_engineer", "release_manager"],
  cost: ["finops_sentinel", "model_spend_analyst", "providerops_manager", "resource_janitor"],
  media: ["media_publisher", "video_editor", "docs_writer"],
  thinkers: ["first_principles_researcher", "relentless_operator", "teacher_who_simplifies", "visionary_founder"],
  yours: [],
};
const LABEL: Record<string, string> = {
  leadership: "Leadership", architecture: "Architecture", engineering: "Engineering", quality: "Quality",
  operations: "Operations", cost: "Cost", media: "Media", thinkers: "Thinkers", yours: "Yours",
};

/** `states`: role_key → [state, line]. Everyone else is idle. */
export function boardWire(states: Record<string, [string, string]> = {}, extra: Record<string, unknown> = {}) {
  const teamOf = new Map(Object.entries(TEAM_WIRE).flatMap(([t, rs]) => rs.map((r) => [r, t] as const)));
  const agents = [...teamOf.keys()].map((role_key) => ({
    role_key, name: role_key.replace(/_/g, " "), team: teamOf.get(role_key),
    state: states[role_key]?.[0] ?? "idle", line: states[role_key]?.[1] ?? "Last: Reviewing · Build · 2h",
  }));
  const teams = Object.entries(TEAM_WIRE).map(([key, members]) => ({
    key, label: LABEL[key], members,
    working: members.filter((m) => ["working"].includes(states[m]?.[0] ?? "")).length,
    needs_you: members.filter((m) => states[m]?.[0] === "needs_you").length,
    in_panel: members.filter((m) => states[m]?.[0] === "in_panel").length,
  }));
  return {
    project_id: "p1", etag: "e1", generated_at: "2026-10-04T09:42:00Z",
    counts: { agents: agents.length, working: 1, needs_you: 1, running: 1, stopped: 0 },
    attention: [], missions: [], outcomes: [], teams, agents, ...extra,
  };
}

export function board(states: Record<string, [string, string]> = {}, extra: Record<string, unknown> = {}): LiveBoard {
  return parseLiveBoard(boardWire(states, extra));
}

export const SHIP_IT_MISSION = {
  run_id: "wfr_1", subject: "Ship it #182", workflow_key: "ship_it", workflow_name: "Ship it", state: "running",
  standing: "active", step_index: 4, step_count: 7, verb: "Coding", cost_usd: 0,
  hops: [
    { act_id: "a0", role_key: null, person: "ceo", state: "done", label: "Started", edge_label: "Started by you" },
    { act_id: "a1", role_key: "pm", state: "done", label: "PRD", edge_label: "Named in workflow" },
    { act_id: "a2", role_key: "chief_architect", state: "done", label: "Design", edge_label: "Named in workflow" },
    { act_id: "a3", role_key: "software_engineer", state: "working", label: "Build", edge_label: "Picked by BOSS over 3" },
  ],
  next: [],
};

/** matchMedia that answers the queries given as true, everything else false. */
export function stubMatchMedia(trueQueries: string[]): void {
  const mm = (query: string) => ({
    matches: trueQueries.some((q) => query.includes(q)), media: query, onchange: null,
    addEventListener: () => {}, removeEventListener: () => {}, addListener: () => {}, removeListener: () => {},
    dispatchEvent: () => false,
  });
  Object.defineProperty(window, "matchMedia", { configurable: true, writable: true, value: mm });
}
