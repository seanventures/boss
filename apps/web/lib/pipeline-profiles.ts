import { API_URL, PROJECT_ID, resolveProjectId } from "./client";

export type PipelineProfile = {
  name: string;
  label: string;
  tier_floor: number;
  tier_cap: number;
  model: Record<string, string>;
  caching: boolean;
  parallel_checks: boolean;
  is_default: boolean;
  custom: boolean;
};

export type PipelineRunCost = {
  id: string;
  title: string;
  status: string;
  profile?: string;
  risk_tier?: number;
  steps_run?: string[];
  cost_usd_micros?: number;
  stage_costs?: Record<string, number>;
};

export type PipelineData = {
  projectId: string;
  apiReachable: boolean;
  defaultProfile: string;
  profiles: PipelineProfile[];
  recentRuns: PipelineRunCost[];
};

export function emptyPipelineData(projectId = PROJECT_ID): PipelineData {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    defaultProfile: "balanced",
    profiles: [],
    recentRuns: [],
  };
}

// The steps that run live today, by tier (the runtime currently runs the core separation).
export function stepsForTier(tier: number): string[] {
  const steps: string[] = [];
  if (tier >= 1) steps.push("write tests");
  steps.push("implement");
  if (tier >= 2) steps.push("review code");
  return steps;
}

// What a standard task resolves to under a profile (clamped to its tier range).
export function effectiveTier(profile: PipelineProfile, baseTier = 2): number {
  return Math.max(profile.tier_floor, Math.min(profile.tier_cap, baseTier));
}

export async function fetchPipelineData(projectId = PROJECT_ID): Promise<PipelineData> {
  const resolved = resolveProjectId(projectId);
  try {
    const pr = await fetch(
      `${API_URL}/v1/pipeline-profiles?project_id=${encodeURIComponent(resolved)}`,
      { cache: "no-store" },
    );
    if (!pr.ok) return emptyPipelineData(resolved);
    const pdata = await pr.json();

    let recentRuns: PipelineRunCost[] = [];
    try {
      const rr = await fetch(
        `${API_URL}/v1/code-tasks?project_id=${encodeURIComponent(resolved)}&limit=25`,
        { cache: "no-store" },
      );
      if (rr.ok) {
        const rdata = await rr.json();
        const items = rdata.code_tasks ?? rdata.items ?? [];
        recentRuns = items
          .map((r: Record<string, unknown>) => {
            const state = (r.state ?? {}) as Record<string, unknown>;
            return {
              id: String(r.id ?? ""),
              title: String(r.title ?? ""),
              status: String(r.status ?? ""),
              profile: state.profile as string | undefined,
              risk_tier: state.risk_tier as number | undefined,
              steps_run: state.steps_run as string[] | undefined,
              cost_usd_micros: state.cost_usd_micros as number | undefined,
              stage_costs: state.stage_costs as Record<string, number> | undefined,
            };
          })
          .filter((r: PipelineRunCost) => r.profile);
      }
    } catch {
      // recent-run cost is best-effort; the profiles still render
    }

    return {
      projectId: resolved,
      apiReachable: true,
      defaultProfile: String(pdata.default ?? "balanced"),
      profiles: (pdata.items ?? []) as PipelineProfile[],
      recentRuns,
    };
  } catch {
    return emptyPipelineData(resolved);
  }
}
