import { API_URL, PROJECT_ID, resolveProjectId, authHeaders } from "./client";

// Phase 4 InfraCommitment reconciler (GET /v1/finops/commitment-reconciliations) — read directly.
// BOSS-244 multi-dimension summary (GET /v1/finops/summary) for MC charts.

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

export type FinopsSpendByProvider = {
  provider: string;
  costUsdMicros: number;
  eventCount: number;
};

export type FinopsSpendByDay = {
  day: string;
  costUsdMicros: number;
};

export type FinopsCommitmentUtilization = {
  commitmentId: string;
  provider: string;
  utilization: number;
  verdict: string;
};

export type FinopsTopResource = {
  resourceRef: string;
  provider: string;
  costUsdMicros: number;
};

export type FinopsSummary = {
  projectId: string;
  days: number;
  periodStart: string;
  periodEnd: string;
  spendByProvider: FinopsSpendByProvider[];
  spendByDay: FinopsSpendByDay[];
  anomaliesOpenCount: number;
  commitmentUtilization: FinopsCommitmentUtilization[];
  topResources: FinopsTopResource[];
  apiReachable: boolean;
};

const EMPTY_SUMMARY = (projectId: string, days: number): FinopsSummary => ({
  projectId,
  days,
  periodStart: "",
  periodEnd: "",
  spendByProvider: [],
  spendByDay: [],
  anomaliesOpenCount: 0,
  commitmentUtilization: [],
  topResources: [],
  apiReachable: false,
});

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

/** BOSS-244: multi-dimension FinOps chart payload (spend, anomalies, commitments, top resources). */
export async function fetchFinopsSummary(
  projectId = PROJECT_ID,
  days = 30,
): Promise<FinopsSummary> {
  const resolved = resolveProjectId(projectId);
  try {
    const r = await fetch(
      `${API_URL}/v1/finops/summary?project_id=${encodeURIComponent(resolved)}&days=${encodeURIComponent(String(days))}`,
      { cache: "no-store", headers: authHeaders() },
    );
    if (!r.ok) return EMPTY_SUMMARY(resolved, days);
    const body = (await r.json()) as Record<string, unknown>;
    const spendByProvider = Array.isArray(body.spend_by_provider)
      ? body.spend_by_provider.map((it: Record<string, unknown>) => ({
          provider: String(it.provider ?? ""),
          costUsdMicros: Number(it.cost_usd_micros ?? 0),
          eventCount: Number(it.event_count ?? 0),
        }))
      : [];
    const spendByDay = Array.isArray(body.spend_by_day)
      ? body.spend_by_day.map((it: Record<string, unknown>) => ({
          day: String(it.day ?? ""),
          costUsdMicros: Number(it.cost_usd_micros ?? 0),
        }))
      : [];
    const commitmentUtilization = Array.isArray(body.commitment_utilization)
      ? body.commitment_utilization.map((it: Record<string, unknown>) => ({
          commitmentId: String(it.commitment_id ?? ""),
          provider: String(it.provider ?? ""),
          utilization: Number(it.utilization ?? 0),
          verdict: String(it.verdict ?? ""),
        }))
      : [];
    const topResources = Array.isArray(body.top_resources)
      ? body.top_resources.map((it: Record<string, unknown>) => ({
          resourceRef: String(it.resource_ref ?? ""),
          provider: String(it.provider ?? ""),
          costUsdMicros: Number(it.cost_usd_micros ?? 0),
        }))
      : [];
    return {
      projectId: String(body.project_id ?? resolved),
      days: Number(body.days ?? days),
      periodStart: String(body.period_start ?? ""),
      periodEnd: String(body.period_end ?? ""),
      spendByProvider,
      spendByDay,
      anomaliesOpenCount: Number(body.anomalies_open_count ?? 0),
      commitmentUtilization,
      topResources,
      apiReachable: true,
    };
  } catch {
    return EMPTY_SUMMARY(resolved, days);
  }
}
