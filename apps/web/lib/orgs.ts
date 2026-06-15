import { BossApiError, type OrgSummary } from "@boss/api-client";

import { client } from "./client";

export type { OrgSummary } from "@boss/api-client";

export type OrgsFeed = {
  apiReachable: boolean;
  blockedReason?: string;
  items: OrgSummary[];
};

export async function fetchOrgs(): Promise<OrgsFeed> {
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
