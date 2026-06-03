import {
  BossApiError,
  type StatusResponse,
} from "@boss/api-client";
import { client, PROJECT_ID } from "./client";

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
  blockedReason?: string;
};

export function placeholderStatus(reason: string): StatusCard {
  return {
    projectId: PROJECT_ID,
    projectName: "BOSS dogfood bootstrap",
    workspaceName: "Awaiting API",
    orgName: "BOSS",
    databaseConnected: false,
    apiReachable: false,
    budgetState: "unknown",
    pendingDecisions: 0,
    openTasks: 0,
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
    blockedReason: payload.blocked_reason,
  };
}

export async function fetchStatus(): Promise<StatusCard> {
  try {
    return mapStatus(await client.getStatus(PROJECT_ID));
  } catch (error) {
    if (error instanceof BossApiError) {
      return placeholderStatus(`API returned ${error.status}`);
    }
    return placeholderStatus(
      error instanceof Error ? error.message : "Unknown API error",
    );
  }
}
