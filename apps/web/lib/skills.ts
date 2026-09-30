import { BossApiError, type SkillSummary } from "@boss/api-client";

import { client } from "./client";

export type SkillFeedItem = {
  id: string;
  kind: string;
  riskTier: number;
  description: string;
};

export type SkillFeed = {
  apiReachable: boolean;
  blockedReason?: string;
  items: SkillFeedItem[];
};

function mapSkill(payload: SkillSummary): SkillFeedItem {
  return {
    id: payload.id,
    kind: payload.kind,
    riskTier: payload.risk_tier,
    description: payload.description,
  };
}

export function emptySkillFeed(reason?: string): SkillFeed {
  return {
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchSkillFeed(projectId?: string): Promise<SkillFeed> {
  try {
    // The project decides which skills exist for it. Without this the feed is every skill on
    // disk, and Agent Studio offers capability the project cannot be granted.
    const payload = await client.listSkills(projectId);
    return {
      apiReachable: true,
      items: payload.items.map(mapSkill),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptySkillFeed(`API returned ${error.status}`);
    }
    return emptySkillFeed(
      error instanceof Error ? error.message : "Unknown API error",
    );
  }
}
