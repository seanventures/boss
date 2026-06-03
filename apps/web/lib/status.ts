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

type ApiStatusResponse = {
  project_id: string;
  project_name: string;
  workspace_name: string;
  org_name: string;
  database_connected: boolean;
  api_reachable: boolean;
  budget_state: string;
  pending_decisions: number;
  open_tasks: number;
  blocked_reason?: string;
};

const API_URL =
  process.env.NEXT_PUBLIC_BOSS_API_URL?.replace(/\/$/, "") ??
  "http://127.0.0.1:8000";
const PROJECT_ID =
  process.env.NEXT_PUBLIC_BOSS_PROJECT_ID ?? "boss-dogfood";

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

export async function fetchStatus(): Promise<StatusCard> {
  try {
    const response = await fetch(
      `${API_URL}/v1/status?project_id=${encodeURIComponent(PROJECT_ID)}`,
      {
        cache: "no-store",
        headers: {
          Accept: "application/json",
        },
      },
    );

    if (!response.ok) {
      return placeholderStatus(`API returned ${response.status}`);
    }

    const payload = (await response.json()) as ApiStatusResponse;

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
  } catch (error) {
    return placeholderStatus(
      error instanceof Error ? error.message : "Unknown API error",
    );
  }
}
