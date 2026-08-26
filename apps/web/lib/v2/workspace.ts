import { API_URL, authHeaders } from "../client";

/**
 * One app's workspace, in one call.
 *
 * The v1 equivalent of this screen was assembled from the full 82-feed page render, so opening
 * a single app cost the same as opening everything. Budget here: TTFB <= 2s.
 */

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
