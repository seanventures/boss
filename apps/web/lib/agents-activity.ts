/**
 * The client for Agents › Activity (agents-home spec §6.4): the project-wide river of agent
 * work, and the tool calls folded under one piece of it. Wire shapes are mapped only by the
 * frozen parsers in lib/agents-live-board.ts.
 */
import {
  parseActivity, parseToolCalls, type ActivityFilter, type ActivityPage, type ToolCalls,
} from "./agents-live-board";

export type ActivityQuery = {
  projectId: string;
  cursor?: string | null;
  limit?: number;
  filter?: ActivityFilter;
  role?: string | null;
  run?: string | null;
  /** Children of one item (`+n inside`), when it belongs to no run. */
  parent?: string | null;
  days?: 1 | 7;
};

export type Got<T> = { ok: true; data: T } | { ok: false; status: number; error: string };

async function get<T>(path: string, map: (w: unknown) => T): Promise<Got<T>> {
  try {
    const r = await fetch(path, { cache: "no-store" });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const detail = (body as { detail?: unknown })?.detail;
      return { ok: false, status: r.status, error: typeof detail === "string" ? detail : `HTTP ${r.status}` };
    }
    return { ok: true, data: map(body) };
  } catch (e) {
    return { ok: false, status: 0, error: String((e as Error)?.message ?? e) };
  }
}

export function activityUrl(q: ActivityQuery): string {
  const p = new URLSearchParams({ project_id: q.projectId, limit: String(q.limit ?? 50), days: String(q.days ?? 1) });
  if (q.cursor) p.set("cursor", q.cursor);
  if (q.filter && q.filter !== "all") p.set("kinds", q.filter);
  if (q.role) p.set("role", q.role);
  if (q.run) p.set("run", q.run);
  if (q.parent) p.set("parent", q.parent);
  return `/v1/agents/activity?${p}`;
}

export function listActivity(q: ActivityQuery): Promise<Got<ActivityPage>> {
  return get(activityUrl(q), parseActivity);
}

export function listToolCalls(projectId: string, actId: string, limit = 50): Promise<Got<ToolCalls>> {
  return get(`/v1/acts/${encodeURIComponent(actId)}/tools?project_id=${encodeURIComponent(projectId)}&limit=${limit}`,
             parseToolCalls);
}
