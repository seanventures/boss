import { API_URL, PROJECT_ID, resolveProjectId } from "./client";

export type CanaryResult = { canary: string; status: string; detail: string };
export type CanaryRun = {
  id: string;
  status: string;
  passed: number;
  failed: number;
  skipped: number;
  results: CanaryResult[];
  createdAt: string;
} | null;

// New endpoints aren't in the typed client yet — read directly.
export async function fetchLatestCanary(projectId = PROJECT_ID): Promise<CanaryRun> {
  try {
    const r = await fetch(
      `${API_URL}/v1/canaries?project_id=${encodeURIComponent(resolveProjectId(projectId))}&limit=1`,
      { cache: "no-store" },
    );
    if (!r.ok) return null;
    const items = (await r.json()).items ?? [];
    if (!items.length) return null;
    const it = items[0];
    return {
      id: String(it.id),
      status: String(it.status),
      passed: Number(it.passed ?? 0),
      failed: Number(it.failed ?? 0),
      skipped: Number(it.skipped ?? 0),
      results: (it.results ?? []) as CanaryResult[],
      createdAt: String(it.created_at ?? ""),
    };
  } catch {
    return null;
  }
}
