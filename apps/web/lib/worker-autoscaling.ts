/**
 * Worker autoscaling control plane — queue depth → desired workers → elastic fabric.
 */

import {
  API_URL,
  authHeaders,
  PROJECT_ID,
  resolveProjectId,
} from "./client";

export type WorkerAutoscalingPolicy = {
  id: string;
  projectId: string;
  poolKey: string;
  enabled: boolean;
  minWorkers: number;
  maxWorkers: number;
  scaleUpThreshold: number;
  cooldownSeconds: number;
  maxQueueDepthPerWorker: number;
  kind: string;
  providerAccountId?: string | null;
  lastAppliedAt?: string | null;
  lastScaleDirection?: string | null;
};

export type WorkerAutoscalingPoolStatus = {
  poolKey: string;
  policyId?: string | null;
  enabled: boolean;
  kind?: string | null;
  minWorkers?: number | null;
  maxWorkers?: number | null;
  scaleUpThreshold?: number | null;
  cooldownSeconds?: number | null;
  desiredWorkers: number;
  actualWorkers: number;
  policyDesired?: number | null;
  action: string;
  holdReason?: string | null;
  lastAppliedAt?: string | null;
  lastScaleDirection?: string | null;
  workerPoolId?: string | null;
};

export type WorkerAutoscalingStatusFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  queueDepth: number;
  leasedJobs: number;
  runningJobs: number;
  pools: WorkerAutoscalingPoolStatus[];
  infraRealProvisionAllowed: boolean;
  message?: string;
};

export type WorkerAutoscalingHistoryItem = {
  id: string;
  poolKey: string;
  action: string;
  status: string;
  queueDepth: number;
  leasedJobs: number;
  desiredWorkers: number;
  actualWorkers: number;
  previousDesired: number;
  receiptId?: string | null;
  reason?: string | null;
  actor: string;
  createdAt?: string | null;
};

function apiBase(): string {
  return API_URL.replace(/\/$/, "");
}

function mapPolicy(raw: Record<string, unknown>): WorkerAutoscalingPolicy {
  return {
    id: String(raw.id ?? ""),
    projectId: String(raw.project_id ?? ""),
    poolKey: String(raw.pool_key ?? "default"),
    enabled: Boolean(raw.enabled),
    minWorkers: Number(raw.min_workers ?? 0),
    maxWorkers: Number(raw.max_workers ?? 0),
    scaleUpThreshold: Number(raw.scale_up_threshold ?? 1),
    cooldownSeconds: Number(raw.cooldown_seconds ?? 0),
    maxQueueDepthPerWorker: Number(raw.max_queue_depth_per_worker ?? 5),
    kind: String(raw.kind ?? "local"),
    providerAccountId: (raw.provider_account_id as string | null) ?? null,
    lastAppliedAt: (raw.last_applied_at as string | null) ?? null,
    lastScaleDirection: (raw.last_scale_direction as string | null) ?? null,
  };
}

function mapPool(raw: Record<string, unknown>): WorkerAutoscalingPoolStatus {
  return {
    poolKey: String(raw.pool_key ?? ""),
    policyId: (raw.policy_id as string | null) ?? null,
    enabled: Boolean(raw.enabled),
    kind: (raw.kind as string | null) ?? null,
    minWorkers: raw.min_workers == null ? null : Number(raw.min_workers),
    maxWorkers: raw.max_workers == null ? null : Number(raw.max_workers),
    scaleUpThreshold:
      raw.scale_up_threshold == null ? null : Number(raw.scale_up_threshold),
    cooldownSeconds:
      raw.cooldown_seconds == null ? null : Number(raw.cooldown_seconds),
    desiredWorkers: Number(raw.desired_workers ?? 0),
    actualWorkers: Number(raw.actual_workers ?? 0),
    policyDesired:
      raw.policy_desired == null ? null : Number(raw.policy_desired),
    action: String(raw.action ?? "unknown"),
    holdReason: (raw.hold_reason as string | null) ?? null,
    lastAppliedAt: (raw.last_applied_at as string | null) ?? null,
    lastScaleDirection: (raw.last_scale_direction as string | null) ?? null,
    workerPoolId: (raw.worker_pool_id as string | null) ?? null,
  };
}

