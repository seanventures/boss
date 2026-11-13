/**
 * WORK UX, SET 2, read and changed from the console (BOSSAPP-879..884; API routers/work_ux.py).
 * Parsing is defensive and pure (tested on its own); every write goes through obsWrite →
 * apiWrite (one press, one effect).
 */
import { aaRead, type ReadResult } from "./ambient";
import { obsWrite, type WriteResult } from "../app/observability/obs-write";

const obj = (v: unknown): Record<string, unknown> =>
  (v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {});
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const str = (v: unknown) => (typeof v === "string" ? v : "");
const q = (projectId: string) => `project_id=${encodeURIComponent(projectId)}`;
const item = (id: string) => `/v1/work-items/${encodeURIComponent(id)}`;
const press = (scope: string, label: string) => ({ scope: `wux:${scope}`, label });

// ── 880: a person's watchdog on a work tree ──────────────────────────────────────────────────
export type WatchCondition = "stopped_unfinished" | "blocked" | "closed" | "stalled";
export const WATCH_WORDS: Record<WatchCondition, string> = {
  stopped_unfinished: "Stops unfinished", blocked: "Gets blocked", closed: "Is closed", stalled: "Stalls",
};
export type Watchdog = {
  id: string; watcher: string; conditions: WatchCondition[]; stall_hours: number; instructions: string;
  enabled: boolean; fired_count: number; last_fired_at: string | null;
};
export function parseWatchdogs(w: unknown): Watchdog[] {
  return arr(obj(w).watchdogs).map(obj).map((d) => ({
    id: str(d.id), watcher: str(d.watcher),
    conditions: arr(d.conditions).map(str).filter((c): c is WatchCondition => c in WATCH_WORDS),
    stall_hours: Number(d.stall_hours) || 24, instructions: str(d.instructions),
    enabled: d.enabled !== false, fired_count: Number(d.fired_count) || 0,
    last_fired_at: str(d.last_fired_at) || null,
  })).filter((d) => d.id);
}
export const readWatchdogs = (projectId: string, id: string): Promise<ReadResult<Watchdog[]>> =>
  aaRead(`${item(id)}/watchdogs?${q(projectId)}`, parseWatchdogs);
export const attachWatchdog = (projectId: string, id: string, body: { conditions: WatchCondition[]; instructions?: string; stall_hours?: number }): Promise<WriteResult> =>
  obsWrite(`${item(id)}/watchdogs`, { project_id: projectId, ...body }, press(`watch:${id}`, "Watch"));
export const detachWatchdog = (projectId: string, id: string, watchdogId: string): Promise<WriteResult> =>
  obsWrite(`${item(id)}/watchdogs/${encodeURIComponent(watchdogId)}?${q(projectId)}`, {}, press(`watch:${id}`, "Stop watching"), "DELETE");

// ── 881: one search over work ────────────────────────────────────────────────────────────────
export type SearchHit = { kind: string; id: string; title: string; snippet: string; status: string; href: string; match: "title" | "body" };
export const SEARCH_KIND_WORDS: Record<string, string> = {
  run: "Run", code_task: "Build", decision: "Decision", meeting: "PRD panel", artifact: "Artifact",
  work_item: "Work item", knowledge: "Knowledge",
};
export function parseSearch(w: unknown): { items: SearchHit[]; searched: string[] } {
  const o = obj(w);
  return {
    items: arr(o.items).map(obj).map((h) => ({
      kind: str(h.kind), id: str(h.id), title: str(h.title) || str(h.id), snippet: str(h.snippet),
      status: str(h.status), href: str(h.href), match: (h.match === "title" ? "title" : "body") as "title" | "body",
    })).filter((h) => h.id && h.href),
    searched: arr(o.searched).map(str),
  };
}
export const searchWork = (projectId: string, query: string, signal?: AbortSignal) =>
  aaRead(`/v1/search?${q(projectId)}&q=${encodeURIComponent(query)}&limit=12`, parseSearch, null, signal);

