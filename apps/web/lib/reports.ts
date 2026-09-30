import { BossApiError, type ReportSummaryResponse } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type ReportFeed = {
  apiReachable: boolean;
  blockedReason?: string;
  data?: ReportSummaryResponse;
};

export async function fetchReportSummary(
  projectId = PROJECT_ID,
  active = true,
): Promise<ReportFeed> {
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return { apiReachable: false, blockedReason: "not requested" };
  try {
    const data = await client.getReportSummary(resolveProjectId(projectId));
    return { apiReachable: true, data };
  } catch (error) {
    if (error instanceof BossApiError) {
      return { apiReachable: false, blockedReason: `API returned ${error.status}` };
    }
    return {
      apiReachable: false,
      blockedReason: error instanceof Error ? error.message : "Unknown API error",
    };
  }
}
