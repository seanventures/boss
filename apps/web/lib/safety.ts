import { API_URL, PROJECT_ID, resolveProjectId, authHeaders } from "./client";

// Phase 1 safety/observability surfaces — read directly (not yet in the typed client).

export type PolicyEval = {
  id: string;
  boundary: string;
  action: string | null;
  decision: string; // allow | deny | defer
  reason: string | null;
  createdAt: string;
};

export async function fetchPolicyEvals(
  projectId = PROJECT_ID,
  limit = 25,
): Promise<PolicyEval[]> {
  try {
    const r = await fetch(
      `${API_URL}/v1/policy-evals?project_id=${encodeURIComponent(resolveProjectId(projectId))}&limit=${limit}`,
      { cache: "no-store", headers: authHeaders() },
    );
    if (!r.ok) return [];
    const items = (await r.json()).items ?? [];
    return items.map((it: Record<string, unknown>) => ({
      id: String(it.id),
      boundary: String(it.boundary),
      action: it.action != null ? String(it.action) : null,
      decision: String(it.decision),
      reason: it.reason != null ? String(it.reason) : null,
      createdAt: String(it.created_at ?? ""),
    }));
  } catch {
    return [];
  }
}

export type RunTraceEvent = {
  eventType: string;
  actor: string;
  createdAt: string;
};
export type RunTrace = {
  run: { id: string; status: string; phase: string; title: string; pullRequestUrl: string | null };
  qualityGate: { status: string; gatesPassed: string[]; gatesFailed: string[] } | null;
  timeline: RunTraceEvent[];
  eventCount: number;
} | null;

export async function fetchRunTrace(runId?: string): Promise<RunTrace> {
  if (!runId || !runId.trim()) return null;
  try {
    const r = await fetch(
      `${API_URL}/v1/trace?run_id=${encodeURIComponent(runId.trim())}`,
      { cache: "no-store", headers: authHeaders() },
    );
    if (!r.ok) return null;
    const d = await r.json();
    const g = d.quality_gate;
    return {
      run: {
        id: String(d.run.id),
        status: String(d.run.status),
        phase: String(d.run.phase ?? ""),
        title: String(d.run.title ?? ""),
        pullRequestUrl: d.run.pull_request_url ? String(d.run.pull_request_url) : null,
      },
      qualityGate: g
        ? {
            status: String(g.status),
            gatesPassed: (g.gates_passed ?? []) as string[],
            gatesFailed: (g.gates_failed ?? []) as string[],
          }
        : null,
      timeline: (d.timeline ?? []).map((e: Record<string, unknown>) => ({
        eventType: String(e.event_type),
        actor: String(e.actor ?? ""),
        createdAt: String(e.created_at ?? ""),
      })),
      eventCount: Number(d.event_count ?? 0),
    };
  } catch {
    return null;
  }
}
