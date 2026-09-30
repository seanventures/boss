import type {
  AssuranceOverviewResponse,
  ModelGatewayUsageResponse,
} from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type AssuranceFeed = {
  projectId: string;
  apiReachable: boolean;
  overview: AssuranceOverviewResponse;
  modelUsage: ModelGatewayUsageResponse;
};

const emptyOverview = (projectId: string): AssuranceOverviewResponse => ({
  project_id: projectId,
  metrics: [],
  cases: [],
  surfaces: [],
  verification_runs: [],
  settled_conclusions: [],
});

const emptyUsage = (projectId: string): ModelGatewayUsageResponse => ({
  project_id: projectId,
  window_days: 30,
  totals: {},
  items: [],
});

export async function fetchAssuranceFeed(
  projectId = PROJECT_ID,
): Promise<AssuranceFeed> {
  const resolved = resolveProjectId(projectId);
  const [overview, usage] = await Promise.allSettled([
    client.getAssuranceOverview(resolved),
    client.getModelGatewayUsage(resolved, 30),
  ]);
  return {
    projectId: resolved,
    apiReachable: overview.status === "fulfilled",
    overview:
      overview.status === "fulfilled" ? overview.value : emptyOverview(resolved),
    modelUsage: usage.status === "fulfilled" ? usage.value : emptyUsage(resolved),
  };
}
