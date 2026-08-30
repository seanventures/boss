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

/** Areas that actually render the code-task list. Everywhere else needs a handful for counts. */
const CODE_TASK_AREAS = new Set(["code_tasks", "delivery", "overview", undefined, ""]);

/**
 * How many runs this page needs.
 *
 * Measured on goldeneye: /v1/code-tasks?limit=200 returns **1.41 MB** while every other feed
 * on the page combined is ~130 KB — 91% of the payload for one list. The page then renders it
 * across ~30 area sections whatever area you asked for, and a single load took 111 seconds and
 * starved the API's healthcheck twice, so Traefik evicted the container and the console showed
 * "HTTP 404 / No hosts yet" about a host that was registered and verified.
 *
 * 200 stays where the list is actually shown — that limit exists because BOSS's own
 * housekeeping runs once filled the first 50 and hid every app an operator had built.
 */
export function codeTaskLimitFor(area?: string): number {
  return CODE_TASK_AREAS.has(area) ? 200 : 12;
}

export async function fetchCodeTasksFeed(
  projectId = PROJECT_ID,
  area?: string,
): Promise<CodeTaskFeed> {
  const resolved = resolveProjectId(projectId);
  try {
    // 50 was the default, and BOSS's own housekeeping runs filled it: 46 of the most recent
    // 50 were "Captain: QUALITY IMPROVE PASS", so an operator's real work — sudoku, n-queens,
    // the calculator, every app they had actually built — never reached the page at all. The
    // list looked nearly empty while 103 real runs sat behind the limit.
    // Internal runs are dropped SERVER-SIDE. Filtering them after the limit is what let 200 of
    // the 200 newest runs be "Captain: QUALITY IMPROVE PASS" and the Factory render an empty
    // grid under a heading reading "200 code tasks" -- the second time housekeeping has eaten
    // the whole window. Raising the limit again only moves it, and this response is already
    // 1.41 MB at 200.
    const res = await client.getCodeTasks(resolved, codeTaskLimitFor(area), true);
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
