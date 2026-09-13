import { API_URL, PROJECT_ID, resolveProjectId, authHeaders } from "./client";

// Phase 4 chaos/load harness (GET /v1/chaos) — read directly.

export type ChaosRun = {
  id: string;
  scenario: string;
  injected: boolean;
  recovered: boolean;
  detail: string | null;
  createdAt: string;
};

export async function fetchChaosRuns(projectId = PROJECT_ID, limit = 15, active = true): Promise<ChaosRun[]> {
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return [];
  try {
    const r = await fetch(
      `${API_URL}/v1/chaos?project_id=${encodeURIComponent(resolveProjectId(projectId))}&limit=${limit}`,
      { cache: "no-store", headers: authHeaders() },
    );
    if (!r.ok) return [];
    const items = (await r.json()).items ?? [];
    return items.map((it: Record<string, unknown>) => ({
      id: String(it.id),
      scenario: String(it.scenario),
      injected: Boolean(it.injected),
      recovered: Boolean(it.recovered),
      detail: it.detail != null ? String(it.detail) : null,
      createdAt: String(it.created_at ?? ""),
    }));
  } catch {
    return [];
  }
}
