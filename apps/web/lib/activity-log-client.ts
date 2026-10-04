import {
  activityLogSearchParams,
  emptyActivityLog,
  mapActivityFacets,
  mapActivityLog,
  type ActivityFacets,
  type ActivityLog,
  type ActivityLogQuery,
} from "./activity-log";
import { resolveProjectId } from "./client";

/**
 * Load the activity log from the browser, through a route handler.
 *
 * `loadActivityLogAction` did the same work as a Server Action, and that reply path cost a
 * full re-render of the console route. Measured live: this one call took 25.3 seconds while
 * the other fourteen actions on the page finished in under 300ms between them. About 14s of
 * that was the query (fixed in activity_log.py); the rest was the reply.
 *
 * The URL is built with the same `activityLogSearchParams` the action used and the response
 * goes through the same `mapActivityLog`, so the filter vocabulary and the shape of an
 * ActivityLog stay defined in exactly one place.
 */
export async function fetchActivityLog(
  projectId: string,
  query: ActivityLogQuery = {},
): Promise<ActivityLog> {
  const project = resolveProjectId(projectId);
  const params = activityLogSearchParams(project, query);
  try {
    const res = await fetch(`/api/activity?${params.toString()}`, { cache: "no-store" });
    if (!res.ok) {
      // Distinguishable from "no activity": an empty log with a reason renders as an error in
      // the panel, not as a project that has done nothing.
      return emptyActivityLog(`API returned ${res.status}`, project);
    }
    return mapActivityLog(await res.json(), project);
  } catch (error) {
    return emptyActivityLog(
      error instanceof Error ? error.message : "Unknown API error",
      project,
    );
  }
}

/**
 * Per-level and per-stream counts for the whole log, for the chips.
 *
 * The chips counted the 200 rows on the page and read as totals: boss-1 showed ERROR 0 and
 * Deadletters 0 while Operations reported 14 dead-letters. null when the counts could not be
 * read, so the chips can say "count unavailable" instead of showing a zero that is not one.
 */
export async function fetchActivityFacets(
  projectId: string,
  scope: { run_id?: string; workflow_run_id?: string; step_id?: string; agent?: string; service?: string } = {},
): Promise<ActivityFacets | null> {
  const params = new URLSearchParams({ project_id: resolveProjectId(projectId) });
  for (const [key, value] of Object.entries(scope)) if (value) params.set(key, value);
  try {
    const res = await fetch(`/api/activity/facets?${params.toString()}`, { cache: "no-store" });
    if (!res.ok) return null;
    return mapActivityFacets(await res.json());
  } catch {
    return null;
  }
}
