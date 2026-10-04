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
  /** One workflow run (`wfr_…`) — the governed run a work task was one step of. A different
   *  scope from `run_id`, and the console URL can carry both. */
  workflow_run_id?: string;
  /** One step of that workflow run: its own rows and the work task it started. */
  step_id?: string;
  /** One AI agent, by role key. */
  agent?: string;
  /** Which BOSS process said it: "api" | "worker". Rows from sources that do not record a
   *  process are excluded rather than guessed at, so this narrows to container logs. */
  service?: string;
  since?: string;
  until?: string;
  sort?: string;
  order?: string;
  limit?: number;
  offset?: number;
  /** `next_cursor` from the previous page: the rows strictly older than it. */
  cursor?: string;
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
  workflowRunId: string;
  /** The AI agent that produced it, by role key. */
  agent: string;
  /** Which BOSS process emitted it: "api" | "worker". Empty for sources that do not record a
   *  process — left empty rather than guessed, so the filter never claims a wrong origin. */
  service: string;
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
  /** Present when there is an older page; pass it back as `cursor` to load it. */
  nextCursor?: string;
};

/** How many rows the LOG holds per level and per stream (GET /v1/activity/facets) — counted
 *  against the log, not the page. */
export type ActivityFacets = {
  levels: Record<string, number>;
  sources: Record<string, number>;
  levelsExact: boolean;
  /** False when a stream count is a floor (a scoped view counts at most the scan cap per stream). */
  sourcesExact: boolean;
  note?: string;
};

export function mapActivityFacets(payload: unknown): ActivityFacets | null {
  if (!isPlainObject(payload)) return null;
  const counts = (value: unknown): Record<string, number> => {
    const out: Record<string, number> = {};
    if (isPlainObject(value)) {
      for (const [key, n] of Object.entries(value)) {
        const parsed = asFiniteNumber(n);
        if (parsed !== undefined) out[key] = parsed;
      }
    }
    return out;
  };
  return {
    levels: counts(payload.levels),
    sources: counts(payload.sources),
    levelsExact: payload.levels_exact !== false,
    sourcesExact: payload.sources_exact !== false,
    note: typeof payload.note === "string" ? payload.note : undefined,
  };
}

/** What a level or stream chip shows. A chip is read as a fact about the log, so it is "…"
 *  until the counts arrive, "?" when one could not be read — never a zero that was not counted —
 *  and carries a "+" when the count is a floor. Stream counts under a run/agent/service scope are
 *  counted from at most the facet scan cap per stream (sources_exact=false); they were shown as
 *  exact while only the level chips knew about floors (review of milestone/console-b). */
export function facetChipLabel(
  facets: ActivityFacets | null, kind: "levels" | "sources", key: string,
): string {
  if (facets === null) return "…";
  const n = facets[kind]?.[key];
  if (typeof n !== "number") return "?";
  const exact = kind === "levels" ? facets.levelsExact : facets.sourcesExact;
  return `${n}${exact ? "" : "+"}`;
}

/** The API sends a next-page cursor only for time, newest first (its order when none is sent);
 *  any other server order is one page, so its last row is not the oldest row. */
export function activityPagesByCursor(sort?: string, order?: string): boolean {
  return (sort ?? "created_at") === "created_at" && (order ?? "desc") === "desc";
}

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
  setIfPresent(params, "workflow_run_id", query.workflow_run_id);
  setIfPresent(params, "step_id", query.step_id);
  setIfPresent(params, "agent", query.agent);
  setIfPresent(params, "service", query.service);
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
  setIfPresent(params, "cursor", query.cursor);
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
    nextCursor:
      typeof payload.next_cursor === "string" && payload.next_cursor ? payload.next_cursor : undefined,
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
      workflowRunId: asText(value.workflow_run_id),
      agent: asText(value.agent),
      service: asText(value.service),
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
