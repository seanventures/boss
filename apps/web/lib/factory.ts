import { API_URL, PROJECT_ID, resolveProjectId, authHeaders } from "./client";

// Phase 3 software-factory scale-up — WIP/queue gating (GET /v1/factory/wip).

export type WipStatus = {
  inFlight: number;
  queued: number;
  wipLimit: number;
  availableSlots: number;
  admit: boolean;
  reason: string;
  fixtureSupported: boolean;
  githubPlane?: string | null;
  githubBindingId?: string | null;
  githubConnectionId?: string | null;
  githubSource?: string | null;
  githubFullName?: string | null;
  githubPlaneReason?: string | null;
  apiReachable: boolean;
  blockedReason?: string;
};

export function emptyWipStatus(reason: string, wipLimit = 5): WipStatus {
  return {
    inFlight: 0,
    queued: 0,
    wipLimit,
    availableSlots: 0,
    admit: false,
    reason: "unavailable",
    fixtureSupported: false,
    apiReachable: false,
    blockedReason: reason,
  };
}

export async function fetchWipStatus(projectId = PROJECT_ID, wipLimit = 5): Promise<WipStatus> {
  try {
    const r = await fetch(
      `${API_URL}/v1/factory/wip?project_id=${encodeURIComponent(resolveProjectId(projectId))}&wip_limit=${wipLimit}`,
      { cache: "no-store", headers: authHeaders() },
    );
    if (!r.ok) {
      return emptyWipStatus(`API returned ${r.status}`, wipLimit);
    }
    const d = await r.json();
    return {
      inFlight: Number(d.in_flight ?? 0),
      queued: Number(d.queued ?? 0),
      wipLimit: Number(d.wip_limit ?? wipLimit),
      availableSlots: Number(d.available_slots ?? 0),
      admit: Boolean(d.admit),
      reason: String(d.reason ?? ""),
      fixtureSupported: d.fixture_supported === true,
      githubPlane: typeof d.github_plane === "string" ? d.github_plane : null,
      githubBindingId: typeof d.github_binding_id === "string" ? d.github_binding_id : null,
      githubConnectionId:
        typeof d.github_connection_id === "string" ? d.github_connection_id : null,
      githubSource: typeof d.github_source === "string" ? d.github_source : null,
      githubFullName: typeof d.github_full_name === "string" ? d.github_full_name : null,
      githubPlaneReason:
        typeof d.github_plane_reason === "string"
          ? d.github_plane_reason
          : typeof d.github_plane_error === "string"
            ? d.github_plane_error
            : null,
      apiReachable: true,
    };
  } catch (error) {
    return emptyWipStatus(
      error instanceof Error ? error.message : "Factory WIP request failed",
      wipLimit,
    );
  }
}
