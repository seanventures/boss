import { API_URL, authHeaders } from "../client";

/**
 * What BOSS actually did, with the evidence attached.
 *
 * The field names are deliberate. `window_total` and `shown` are separate because a tile on
 * the old console read "Verified today: 100" — which was the page size, not a fact about the
 * world. Nothing here is called plain `total`, so there is no unqualified number to misread.
 */

export type V2Receipt = {
  id: string;
  effect: string;
  scope: string;
  status: string;
  /** Recorded as started and never resolved — which is what an API restart mid-effect leaves
   *  behind. Not a success, and not a failure either. */
  unfinished: boolean;
  provider_ref: string | null;
  error: string | null;
  data_boundary: string;
  created_at: string | null;
  /** CODE / DEPLOY / SPEND / GATE / PANEL / PROCESS / RESEARCH / SYSTEM / OTHER. */
  family: string;
  /** BOSS keeping itself alive — a tick, a watch, an overwatch. A real effect, but not work
   *  anyone asked for, and it outnumbers the real work about ten to one. */
  routine: boolean;
  /** What happened, in words. The dotted identifier is precise and unreadable; both matter. */
  summary: string;
  /** The run this effect belongs to, when its scope names one. */
  run_id: string | null;
};

export type ReceiptFeed = {
  /** How many of the shown rows are BOSS's own heartbeat. */
  shown_routine?: number;
  project_id: string;
  window_hours: number;
  items: V2Receipt[];
  window_total: number;
  window_unfinished: number;
  window_failed: number;
  shown: number;
  apiReachable: boolean;
  blockedReason?: string;
};

export function emptyReceipts(projectId: string, reason: string): ReceiptFeed {
  return {
    project_id: projectId,
    window_hours: 24,
    items: [],
    window_total: 0,
    window_unfinished: 0,
    window_failed: 0,
    shown: 0,
    apiReachable: false,
    blockedReason: reason,
  };
}

export async function fetchReceipts(projectId: string, hours = 24): Promise<ReceiptFeed> {
  try {
    const r = await fetch(
      `${API_URL}/v1/console/v2/receipts?project_id=${encodeURIComponent(projectId)}&hours=${hours}`,
      { headers: authHeaders({}), cache: "no-store" },
    );
    if (!r.ok) return emptyReceipts(projectId, `API returned ${r.status}`);
    return { ...(await r.json()), apiReachable: true };
  } catch (e) {
    return emptyReceipts(projectId, String((e as Error)?.message ?? e));
  }
}
