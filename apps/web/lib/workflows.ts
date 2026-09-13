import {
  BossApiError,
  type WorkflowDefinitionSummary,
  type WorkflowMappingSummary,
  type WorkflowRunDetail,
  type WorkflowRunSummary,
} from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type WorkflowFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  definitions: WorkflowDefinitionSummary[];
  mappings: WorkflowMappingSummary[];
  runs: WorkflowRunSummary[];
  selectedRun?: WorkflowRunDetail;
};

export function emptyWorkflowFeed(
  reason?: string,
  projectId = PROJECT_ID,
): WorkflowFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    definitions: [],
    mappings: [],
    runs: [],
  };
}

export async function fetchWorkflowFeed(
  projectId = PROJECT_ID,
  active = true,
): Promise<WorkflowFeed> {
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return emptyWorkflowFeed("not requested", resolveProjectId(projectId));
  const resolved = resolveProjectId(projectId);
  try {
    const [definitions, mappings, runs] = await Promise.all([
      client.getWorkflowDefinitions(resolved),
      client.getWorkflowMappings(resolved),
      client.getWorkflowRuns(resolved, undefined, 25),
    ]);
    const selectedRun = runs.items[0]
      ? await client.getWorkflowRun(runs.items[0].id, resolved)
      : undefined;
    return {
      projectId: resolved,
      apiReachable: true,
      definitions: definitions.items,
      mappings: mappings.items,
      runs: runs.items,
      selectedRun,
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyWorkflowFeed(`API returned ${error.status}`, resolved);
    }
    return emptyWorkflowFeed(
      error instanceof Error ? error.message : "Unknown API error",
      resolved,
    );
  }
}
