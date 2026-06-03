import { BossApiError, type ArtifactSummary } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type ArtifactFeedItem = {
  id: string;
  taskId?: string;
  artifactType: string;
  title: string;
  createdAt: string;
};

export type ArtifactFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: ArtifactFeedItem[];
};

function mapArtifact(payload: ArtifactSummary): ArtifactFeedItem {
  return {
    id: payload.id,
    taskId: payload.task_id,
    artifactType: payload.artifact_type,
    title: payload.title,
    createdAt: payload.created_at,
  };
}

export function emptyArtifactFeed(reason?: string, projectId = PROJECT_ID): ArtifactFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchArtifactFeed(
  projectId = PROJECT_ID,
  limit = 8,
): Promise<ArtifactFeed> {
  try {
    const payload = await client.listArtifacts(resolveProjectId(projectId), limit);
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapArtifact),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyArtifactFeed(`API returned ${error.status}`, projectId);
    }
    return emptyArtifactFeed(
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}
