import { BossApiError, type ProjectSummary } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type ProjectFeedItem = {
  id: string;
  name: string;
  slug: string;
  workspaceId: string;
  workspaceName: string;
  orgId: string;
  orgName: string;
  createdAt: string;
};

export type ProjectFeed = {
  activeProjectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: ProjectFeedItem[];
};

function mapProject(payload: ProjectSummary): ProjectFeedItem {
  return {
    id: payload.id,
    name: payload.name,
    slug: payload.slug,
    workspaceId: payload.workspace_id,
    workspaceName: payload.workspace_name,
    orgId: payload.org_id,
    orgName: payload.org_name,
    createdAt: payload.created_at,
  };
}

export function emptyProjectFeed(
  reason?: string,
  activeProjectId = PROJECT_ID,
): ProjectFeed {
  return {
    activeProjectId: resolveProjectId(activeProjectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchProjectFeed(
  activeProjectId = PROJECT_ID,
): Promise<ProjectFeed> {
  try {
    const payload = await client.listProjects();
    return {
      activeProjectId: resolveProjectId(activeProjectId),
      apiReachable: true,
      items: payload.items.map(mapProject),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyProjectFeed(`API returned ${error.status}`, activeProjectId);
    }
    return emptyProjectFeed(
      error instanceof Error ? error.message : "Unknown API error",
      activeProjectId,
    );
  }
}
