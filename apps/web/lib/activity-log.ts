/**
 * The Activity log's shapes and mappers — the ONE type source for the Log, the step panel and
 * the Factory's build log (the viewer used to re-declare its own copy of these).
 *
 * The mapper never invents a row or a success: a missing status reads `info`, a missing payload
 * stays empty. It KEEPS the honesty fields (`total_is_exact`, `truncated_note`, `window`) that
 * the old mapper dropped, so a count that is a floor is shown as one. Data arrives redacted.
 */

import { PROJECT_ID, resolveProjectId } from "./client";

export const ACTIVITY_LOG_STATUSES = ["ok", "failed", "waiting", "stuck", "info"] as const;
export const ACTIVITY_LOG_LEVELS = ["error", "warn", "info", "verbose"] as const;
/** The ten streams, in the rail's order. */
export const ACTIVITY_LOG_STREAMS = ["event", "workflow_step", "process_step", "agent_turn", "llm_call",
                                     "app_log", "deployment", "job", "deadletter", "receipt"] as const;

export type ActivityLogStatus = (typeof ACTIVITY_LOG_STATUSES)[number];
export type ActivityLogLevel = (typeof ACTIVITY_LOG_LEVELS)[number];

export type ActivityLogQuery = Record<string, string | number | undefined>;

export type ActivityLogItem = {
  id: string;
  source: string;
  kind: string;
  status: ActivityLogStatus;
  /** The log level. "info" for a payload from an API that predates levels, so a row never
   *  falls out of every level filter. */
  level: string;
  title: string;
  actor: string;
  entityType: string;
  entityId: string;
  stage: string;
  runId: string;
  workflowRunId: string;
  agent: string;
  service: string;
  body: string;
  createdAt: string;
  data: Record<string, unknown>;
};

export type ActivityLog = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: ActivityLogItem[];
  total?: number;
  /** False when `total` is a floor; the console then says `≥`. */
  totalIsExact?: boolean;
  truncatedNote?: string;
  limit?: number;
  offset?: number;
  nextCursor?: string;
  /** The newest row's cursor: the live tail asks for rows after it. */
  newestCursor?: string;
  /** More new rows than one page arrived since `after`: reload rather than splice. */
  gap?: boolean;
  /** Names for the ids on the page: runs, workflow runs, `wfr/step` and work items. */
  labels: Record<string, string>;
  window?: { from: string | null; to: string | null; range: string | null };
  /** Step panel (`summary=1`): how many errors and warnings, alongside the five rows. */
  counts?: { error: number; warn: number };
  /** Step panel (`summary=1`): the step's model calls — how many, their money (a subscription
   *  call is not money) and the newest model. */
  calls?: { count: number; costUsd: number; model: string };
};

export type ActivityFacets = {
  levels: Record<string, number>;
  sources: Record<string, number>;
  statuses: Record<string, number>;
  services: Record<string, number>;
  agents: { roleKey: string; n: number }[];
  agentsMore: number;
  /** The real kinds in the window (D13: `kind:` autocomplete), most common first. */
  kinds: { kind: string; n: number }[];
  runs: { id: string; label: string; n: number }[];
  total: number;
  exact: boolean;
};

export type ActivityHistogram = {
  buckets: { t: string; error: number; warn: number; info: number; verbose: number }[];
  bucketSeconds: number;
  from: string;
  to: string;
  total: number;
  totalIsExact: boolean;
};

export function emptyActivityLog(reason?: string, projectId = PROJECT_ID): ActivityLog {
  return { projectId: resolveProjectId(projectId), apiReachable: false, blockedReason: reason,
           items: [], labels: {} };
}

/** GET /v1/activity params from a plain object. Omits empties; never logs values. */
export function activityLogSearchParams(projectId: string, query: ActivityLogQuery = {}): URLSearchParams {
  const params = new URLSearchParams();
  params.set("project_id", resolveProjectId(projectId));
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null) continue;
    const text = String(value).trim();
    if (text) params.set(key, text);
  }
  return params;
}

