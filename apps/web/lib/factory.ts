import { API_URL, PROJECT_ID, resolveProjectId, authHeaders } from "./client";

// Phase 3 software-factory scale-up — WIP/queue gating (GET /v1/factory/wip), read directly.

export type WipStatus = {
  inFlight: number;
  queued: number;
  wipLimit: number;
  availableSlots: number;
  admit: boolean;
  reason: string;
} | null;

export async function fetchWipStatus(projectId = PROJECT_ID, wipLimit = 5): Promise<WipStatus> {
  try {
    const r = await fetch(
      `${API_URL}/v1/factory/wip?project_id=${encodeURIComponent(resolveProjectId(projectId))}&wip_limit=${wipLimit}`,
      { cache: "no-store", headers: authHeaders() },
    );
    if (!r.ok) return null;
    const d = await r.json();
    return {
      inFlight: Number(d.in_flight ?? 0),
      queued: Number(d.queued ?? 0),
      wipLimit: Number(d.wip_limit ?? wipLimit),
      availableSlots: Number(d.available_slots ?? 0),
      admit: Boolean(d.admit),
      reason: String(d.reason ?? ""),
    };
  } catch {
    return null;
  }
}
