"use server";

import {
  activityLogSearchParams,
  emptyActivityLog,
  mapActivityLog,
  type ActivityLog,
  type ActivityLogQuery,
} from "../lib/activity-log";
import { API_URL, apiUrlProblem, authHeaders, resolveProjectId } from "../lib/client";

export async function loadActivityLogAction(
  projectId: string,
  query: ActivityLogQuery = {},
): Promise<ActivityLog> {
  const project = resolveProjectId(projectId);
  const misconfigured = apiUrlProblem();
  if (misconfigured) {
    return emptyActivityLog(misconfigured, project);
  }

  const params = activityLogSearchParams(project, query);
  try {
    const response = await fetch(`${API_URL}/v1/activity?${params.toString()}`, {
      headers: authHeaders({ "Content-Type": "application/json" }),
      cache: "no-store",
    });
    if (!response.ok) {
      return emptyActivityLog(`API returned ${response.status}`, project);
    }
    const payload: unknown = await response.json();
    return mapActivityLog(payload, project);
  } catch (error) {
    return emptyActivityLog(
      error instanceof Error ? error.message : "Unknown API error",
      project,
    );
  }
}
