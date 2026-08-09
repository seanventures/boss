import { BossApiError, type OrgSummary } from "@boss/api-client";

import { apiUrlProblem, client } from "./client";

export type { OrgSummary } from "@boss/api-client";

export type OrgsFeed = {
  apiReachable: boolean;
  blockedReason?: string;
  items: OrgSummary[];
};

export async function fetchOrgs(): Promise<OrgsFeed> {
  // A misconfiguration is not an outage. Without this the console reported
  // "Failed to parse URL from /v1/orgs" and retried forever, because an empty API_URL makes
  // every call a relative one — it would have retried until someone read the source.
  const misconfigured = apiUrlProblem();
  if (misconfigured) {
    return { apiReachable: false, blockedReason: misconfigured, items: [] };
  }
  try {
    const data = await client.getOrgs();
    return { apiReachable: true, items: data.items };
  } catch (error) {
    const reason =
      error instanceof BossApiError
        ? `API returned ${error.status}`
        : error instanceof Error
          ? error.message
          : "Unknown API error";
    return { apiReachable: false, blockedReason: reason, items: [] };
  }
}
