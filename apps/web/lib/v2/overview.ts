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
  autonomy: {
    mode: string;
    detail: string;
    modes_available: string[];
    threshold_usd?: number;
    set_by?: string | null;
    set_at?: string | null;
  };
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

/**
 * THE OVERVIEW IS ALWAYS WHOLE. The API's composite view leaves out a section the caller may not
 * read (a custom role without budget:read gets no `burn`), and the Shell read
 * `overview.burn.spent_usd` — every v2 screen "hit an error" for that role (round 3 role walk).
 * A missing section is the empty one; it never invents a number (cap stays null, burn 0 is shown
 * as —/0 only where the empty overview already does).
 */
export function overviewFrom(projectId: string, raw: unknown): CompanyOverview {
  const empty = emptyOverview(projectId, "");
  const d = (raw && typeof raw === "object" ? raw : {}) as Partial<CompanyOverview>;
  return {
    company: d.company && typeof d.company === "object" ? { ...empty.company, ...d.company } : empty.company,
    burn: d.burn && typeof d.burn === "object" ? { ...empty.burn, ...d.burn } : empty.burn,
    gates: typeof d.gates === "number" ? d.gates : 0,
    projects: Array.isArray(d.projects) ? d.projects : [],
    autonomy: d.autonomy && typeof d.autonomy === "object" ? { ...empty.autonomy, ...d.autonomy } : empty.autonomy,
    apiReachable: true,
    ...(d.blockedReason ? { blockedReason: d.blockedReason } : {}),
  };
}

export async function fetchCompanyOverview(projectId: string): Promise<CompanyOverview> {
  try {
    const r = await fetch(
      `${API_URL}/v1/console/v2/overview?project_id=${encodeURIComponent(projectId)}`,
      { headers: authHeaders({}), cache: "no-store" },
    );
    if (!r.ok) return emptyOverview(projectId, `API returned ${r.status}`);
    return overviewFrom(projectId, await r.json());
  } catch (e) {
    return emptyOverview(projectId, String((e as Error)?.message ?? e));
  }
}
