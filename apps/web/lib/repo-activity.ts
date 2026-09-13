import { API_URL, authHeaders, resolveProjectId } from "./client";

export type RepoRecentRun = {
  id: string;
  title: string;
  status: string;
  pr: number | null;
  pr_url: string | null;
  minutes: number | null;
  cost_usd: number;
  created_at: string;
};

export type RepoActivity = {
  owner: string;
  repo: string;
  full_name: string;
  window_days: number;
  runs_total: number;
  runs_completed: number;
  runs_failed: number;
  runs_active: number;
  prs_opened: number;
  prs_merged: number | null;
  merge_rate?: number | null;
  success_rate: number | null;
  first_pass_rate: number | null;
  median_minutes_to_pr: number | null;
  spend_usd: number;
  spend_per_pr_usd: number | null;
  tokens: number;
  last_activity_at: string | null;
  recent_runs: RepoRecentRun[];
};

/** What BOSS actually did in each repo. Never throws — the console must still render
 *  its other panels if this one endpoint is unavailable. */
export async function fetchRepoActivity(
  projectId?: string,
  days = 30,
  active = true,
): Promise<RepoActivity[]> {
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return [];
  const project = resolveProjectId(projectId);
  try {
    const response = await fetch(
      `${API_URL}/v1/github/repository-activity?project_id=${encodeURIComponent(
        project,
      )}&days=${days}`,
      { headers: authHeaders(), cache: "no-store" },
    );
    if (!response.ok) return [];
    const payload = await response.json();
    return (payload?.items ?? []) as RepoActivity[];
  } catch {
    return [];
  }
}
