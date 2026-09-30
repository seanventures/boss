import { API_URL, authHeaders } from "./client";
import { boardStatus, taskBoardColumn } from "./roster";
import type { TaskFeedItem } from "./tasks";

/**
 * Delivery metrics — real aggregates, or absent.
 *
 * The tile this replaces read "Verified today: 100", which was the console's own page size:
 * it fetched the 100 most recent receipts and counted them. Everything here is computed with
 * count()/sum() over the whole window server-side, so a number shown is the real number.
 */
export type DeliveryMetrics = {
  windowDays: number;
  prsOpened: number;
  runsCompletedWithPr: number;
  runsTotal: number;
  runsFailed: number;
  runSuccessRate: number | null;
  jobsRan: number;
  deployments: number;
  spendUsd: number;
  tokens: number;
  agentHours: number;
  runsRunning: number;
  jobsQueued: number;
  apiReachable: boolean;
};

const EMPTY: DeliveryMetrics = {
  windowDays: 7, prsOpened: 0, runsCompletedWithPr: 0, runsTotal: 0, runsFailed: 0,
  runSuccessRate: null, jobsRan: 0, deployments: 0, spendUsd: 0, tokens: 0, agentHours: 0,
  runsRunning: 0, jobsQueued: 0,
  apiReachable: false,
};

export async function fetchDeliveryMetrics(
  projectId: string,
  days = 7,
): Promise<DeliveryMetrics> {
  try {
    const r = await fetch(
      `${API_URL}/v1/metrics/delivery?project_id=${encodeURIComponent(projectId)}&days=${days}`,
      { headers: authHeaders({}), cache: "no-store" },
    );
    if (!r.ok) return EMPTY;
    const d = (await r.json()) as Record<string, number | null>;
    return {
      windowDays: Number(d.window_days ?? days),
      prsOpened: Number(d.prs_opened ?? 0),
      runsCompletedWithPr: Number(d.runs_completed_with_pr ?? 0),
      runsTotal: Number(d.runs_total ?? 0),
      runsFailed: Number(d.runs_failed ?? 0),
      runSuccessRate: d.run_success_rate === null || d.run_success_rate === undefined
        ? null : Number(d.run_success_rate),
      jobsRan: Number(d.jobs_ran ?? 0),
      deployments: Number(d.deployments ?? 0),
      spendUsd: Number(d.spend_usd ?? 0),
      tokens: Number(d.tokens ?? 0),
      agentHours: Number(d.agent_hours ?? 0),
      runsRunning: Number(d.runs_running ?? 0),
      jobsQueued: Number(d.jobs_queued ?? 0),
      apiReachable: true,
    };
  } catch {
    return EMPTY;
  }
}

/** One thing the operator started, and where it is now. */
export type InFlightItem = {
  taskId: string;
  title: string;
  status: string;
  stage: string;
  area: string;
  createdAt: string | null;
  active: boolean;
  owner?: string;
  updatedAt?: string | null;
};

/** Prefer the canonical task lifecycle when the metrics feed and task feed disagree. */
export function reconcileRecentWork(items: InFlightItem[], tasks: TaskFeedItem[]): InFlightItem[] {
  const byId = new Map(tasks.map((task) => [task.id, task]));
  return items.map((item) => {
    const task = byId.get(item.taskId);
    if (!task) return item;
    const status = boardStatus(task);
    return {
      ...item,
      status,
      active: taskBoardColumn(status) === "in_progress",
      owner: task.ownerRoleId,
      updatedAt: task.updatedAt,
    };
  });
}

export async function fetchInFlightFeed(projectId: string, limit = 6): Promise<{
  items: InFlightItem[];
  apiReachable: boolean;
}> {
  try {
    const r = await fetch(
      `${API_URL}/v1/metrics/in-flight?project_id=${encodeURIComponent(projectId)}&limit=${limit}`,
      { headers: authHeaders({}), cache: "no-store" },
    );
    if (!r.ok) return { items: [], apiReachable: false };
    const d = (await r.json()) as { items?: Array<Record<string, unknown>> };
    return { items: (d.items ?? []).map((i) => ({
      taskId: String(i.task_id ?? ""),
      title: String(i.title ?? ""),
      status: String(i.status ?? ""),
      stage: String(i.stage ?? ""),
      area: String(i.area ?? "code_tasks"),
      createdAt: (i.created_at as string) ?? null,
      active: Boolean(i.active),
    })), apiReachable: true };
  } catch {
    return { items: [], apiReachable: false };
  }
}

export async function fetchInFlight(projectId: string, limit = 6): Promise<InFlightItem[]> {
  return (await fetchInFlightFeed(projectId, limit)).items;
}
