import { API_URL, PROJECT_ID, resolveProjectId, authHeaders } from "./client";

// Consolidated real-time dashboard (Phase 4: GET /v1/dashboard) — ties every phase together.
// Read directly (not yet in the typed client), like the other Phase 2-4 surfaces.

export type DashboardSnapshot = {
  cursor: string;
  budgetState: string;
  queue: { queued: number; leased: number; retryable: number; succeeded: number };
  workload: { openTasks: number; completedTasks: number; pendingDecisions: number; highRiskPending: number };
  incidents: { deadletters: number; blockedOutbox: number; deadletteredOutbox: number };
  providerHealth: { total: number; healthy: number };
  canary: { status: string; passed: number; failed: number; skipped: number } | null;
  evals: { status: string; passed: number; failed: number } | null;
  spend: { capUsdMicros: number; reservedUsdMicros: number; actualUsdMicros: number; runRate1hUsdMicros: number };
  ackBacklog: number;
};

export type DashboardFeed = {
  snapshot: DashboardSnapshot | null;
  apiReachable: boolean;
  blockedReason?: string;
};

export function emptyDashboardFeed(reason: string): DashboardFeed {
  return { snapshot: null, apiReachable: false, blockedReason: reason };
}

export async function fetchDashboard(projectId = PROJECT_ID, active = true): Promise<DashboardFeed> {
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return emptyDashboardFeed("not requested");
  try {
    const r = await fetch(
      `${API_URL}/v1/dashboard?project_id=${encodeURIComponent(resolveProjectId(projectId))}`,
      { cache: "no-store", headers: authHeaders() },
    );
    if (!r.ok) return emptyDashboardFeed(`API returned ${r.status}`);
    const d = await r.json();
    const q = d.queue_state ?? {};
    const w = d.workload_state ?? {};
    const i = d.incident_state ?? {};
    const p = d.provider_health ?? {};
    const s = d.spend ?? {};
    return {
      apiReachable: true,
      snapshot: {
        cursor: String(d.cursor ?? ""),
        budgetState: String(d.budget_state ?? "unknown"),
        queue: {
          queued: Number(q.queued_jobs ?? 0),
          leased: Number(q.leased_jobs ?? 0),
          retryable: Number(q.retryable_jobs ?? 0),
          succeeded: Number(q.succeeded_jobs ?? 0),
        },
        workload: {
          openTasks: Number(w.open_tasks ?? 0),
          completedTasks: Number(w.completed_tasks ?? 0),
          pendingDecisions: Number(w.pending_decisions ?? 0),
          highRiskPending: Number(w.high_risk_pending_decisions ?? 0),
        },
        incidents: {
          deadletters: Number(i.deadletters ?? 0),
          blockedOutbox: Number(i.blocked_outbox_messages ?? 0),
          deadletteredOutbox: Number(i.deadlettered_outbox_messages ?? 0),
        },
        providerHealth: { total: Number(p.total ?? 0), healthy: Number(p.healthy ?? 0) },
        canary: d.canary
          ? {
              status: String(d.canary.status),
              passed: Number(d.canary.passed ?? 0),
              failed: Number(d.canary.failed ?? 0),
              skipped: Number(d.canary.skipped ?? 0),
            }
          : null,
        evals: d.eval
          ? {
              status: String(d.eval.status),
              passed: Number(d.eval.passed ?? 0),
              failed: Number(d.eval.failed ?? 0),
            }
          : null,
        spend: {
          capUsdMicros: Number(s.cap_usd_micros ?? 0),
          reservedUsdMicros: Number(s.reserved_usd_micros ?? 0),
          actualUsdMicros: Number(s.actual_usd_micros ?? 0),
          runRate1hUsdMicros: Number(s.run_rate_1h_usd_micros ?? 0),
        },
        ackBacklog: Number(d.ack_backlog ?? 0),
      },
    };
  } catch (error) {
    return emptyDashboardFeed(
      error instanceof Error ? error.message : "Dashboard request failed",
    );
  }
}
