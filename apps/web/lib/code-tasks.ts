import { BossApiError, type CodeTaskRun } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type CodeTaskFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: CodeTaskRun[];
};

export function emptyCodeTaskFeed(reason?: string, projectId = PROJECT_ID): CodeTaskFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchCodeTasksFeed(projectId = PROJECT_ID): Promise<CodeTaskFeed> {
  const resolved = resolveProjectId(projectId);
  try {
    const res = await client.getCodeTasks(resolved);
    return { projectId: resolved, apiReachable: true, items: res.code_tasks ?? [] };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyCodeTaskFeed(error.message, resolved);
    }
    return emptyCodeTaskFeed("API unreachable", resolved);
  }
}
