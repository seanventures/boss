import { API_URL, authHeaders } from "../client";

/**
 * The one call behind the v2 shell and Projects screen.
 *
 * v1 issues 82 requests per render and shows nothing until the slowest answers — measured
 * TTFB 24s, of which 13.2s was one feed only a single area displayed. v2 screens each get a
 * purpose-built endpoint so a page is bounded by its own query set, not by its unluckiest
 * dependency. Budget: TTFB <= 2s, enforced by test.
 */

export type V2Status = "RUNNING" | "GATED" | "DONE" | "STUCK" | "IDLE";
export type V2Stage = "DISCOVERY" | "BUILD" | "REVIEW" | "SHIPPED";

export type V2Project = {
  id: string;
  title: string;
  description: string;
  status: V2Status;
  stage: V2Stage;
  pct: number;
  runs: number;
  steps: number;
  spend_usd: number;
  repo: string;
  latest_run_id: string;
  pull_request_number: number | null;
  updated_at: string | null;
};

export type CompanyOverview = {
  company: { id: string; name: string };
  burn: { spent_usd: number; cap_usd: number | null };
  gates: number;
  projects: V2Project[];
  autonomy: { mode: string; detail: string; modes_available: string[] };
  apiReachable: boolean;
  blockedReason?: string;
};

/** Shown when the control plane cannot be reached. Never invented numbers: zeros that look
 *  like facts are how an outage gets read as an empty company. */
export function emptyOverview(projectId: string, reason: string): CompanyOverview {
  return {
    company: { id: projectId, name: projectId },
    burn: { spent_usd: 0, cap_usd: null },
    gates: 0,
    projects: [],
    autonomy: { mode: "Supervised", detail: "Spend & deploys pause for you.", modes_available: [] },
    apiReachable: false,
    blockedReason: reason,
  };
}

export async function fetchCompanyOverview(projectId: string): Promise<CompanyOverview> {
  try {
    const r = await fetch(
      `${API_URL}/v1/console/v2/overview?project_id=${encodeURIComponent(projectId)}`,
      { headers: authHeaders({}), cache: "no-store" },
    );
    if (!r.ok) return emptyOverview(projectId, `API returned ${r.status}`);
    const d = (await r.json()) as Omit<CompanyOverview, "apiReachable">;
    return { ...d, apiReachable: true };
  } catch (e) {
    return emptyOverview(projectId, String((e as Error)?.message ?? e));
  }
}
