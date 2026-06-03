import {
  BossApiError,
  type StatusResponse,
} from "@boss/api-client";
import { client, PROJECT_ID, resolveProjectId } from "./client";

export type StatusCard = {
  projectId: string;
  projectName: string;
  workspaceName: string;
  orgName: string;
  databaseConnected: boolean;
  apiReachable: boolean;
  budgetState: string;
  pendingDecisions: number;
  openTasks: number;
  deadletters: number;
  activeJobs: number;
  topRisks: Array<Record<string, unknown>>;
  latestSnapshotId?: string;
  latestSnapshotAt?: string;
  blockedReason?: string;
};

export function placeholderStatus(reason: string, projectId = PROJECT_ID): StatusCard {
  return {
    projectId: resolveProjectId(projectId),
    projectName: resolveProjectId(projectId),
    workspaceName: "Awaiting API",
    orgName: "Unknown org",
    databaseConnected: false,
    apiReachable: false,
    budgetState: "unknown",
    pendingDecisions: 0,
    openTasks: 0,
    deadletters: 0,
    activeJobs: 0,
    topRisks: [],
    latestSnapshotId: undefined,
    latestSnapshotAt: undefined,
    blockedReason: reason,
  };
}

function mapStatus(payload: StatusResponse): StatusCard {
  return {
    projectId: payload.project_id,
    projectName: payload.project_name,
    workspaceName: payload.workspace_name,
    orgName: payload.org_name,
    databaseConnected: payload.database_connected,
    apiReachable: payload.api_reachable,
    budgetState: payload.budget_state,
    pendingDecisions: payload.pending_decisions,
    openTasks: payload.open_tasks,
    deadletters: payload.deadletters,
    activeJobs: payload.active_jobs,
    topRisks: payload.top_risks,
    latestSnapshotId: payload.latest_snapshot_id,
    latestSnapshotAt: payload.latest_snapshot_at,
    blockedReason: payload.blocked_reason,
  };
}

export async function fetchStatus(projectId = PROJECT_ID): Promise<StatusCard> {
  try {
    return mapStatus(await client.getStatus(resolveProjectId(projectId)));
  } catch (error) {
    if (error instanceof BossApiError) {
      return placeholderStatus(`API returned ${error.status}`, projectId);
    }
    return placeholderStatus(
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}
