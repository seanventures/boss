import { API_URL, PROJECT_ID, resolveProjectId, authHeaders } from "./client";

// Phase 4 InfraCommitment reconciler (GET /v1/finops/commitment-reconciliations) — read directly.

export type CommitmentReconciliation = {
  id: string;
  commitmentId: string;
  provider: string;
  committedUsdMicros: number;
  actualUsdMicros: number;
  utilization: number;
  verdict: string; // on_track | under_utilized | over_utilized
  decisionId: string | null;
  createdAt: string;
};

export async function fetchCommitmentReconciliations(
  projectId = PROJECT_ID,
  limit = 20,
): Promise<CommitmentReconciliation[]> {
  try {
    const r = await fetch(
      `${API_URL}/v1/finops/commitment-reconciliations?project_id=${encodeURIComponent(resolveProjectId(projectId))}&limit=${limit}`,
      { cache: "no-store", headers: authHeaders() },
    );
    if (!r.ok) return [];
    const items = (await r.json()).items ?? [];
    return items.map((it: Record<string, unknown>) => ({
      id: String(it.id),
      commitmentId: String(it.commitment_id),
      provider: String(it.provider),
      committedUsdMicros: Number(it.committed_usd_micros ?? 0),
      actualUsdMicros: Number(it.actual_usd_micros ?? 0),
      utilization: Number(it.utilization ?? 0),
      verdict: String(it.verdict),
      decisionId: it.decision_id != null ? String(it.decision_id) : null,
      createdAt: String(it.created_at ?? ""),
    }));
  } catch {
    return [];
  }
}
