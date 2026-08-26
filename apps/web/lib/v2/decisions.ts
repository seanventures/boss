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