export function mapActivityLog(payload: unknown, projectId?: string): ActivityLog {
  const fallback = resolveProjectId(projectId);
  if (!isPlainObject(payload)) return { projectId: fallback, apiReachable: true, items: [], labels: {} };
  const rawItems = Array.isArray(payload.items) ? payload.items : [];
  const window = isPlainObject(payload.window) ? payload.window : null;
  const counts = isPlainObject(payload.counts) ? payload.counts : null;
  const calls = isPlainObject(payload.calls) ? payload.calls : null;
  return {
    projectId: typeof payload.project_id === "string" && payload.project_id.trim()
      ? payload.project_id.trim() : fallback,
    apiReachable: true,
    items: rawItems.flatMap(mapActivityLogItem),
    total: asFiniteNumber(payload.total),
    totalIsExact: typeof payload.total_is_exact === "boolean" ? payload.total_is_exact : undefined,
    truncatedNote: typeof payload.truncated_note === "string" ? payload.truncated_note : undefined,
    limit: asFiniteNumber(payload.limit),
    offset: asFiniteNumber(payload.offset),
    nextCursor: asText(payload.next_cursor) || undefined,
    newestCursor: asText(payload.newest_cursor) || undefined,
    gap: payload.gap === true,
    labels: mapStrings(payload.labels),
    window: window ? { from: asText(window.from) || null, to: asText(window.to) || null,
                       range: asText(window.range) || null } : undefined,
    counts: counts ? { error: asFiniteNumber(counts.error) ?? 0, warn: asFiniteNumber(counts.warn) ?? 0 }
      : undefined,
    calls: calls ? { count: asFiniteNumber(calls.count) ?? 0, costUsd: asFiniteNumber(calls.cost_usd) ?? 0,
                     model: asText(calls.model) } : undefined,
  };
}

export function mapActivityLogItem(value: unknown): ActivityLogItem[] {
  if (!isPlainObject(value)) return [];
  const id = asText(value.id);
  if (!id) return [];
  return [{
    id,
    source: asText(value.source),
    kind: asText(value.kind),
    status: mapStatus(value.status),
    level: asText(value.level) || "info",
    title: asText(value.title),
    actor: asText(value.actor),
    entityType: asText(value.entity_type),
    entityId: asText(value.entity_id),
    stage: asText(value.stage),
    runId: asText(value.run_id),
    workflowRunId: asText(value.workflow_run_id),
    agent: asText(value.agent),
    service: asText(value.service),
    body: typeof value.body === "string" ? value.body : "",
    createdAt: asText(value.created_at),
    data: isPlainObject(value.data) ? value.data : {},
  }];
}

export function mapActivityFacets(payload: unknown): ActivityFacets | null {
  if (!isPlainObject(payload)) return null;
  const agents = Array.isArray(payload.agents) ? payload.agents : [];
  const runs = Array.isArray(payload.runs) ? payload.runs : [];
  return {
    levels: mapCounts(payload.levels),
    sources: mapCounts(payload.sources),
    statuses: mapCounts(payload.statuses),
    services: mapCounts(payload.services),
    agents: agents.filter(isPlainObject).map((a) => ({ roleKey: asText(a.role_key), n: asFiniteNumber(a.n) ?? 0 }))
      .filter((a) => a.roleKey),
    agentsMore: asFiniteNumber(payload.agents_more) ?? 0,
    kinds: (Array.isArray(payload.kinds) ? payload.kinds : []).filter(isPlainObject)
      .map((k) => ({ kind: asText(k.kind), n: asFiniteNumber(k.n) ?? 0 })).filter((k) => k.kind),
    runs: runs.filter(isPlainObject).map((r) => ({ id: asText(r.id), label: asText(r.label), n: asFiniteNumber(r.n) ?? 0 }))
      .filter((r) => r.id),
    total: asFiniteNumber(payload.total) ?? 0,
    exact: payload.exact !== false && payload.levels_exact !== false,
  };
}

export function mapActivityHistogram(payload: unknown): ActivityHistogram | null {
  if (!isPlainObject(payload) || !Array.isArray(payload.buckets)) return null;
  return {
    buckets: payload.buckets.filter(isPlainObject).map((b) => ({
      t: asText(b.t), error: asFiniteNumber(b.error) ?? 0, warn: asFiniteNumber(b.warn) ?? 0,
      info: asFiniteNumber(b.info) ?? 0, verbose: asFiniteNumber(b.verbose) ?? 0,
    })),
    bucketSeconds: asFiniteNumber(payload.bucket_seconds) ?? 900,
    from: asText(payload.from),
    to: asText(payload.to),
    total: asFiniteNumber(payload.total) ?? 0,
    totalIsExact: payload.total_is_exact !== false,
  };
}

const STATUS_SET = new Set<string>(ACTIVITY_LOG_STATUSES);

function mapStatus(value: unknown): ActivityLogStatus {
  // Neutral only — never invent a success.
  return typeof value === "string" && STATUS_SET.has(value) ? (value as ActivityLogStatus) : "info";
}

function mapCounts(value: unknown): Record<string, number> {
  const out: Record<string, number> = {};
  if (isPlainObject(value)) {
    for (const [key, n] of Object.entries(value)) {
      const parsed = asFiniteNumber(n);
      if (parsed !== undefined) out[key] = parsed;
    }
  }
  return out;
}

function mapStrings(value: unknown): Record<string, string> {
  const out: Record<string, string> = {};
  if (isPlainObject(value)) for (const [k, v] of Object.entries(value)) if (typeof v === "string" && v) out[k] = v;
  return out;
}

function asText(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function asFiniteNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
