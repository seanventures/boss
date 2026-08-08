import {
  BossApiError,
  type CodeTaskRun,
  type CodeTaskTestResult,
  type PreviewRec,
  type QaBenchmarkApp,
} from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export async function fetchPreviews(projectId = PROJECT_ID): Promise<PreviewRec[]> {
  try {
    return (await client.getPreviews(resolveProjectId(projectId))).previews ?? [];
  } catch {
    return [];
  }
}

export type CodeTaskFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: CodeTaskRun[];
};

/**
 * Code-task state is persisted JSON and may include legacy/demo payloads. Treat a
 * non-array `tests` value as absent structured evidence instead of trusting it as
 * a test-result list (or crashing the operator console).
 */
export function structuredCodeTaskTests(state?: CodeTaskRun["state"]): CodeTaskTestResult[] {
  return Array.isArray(state?.tests) ? state.tests : [];
}

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

export async function fetchQaBenchmarkApps(): Promise<QaBenchmarkApp[]> {
  try {
    return (await client.getQaBenchmarkApps()).benchmark_apps ?? [];
  } catch {
    return [];
  }
}
