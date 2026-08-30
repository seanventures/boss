/**
 * Global activity log (BOSS-379). Projection over events + read-only
 * process/job/deadletter/receipt rows — not a second event store.
 *
 * Mapper never invents rows or a success status. Missing payload stays
 * empty. Data is treated as already redacted; this module never logs it.
 */

import { PROJECT_ID, resolveProjectId } from "./client";

export const ACTIVITY_LOG_SOURCES = [
  "event",
  "process_step",
  "job",
  "deadletter",
  "receipt",
] as const;

export const ACTIVITY_LOG_STATUSES = [
  "ok",
  "failed",
  "waiting",
  "stuck",
  "info",
] as const;

export const ACTIVITY_LOG_SORTS = ["created_at", "status", "kind"] as const;

export const ACTIVITY_LOG_ORDERS = ["desc", "asc"] as const;

export type ActivityLogSource = (typeof ACTIVITY_LOG_SOURCES)[number];
export type ActivityLogStatus = (typeof ACTIVITY_LOG_STATUSES)[number];
export type ActivityLogSort = (typeof ACTIVITY_LOG_SORTS)[number];
export type ActivityLogOrder = (typeof ACTIVITY_LOG_ORDERS)[number];

export type ActivityLogQuery = {
  q?: string;
  kind?: string;
  status?: string;
  actor?: string;
  entity_type?: string;
  /** verbose | info | warn | error — how loudly a row speaks, which is a different question
   *  from `status` (what happened to the work). */
  level?: string;
  /** One stream, or "infra" for the three that describe BOSS operating itself. */
  source?: string;
  /** One work task. */
  run_id?: string;
  /** One AI agent, by role key. */
  agent?: string;
  since?: string;
  until?: string;
  sort?: string;
  order?: string;
  limit?: number;
  offset?: number;
};

export type ActivityLogItem = {
  id: string;
  source: string;
  kind: string;
  status: ActivityLogStatus;
  /** The log level. Defaults to "info" for a payload from an older API, so a mixed-version
   *  deployment shows rows at a sane level rather than dropping them out of every filter. */
  level: string;
  title: string;
  actor: string;
  entityType: string;
  entityId: string;
  stage: string;
  /** The work task this row belongs to, when it belongs to one. */
  runId: string;
  /** The AI agent that produced it, by role key. */
  agent: string;
  /** The readable body of a transcript turn — what was said, or what a tool returned. Empty
   *  for every other source, which carry structured `data` instead. */
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
  limit?: number;
  offset?: number;
};

const STATUS_SET = new Set<string>(ACTIVITY_LOG_STATUSES);
const SORT_SET = new Set<string>(ACTIVITY_LOG_SORTS);
const ORDER_SET = new Set<string>(ACTIVITY_LOG_ORDERS);

export function emptyActivityLog(
  reason?: string,
  projectId = PROJECT_ID,
): ActivityLog {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

/** Build GET /v1/activity query params. Omits empty filters; never logs values. */
export function activityLogSearchParams(
  projectId: string,
  query: ActivityLogQuery = {},
): URLSearchParams {
  const params = new URLSearchParams();
  params.set("project_id", resolveProjectId(projectId));
  setIfPresent(params, "q", query.q);
  setIfPresent(params, "kind", query.kind);
  setIfPresent(params, "status", query.status);
  setIfPresent(params, "actor", query.actor);
  setIfPresent(params, "entity_type", query.entity_type);
  setIfPresent(params, "level", query.level);
  setIfPresent(params, "source", query.source);
  setIfPresent(params, "run_id", query.run_id);
  setIfPresent(params, "agent", query.agent);
  setIfPresent(params, "since", query.since);
  setIfPresent(params, "until", query.until);
  if (query.sort && SORT_SET.has(query.sort)) {
    params.set("sort", query.sort);
  }
  if (query.order && ORDER_SET.has(query.order)) {
    params.set("order", query.order);
  }
  if (typeof query.limit === "number" && Number.isFinite(query.limit)) {
    params.set("limit", String(Math.min(200, Math.max(1, Math.trunc(query.limit)))));
  }
  if (typeof query.offset === "number" && Number.isFinite(query.offset)) {
    params.set("offset", String(Math.max(0, Math.trunc(query.offset))));
  }
  return params;
}

export function mapActivityLog(
  payload: unknown,
  projectId?: string,
): ActivityLog {
  const fallback = resolveProjectId(projectId);
  if (!isPlainObject(payload)) {
    return { projectId: fallback, apiReachable: true, items: [] };
  }
  const rawItems = Array.isArray(payload.items) ? payload.items : [];
  return {
    projectId:
      typeof payload.project_id === "string" && payload.project_id.trim()
        ? payload.project_id.trim()
        : fallback,
    apiReachable: true,
    items: rawItems.flatMap(mapActivityLogItem),
    total: asFiniteNumber(payload.total),
    limit: asFiniteNumber(payload.limit),
    offset: asFiniteNumber(payload.offset),
  };
}

function mapActivityLogItem(value: unknown): ActivityLogItem[] {
  if (!isPlainObject(value)) return [];
  const id = asText(value.id);
  if (!id) return [];
  return [
    {
      id,
      source: asText(value.source),
      kind: asText(value.kind),
      status: mapStatus(value.status),
      // "info" rather than "" when absent: an API that predates levels should render its rows
      // at a sane level, not have them vanish from every level filter.
      level: asText(value.level) || "info",
      title: asText(value.title),
      actor: asText(value.actor),
      entityType: asText(value.entity_type),
      entityId: asText(value.entity_id),
      stage: asText(value.stage),
      runId: asText(value.run_id),
      agent: asText(value.agent),
      body: asText(value.body),
      createdAt: asText(value.created_at),
      data: isPlainObject(value.data) ? value.data : {},
    },
  ];
}

function mapStatus(value: unknown): ActivityLogStatus {
  if (typeof value === "string" && STATUS_SET.has(value)) {
    return value as ActivityLogStatus;
  }
  // Neutral only — never invent a success.
  return "info";
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

function setIfPresent(
  params: URLSearchParams,
  key: string,
  value: string | undefined,
): void {
  const trimmed = value?.trim();
  if (trimmed) params.set(key, trimmed);
}
