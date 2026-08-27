import { API_URL, authHeaders } from "../client";

/**
 * One app's workspace, in one call.
 *
 * The v1 equivalent of this screen was assembled from the full 82-feed page render, so opening
 * a single app cost the same as opening everything. Budget here: TTFB <= 2s.
 */

export type V2Step = {
  /** The pipeline stage — spec, plan, implement, review_code, check_tests. */
  stage: string;
  model: string | null;
  engine: string | null;
  tokens: number;
  spend_usd: number;
  latency_ms: number;
  /** Per CALL, not per stage. The earlier per-stage aggregate discarded these three, and on a
   *  failed attempt the error is the only thing worth opening the run for. */
  turns: number;
  retries: number;
  ok: boolean;
  error: string | null;
};

/** One quality check. `ok: null` means it did not run — which is neither a pass nor a failure,
 *  and rendering it as either invents a result nobody measured. */
export type V2Gate = {
  label: string;
  ok: boolean | null;
  skipped: boolean;
  note: string | null;
};

export type V2Provenance = {
  build_engine: string | null;
  build_model: string | null;
  review_engine: string | null;
  review_model: string | null;
  /** Whether a different model reviewed the build. The single fact that most changes what a
   *  green review is worth. */
  cross_model: boolean;
  reviewer_independent: boolean;
  test_author_independent: boolean;
  profile: string | null;
  risk_tier: number | null;
};

export type V2Evidence = {
  key: string;
  title: string;
  /** Markdown — the reviewer's own words. */
  text: string;
  truncated: boolean;
};

export type V2Run = {
  id: string;
  status: string;
  phase: string;
  attempt: number;
  turns: number;
  spend_usd: number;
  pull_request_number: number | null;
  pull_request_url: string | null;
  error: string | null;
  created_at: string | null;
  /** The steps this attempt ran, in order, each with the model that ran it and what it cost.
   *  A card saying "failed" and nothing else is a dead end — the only move left is to go and
   *  read logs. */
  steps: V2Step[];
  latency_ms: number;
  gates?: V2Gate[];
  provenance?: V2Provenance;
  evidence?: V2Evidence[];
  impl_files?: string[];
  test_files?: string[];
  phase_timings?: { phase: string; ms: number }[];
  /** A first-pass build ships with failing checks marked advisory. Saying so is the difference
   *  between "this passed" and "we let this through on purpose". */
  advisory?: boolean;
};

export type V2Phase = { id: string; reached: boolean; label: string };

export type Workspace = {
  found: boolean;
  app_id: string;
  project_id: string;
  title?: string;
  description?: string;
  repo?: string;
  stage?: string;
  status?: string;
  pct?: number;
  spend_usd?: number;
  runs?: V2Run[];
  /** The contract the work was accepted against. Without it the quality checks are marks with
   *  no exam paper. */
  criteria?: string[];
  verification?: string[];
  deployable?: boolean;
  deployable_reason?: string;
  latest_run_id?: string;
  phases?: V2Phase[];
  /** Set only when the control plane could not be reached. Distinct from found=false, which
   *  is a real answer meaning no such app. */
  blockedReason?: string;
};

export async function fetchWorkspace(projectId: string, appId: string): Promise<Workspace> {
  try {
    const r = await fetch(
      `${API_URL}/v1/console/v2/workspace?project_id=${encodeURIComponent(projectId)}` +
        `&app_id=${encodeURIComponent(appId)}`,
      { headers: authHeaders({}), cache: "no-store" },
    );
    if (!r.ok) {
      return { found: false, app_id: appId, project_id: projectId,
               blockedReason: `API returned ${r.status}` };
    }
    return (await r.json()) as Workspace;
  } catch (e) {
    return { found: false, app_id: appId, project_id: projectId,
             blockedReason: String((e as Error)?.message ?? e) };
  }
}
