import {
  BossApiError,
  type CodeTaskRun,
  type CodeTaskTestResult,
  type PreviewRec,
  type QaBenchmarkApp,
} from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type PreviewFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: PreviewRec[];
};

export function emptyPreviewFeed(reason?: string, projectId = PROJECT_ID): PreviewFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export function isLoopbackPreviewUrl(url: string | null | undefined): boolean {
  if (!url) return false;
  const lowered = url.trim().toLowerCase();
  return (
    lowered.includes("://127.") ||
    lowered.includes("://localhost") ||
    lowered.includes("://[::1]") ||
    lowered.includes("://0.0.0.0") ||
    lowered.startsWith("127.") ||
    lowered.startsWith("localhost") ||
    lowered.startsWith("[::1]") ||
    lowered.startsWith("0.0.0.0")
  );
}

/** Operator View href. Never invent a gateway path for verified-test without a URL
 * (that 409s). Running previews may use the gateway path when open_url is absent. */
export function operatorPreviewOpenHref(
  preview: Pick<PreviewRec, "id" | "status" | "url" | "open_url">,
): string | null {
  const status = (preview.status || "").toLowerCase();
  if (status === "expired" || status === "stopped" || status === "failed") {
    return null;
  }
  const candidate = (preview.open_url || preview.url || "").trim();
  if (candidate && !isLoopbackPreviewUrl(candidate)) return candidate;
  if (status === "running" && preview.id) {
    return `/v1/previews/${encodeURIComponent(preview.id)}/web/`;
  }
  return null;
}

export async function fetchPreviews(projectId = PROJECT_ID): Promise<PreviewFeed> {
  const resolved = resolveProjectId(projectId);
  try {
    const res = await client.getPreviews(resolved);
    return {
      projectId: resolved,
      apiReachable: true,
      items: res.previews ?? [],
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyPreviewFeed(`API returned ${error.status}`, resolved);
    }
    return emptyPreviewFeed(
      error instanceof Error ? error.message : "Preview list failed",
      resolved,
    );
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

export async function fetchQaBenchmarkApps(projectId?: string): Promise<QaBenchmarkApp[]> {
  try {
    // Passing the project is what lets each card know whether it has already been built here.
    return (await client.getQaBenchmarkApps(projectId)).benchmark_apps ?? [];
  } catch {
    return [];
  }
}
