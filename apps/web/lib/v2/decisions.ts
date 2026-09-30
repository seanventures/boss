import { API_URL, authHeaders } from "../client";

/**
 * The gates waiting on a human.
 *
 * A decision carries a deadline and a default action, so an unanswered gate does not wait — it
 * resolves itself. `if_ignored` is the sentence that says which, per row, because "waits for
 * you" and "approves in two hours" render identically as a pending row and mean opposite
 * things.
 */

export type V2Decision = {
  id: string;
  task_id: string | null;
  question: string;
  recommendation: string;
  options: string[];
  risk_tier: number;
  status: string;
  delegated_to: string | null;
  deadline_at: string | null;
  default_action: string | null;
  if_ignored: string;
  created_at: string | null;
};

export type DecisionQueue = {
  project_id: string;
  items: V2Decision[];
  total: number;
  /** How many will be decided WITHOUT you if nobody acts. A different kind of urgent. */
  auto_resolving: number;
  apiReachable: boolean;
  blockedReason?: string;
};

export function emptyQueue(projectId: string, reason: string): DecisionQueue {
  return {
    project_id: projectId,
    items: [],
    total: 0,
    auto_resolving: 0,
    apiReachable: false,
    blockedReason: reason,
  };
}

export async function fetchDecisionQueue(projectId: string): Promise<DecisionQueue> {
  try {
    const r = await fetch(
      `${API_URL}/v1/console/v2/decisions?project_id=${encodeURIComponent(projectId)}`,
      { headers: authHeaders({}), cache: "no-store" },
    );
    if (!r.ok) return emptyQueue(projectId, `API returned ${r.status}`);
    return { ...(await r.json()), apiReachable: true };
  } catch (e) {
    return emptyQueue(projectId, String((e as Error)?.message ?? e));
  }
}


/**
 * Panels — who is debating what, and what they concluded.
 *
 * `streamed` is false and stays false: BOSS's panels produce real minutes but are not watched
 * live. The screen must not animate a debate nobody is watching.
 */
export type V2Turn = {
  speaker: string;
  phase: string;
  text: string;
  /** null unless the transcript recorded one. BOSS does not, and inventing FOR/AGAINST would
   *  put words in an agent's mouth on the record. */
  stance: "FOR" | "AGAINST" | null;
  /** True when the argument was longer than travels to the console. Shown, because an argument
   *  cut in silence reads as an argument that finished. */
  truncated?: boolean;
};

export type V2Panel = {
  id: string;
  title: string;
  purpose: string;
  status: string;
  deliberating: boolean;
  panelists: string[];
  decisions: string[];
  /** Recorded objections. A panel that agreed and a panel that overruled an objection produce
   *  the same decision and are not the same event. */
  dissent: string[];
  blocked: boolean;
  blocked_reason: string | null;
  has_minutes: boolean;
  completed_at: string | null;
  created_at: string | null;
  /** What was actually said, per speaker, across both rounds. Read from the MINUTES, where a
   *  real BOSS meeting keeps its reasoning — the transcript path is only taken by simulated
   *  meetings, and reading it alone reported "no transcript" on twenty real sessions. */
  turns: V2Turn[];
  /** What the panel agreed, in its own words. */
  synthesis: string | null;
};

export type PanelFeed = {
  project_id: string;
  sessions: V2Panel[];
  deliberating: number;
  blocked: number;
  streamed: boolean;
  stream_note: string;
  apiReachable: boolean;
  blockedReason?: string;
};

export function emptyPanels(projectId: string, reason: string): PanelFeed {
  return {
    project_id: projectId, sessions: [], deliberating: 0, blocked: 0,
    streamed: false, stream_note: "", apiReachable: false, blockedReason: reason,
  };
}

export async function fetchPanels(projectId: string): Promise<PanelFeed> {
  try {
    const r = await fetch(
      `${API_URL}/v1/console/v2/panels?project_id=${encodeURIComponent(projectId)}`,
      { headers: authHeaders({}), cache: "no-store" },
    );
    if (!r.ok) return emptyPanels(projectId, `API returned ${r.status}`);
    return { ...(await r.json()), apiReachable: true };
  } catch (e) {
    return emptyPanels(projectId, String((e as Error)?.message ?? e));
  }
}