export function emptyWorkerAutoscalingStatusFeed(
  reason?: string,
  projectId = PROJECT_ID,
): WorkerAutoscalingStatusFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    queueDepth: 0,
    leasedJobs: 0,
    runningJobs: 0,
    pools: [],
    infraRealProvisionAllowed: false,
  };
}

export async function fetchWorkerAutoscalingStatus(
  projectId = PROJECT_ID,
): Promise<WorkerAutoscalingStatusFeed> {
  const pid = resolveProjectId(projectId);
  const base = apiBase();
  if (!base) {
    return emptyWorkerAutoscalingStatusFeed(
      "BOSS API URL is not configured",
      pid,
    );
  }
  try {
    const res = await fetch(
      `${base}/v1/worker-autoscaling/status?project_id=${encodeURIComponent(pid)}`,
      { headers: authHeaders(), cache: "no-store" },
    );
    if (!res.ok) {
      return emptyWorkerAutoscalingStatusFeed(
        `API returned ${res.status}`,
        pid,
      );
    }
    const raw = (await res.json()) as Record<string, unknown>;
    const queue = (raw.queue as Record<string, unknown>) || {};
    return {
      projectId: pid,
      apiReachable: true,
      queueDepth: Number(raw.queue_depth ?? queue.queue_depth ?? 0),
      leasedJobs: Number(raw.leased_jobs ?? queue.leased_jobs ?? 0),
      runningJobs: Number(raw.running_jobs ?? queue.running_jobs ?? 0),
      pools: Array.isArray(raw.pools)
        ? (raw.pools as Record<string, unknown>[]).map(mapPool)
        : [],
      infraRealProvisionAllowed: Boolean(raw.infra_real_provision_allowed),
      message: raw.message as string | undefined,
    };
  } catch (error) {
    return emptyWorkerAutoscalingStatusFeed(
      error instanceof Error ? error.message : "Unknown API error",
      pid,
    );
  }
}

export async function fetchWorkerAutoscalingPolicies(
  projectId = PROJECT_ID,
): Promise<WorkerAutoscalingPolicy[]> {
  const pid = resolveProjectId(projectId);
  const base = apiBase();
  if (!base) return [];
  try {
    const res = await fetch(
      `${base}/v1/worker-autoscaling/policies?project_id=${encodeURIComponent(pid)}`,
      { headers: authHeaders(), cache: "no-store" },
    );
    if (!res.ok) return [];
    const raw = (await res.json()) as { items?: Record<string, unknown>[] };
    return (raw.items ?? []).map(mapPolicy);
  } catch {
    return [];
  }
}

export async function fetchWorkerAutoscalingHistory(
  projectId = PROJECT_ID,
  limit = 20,
): Promise<WorkerAutoscalingHistoryItem[]> {
  const pid = resolveProjectId(projectId);
  const base = apiBase();
  if (!base) return [];
  try {
    const res = await fetch(
      `${base}/v1/worker-autoscaling/history?project_id=${encodeURIComponent(pid)}&limit=${limit}`,
      { headers: authHeaders(), cache: "no-store" },
    );
    if (!res.ok) return [];
    const raw = (await res.json()) as { items?: Record<string, unknown>[] };
    return (raw.items ?? []).map((item) => ({
      id: String(item.id ?? ""),
      poolKey: String(item.pool_key ?? ""),
      action: String(item.action ?? ""),
      status: String(item.status ?? ""),
      queueDepth: Number(item.queue_depth ?? 0),
      leasedJobs: Number(item.leased_jobs ?? 0),
      desiredWorkers: Number(item.desired_workers ?? 0),
      actualWorkers: Number(item.actual_workers ?? 0),
      previousDesired: Number(item.previous_desired ?? 0),
      receiptId: (item.receipt_id as string | null) ?? null,
      reason: (item.reason as string | null) ?? null,
      actor: String(item.actor ?? ""),
      createdAt: (item.created_at as string | null) ?? null,
    }));
  } catch {
    return [];
  }
}
