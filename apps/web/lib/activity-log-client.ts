import {
  activityLogSearchParams,
  emptyActivityLog,
  mapActivityLog,
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
