import { API_URL, authHeaders } from "../client";

/**
 * The agents, for Workforce, Hive and Org Chart.
 *
 * One endpoint behind three screens, so their counts cannot disagree. v1 showed
 * "Agents active 0/21" — a number that counted roles owning a task, when only 23 of 187 tasks
 * had an owner at all, so it described the schema rather than the agents.
 */

export type V2AgentStatus = "WORKING" | "STUCK" | "IDLE" | "AT GATE";

export type V2Agent = {
  id: string;
  name: string;
  role: string;
  status: V2AgentStatus;
  task: string;
  model: string | null;
  /** ABSENT, NOT ZERO. BOSS records project_id/code_task_run_id/stage on an LLM call and
   *  run_id/role_key on a step, with no join between them — so per-agent cost cannot be
   *  computed. A zero would read as "this agent costs nothing" rather than "nobody knows".
   *  An earlier version faked it with a time-window join and showed two agents the same
   *  $61.3802. */
  tokens: number | null;
  spend_usd: number | null;
  calls: number | null;
  autonomy_level: number;
  trust_score: number;
  successful_runs: number;
  violations: number;
  hidden_failures: number;
  last_seen_seconds: number | null;
  run_id: string | null;
  /** Which piece of work the agent is on — the run its newest step belongs to. */
  project: string | null;
  /** The last artefact it touched, read from the step's own output. Null when the step
   *  recorded nothing: a plausible-looking filename cannot be told apart from a real one. */
  last_touched: string | null;
};

export type V2Handoff = {
  source: string;
  target: string;
  times: number;
  last_action: string | null;
  last_at: string | null;
};

export type Workforce = {
  project_id: string;
  window_hours: number;
  agents: V2Agent[];
  handoffs: V2Handoff[];
  counts: Record<string, number>;
  spend_attributed?: boolean;
  spend_note?: string;
  apiReachable: boolean;
  blockedReason?: string;
};

export type OrgNode = V2Agent & {
  handoffs_in: number;
  handoffs_out: number;
  connected: boolean;
};

export type OrgChart = {
  project_id: string;
  window_hours: number;
  nodes: OrgNode[];
  edges: V2Handoff[];
  derived_from: string;
  note: string;
  isolated: string[];
  apiReachable: boolean;
  blockedReason?: string;
};

const EMPTY_COUNTS = { WORKING: 0, STUCK: 0, IDLE: 0, "AT GATE": 0, total: 0 };

export function emptyWorkforce(projectId: string, reason: string): Workforce {
  return {
    project_id: projectId,
    window_hours: 24,
    agents: [],
    handoffs: [],
    counts: { ...EMPTY_COUNTS },
    apiReachable: false,
    blockedReason: reason,
  };
}

export function emptyOrgChart(projectId: string, reason: string): OrgChart {
  return {
    project_id: projectId,
    window_hours: 168,
    nodes: [],
    edges: [],
    derived_from: "observed handoffs",
    note: "",
    isolated: [],
    apiReachable: false,
    blockedReason: reason,
  };
}

export async function fetchWorkforce(projectId: string, hours = 24): Promise<Workforce> {
  try {
    const r = await fetch(
      `${API_URL}/v1/console/v2/workforce?project_id=${encodeURIComponent(projectId)}&hours=${hours}`,
      { headers: authHeaders({}), cache: "no-store" },
    );
    if (!r.ok) return emptyWorkforce(projectId, `API returned ${r.status}`);
    return { ...(await r.json()), apiReachable: true };
  } catch (e) {
    return emptyWorkforce(projectId, String((e as Error)?.message ?? e));
  }
}

export async function fetchOrgChart(projectId: string, hours = 168): Promise<OrgChart> {
  try {
    const r = await fetch(
      `${API_URL}/v1/console/v2/org?project_id=${encodeURIComponent(projectId)}&hours=${hours}`,
      { headers: authHeaders({}), cache: "no-store" },
    );
    if (!r.ok) return emptyOrgChart(projectId, `API returned ${r.status}`);
    return { ...(await r.json()), apiReachable: true };
  } catch (e) {
    return emptyOrgChart(projectId, String((e as Error)?.message ?? e));
  }
}
