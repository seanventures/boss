import { BossApiError, type ArtifactSummary } from "@boss/api-client";

import { client, PROJECT_ID } from "./client";

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

export function emptyArtifactFeed(reason?: string): ArtifactFeed {
  return {
    projectId: PROJECT_ID,
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchArtifactFeed(limit = 8): Promise<ArtifactFeed> {
  try {
    const payload = await client.listArtifacts(PROJECT_ID, limit);
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapArtifact),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyArtifactFeed(`API returned ${error.status}`);
    }
    return emptyArtifactFeed(
      error instanceof Error ? error.message : "Unknown API error",
    );
  }
}
