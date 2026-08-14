import { API_URL, PROJECT_ID, resolveProjectId, authHeaders } from "./client";

// Phase 3 software-factory scale-up — WIP/queue gating (GET /v1/factory/wip).

export const LIVE_PR_UNKNOWN_LABEL = "unknown / not proven";

export type LastSimulatedFixture = {
  runId: string;
  status: string;
  phase?: string | null;
  title?: string | null;
  createdAt?: string | null;
  costUsdMicros: number;
};

export type LivePrSuccess = {
  rate: number | null;
  label: string;
  proven: boolean;
  sampleSize: number;
  successes: number;
};

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
  lastSimulatedFixture?: LastSimulatedFixture | null;
  livePrSuccess?: LivePrSuccess;
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
    lastSimulatedFixture: null,
    livePrSuccess: {
      rate: null,
      label: LIVE_PR_UNKNOWN_LABEL,
      proven: false,
      sampleSize: 0,
      successes: 0,
    },
    apiReachable: false,
    blockedReason: reason,
  };
}

export function livePrSuccessCopy(wip: WipStatus): string {
  const label = wip.livePrSuccess?.label?.trim() || LIVE_PR_UNKNOWN_LABEL;
  // Never render a bare invented percent. The API sends a k/n label or unknown.
  if (/^\d+(\.\d+)?%$/.test(label)) {
    return `live PR success rate: ${LIVE_PR_UNKNOWN_LABEL}`;
  }
  return `live PR success rate: ${label}`;
}

export function lastSimulatedCopy(wip: WipStatus): string {
  const fixture = wip.lastSimulatedFixture;
  if (!fixture?.runId) {
    return "last simulated fixture: none";
  }
  const cost = Number.isFinite(fixture.costUsdMicros) ? fixture.costUsdMicros : 0;
  return `last simulated fixture: ${fixture.status} (${fixture.runId}, cost=${cost})`;
}

function mapLastSimulatedFixture(raw: unknown): LastSimulatedFixture | null {
  if (!raw || typeof raw !== "object") return null;
  const row = raw as Record<string, unknown>;
  const runId = typeof row.run_id === "string" ? row.run_id : "";
  if (!runId) return null;
  return {
    runId,
    status: typeof row.status === "string" ? row.status : "simulated",
    phase: typeof row.phase === "string" ? row.phase : null,
    title: typeof row.title === "string" ? row.title : null,
    createdAt: typeof row.created_at === "string" ? row.created_at : null,
    costUsdMicros: Number(row.cost_usd_micros ?? 0) || 0,
  };
}

function mapLivePrSuccess(raw: unknown): LivePrSuccess {
  const fallback: LivePrSuccess = {
    rate: null,
    label: LIVE_PR_UNKNOWN_LABEL,
    proven: false,
    sampleSize: 0,
    successes: 0,
  };
  if (!raw || typeof raw !== "object") return fallback;
  const row = raw as Record<string, unknown>;
  const label =
    typeof row.label === "string" && row.label.trim()
      ? row.label.trim()
      : LIVE_PR_UNKNOWN_LABEL;
  const rate = typeof row.rate === "number" && Number.isFinite(row.rate) ? row.rate : null;
  return {
    rate,
    label: /^\d+(\.\d+)?%$/.test(label) ? LIVE_PR_UNKNOWN_LABEL : label,
    proven: row.proven === true,
    sampleSize: Number(row.sample_size ?? 0) || 0,
    successes: Number(row.successes ?? 0) || 0,
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
      lastSimulatedFixture: mapLastSimulatedFixture(d.last_simulated_fixture),
      livePrSuccess: mapLivePrSuccess(d.live_pr_success),
      apiReachable: true,
    };
  } catch (error) {
    return emptyWipStatus(
      error instanceof Error ? error.message : "Factory WIP request failed",
      wipLimit,
    );
  }
}
