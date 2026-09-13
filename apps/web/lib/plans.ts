import { API_URL, PROJECT_ID, resolveProjectId, authHeaders} from "./client";

export type PlanItem = {
  id: string;
  title: string;
  objective: string;
  status: string;
  phaseCount: number;
};

export async function fetchPlans(projectId = PROJECT_ID, active = true): Promise<PlanItem[]> {
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return [];
  try {
    const r = await fetch(
      `${API_URL}/v1/plans?project_id=${encodeURIComponent(resolveProjectId(projectId))}&limit=20`,
      { cache: "no-store", headers: authHeaders() },
    );
    if (!r.ok) return [];
    const items = (await r.json()).items ?? [];
    return items.map((p: Record<string, unknown>) => ({
      id: String(p.id),
      title: String(p.title),
      objective: String(p.objective ?? ""),
      status: String(p.status),
      phaseCount: Array.isArray(p.phases) ? p.phases.length : 0,
    }));
  } catch {
    return [];
  }
}
