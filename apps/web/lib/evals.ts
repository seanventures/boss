import { API_URL, PROJECT_ID, resolveProjectId, authHeaders } from "./client";

// Phase 2 eval-harness (GET /v1/evals) — read directly, mirrors the canary feed.

export type EvalResult = { eval: string; status: string; detail: string };
export type EvalRun = {
  id: string;
  suite: string;
  status: string;
  passed: number;
  failed: number;
  results: EvalResult[];
  createdAt: string;
} | null;

export async function fetchLatestEval(projectId = PROJECT_ID): Promise<EvalRun> {
  try {
    const r = await fetch(
      `${API_URL}/v1/evals?project_id=${encodeURIComponent(resolveProjectId(projectId))}&limit=1`,
      { cache: "no-store", headers: authHeaders() },
    );
    if (!r.ok) return null;
    const items = (await r.json()).items ?? [];
    if (!items.length) return null;
    const it = items[0];
    return {
      id: String(it.id),
      suite: String(it.suite ?? "all"),
      status: String(it.status),
      passed: Number(it.passed ?? 0),
      failed: Number(it.failed ?? 0),
      results: (it.results ?? []) as EvalResult[],
      createdAt: String(it.created_at ?? ""),
    };
  } catch {
    return null;
  }
}
