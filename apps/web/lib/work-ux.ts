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
// ── 882: outputs and cross-references on a work item ─────────────────────────────────────────
export type Output = { id: string; kind: string; ref: string; title: string; status: string };
export type ItemRef = { to_kind: string; to_ref: string; source: string; title: string; human_key: string; status: string };
export type Backlink = { id: string; human_key: string; title: string; status: string };
export type Outputs = { outputs: Output[]; refs: ItemRef[]; backlinks: Backlink[] };
export const OUTPUT_WORDS: Record<string, string> = {
  pull_request: "PR", preview: "Preview", deploy: "Deploy", artifact: "Artifact", document: "Doc", other: "Link",
  work_item: "Item", decision: "Decision", code_task: "Build", workflow_run: "Run",
};
export function parseOutputs(w: unknown): Outputs {
  const o = obj(w);
  return {
    outputs: arr(o.outputs).map(obj).map((x) => ({ id: str(x.id), kind: str(x.kind), ref: str(x.ref),
      title: str(x.title) || str(x.ref), status: str(x.status) })).filter((x) => x.ref),
    refs: arr(o.refs).map(obj).map((x) => ({ to_kind: str(x.to_kind), to_ref: str(x.to_ref), source: str(x.source),
      title: str(x.title), human_key: str(x.human_key), status: str(x.status) })).filter((x) => x.to_ref),
    backlinks: arr(o.backlinks).map(obj).map((x) => ({ id: str(x.id), human_key: str(x.human_key),
      title: str(x.title), status: str(x.status) })).filter((x) => x.id),
  };
}
/** A link to open, when the ref is one (a URL); otherwise null (an id shown as text). */
export const openable = (ref: string): string | null => (/^https?:\/\//.test(ref) ? ref : null);
export const readOutputs = (projectId: string, id: string): Promise<ReadResult<Outputs>> =>
  aaRead(`${item(id)}/outputs?${q(projectId)}`, parseOutputs);
export const refreshOutputs = (projectId: string, id: string): Promise<WriteResult> =>
  obsWrite(`${item(id)}/outputs/refresh`, { project_id: projectId }, press(`out:${id}`, "Refresh"));
export const recordOutput = (projectId: string, id: string, body: { kind: string; ref: string; title?: string }): Promise<WriteResult> =>
  obsWrite(`${item(id)}/outputs`, { project_id: projectId, ...body }, press(`out:${id}`, "Add"));

// ── 883: status cards and stored summaries ───────────────────────────────────────────────────
export type StatusCard = {
  id: string; title: string; kind: "card" | "closing"; work_item_id: string | null; document: string;
  revision: number; trigger: string; refreshed_at: string | null; refresh_minutes: number; pinned: boolean;
};
export function parseCards(w: unknown): StatusCard[] {
  return arr(obj(w).cards).map(obj).map((c) => ({
    id: str(c.id), title: str(c.title), kind: (c.kind === "closing" ? "closing" : "card") as "card" | "closing",
    work_item_id: str(obj(c.scope).work_item_id) || null, document: str(c.document),
    revision: Number(c.revision) || 0, trigger: str(c.trigger), refreshed_at: str(c.refreshed_at) || null,
    refresh_minutes: Number(c.refresh_minutes) || 0, pinned: c.pinned === true,
  })).filter((c) => c.id);
}
export const readCards = (projectId: string, opts: { workItemId?: string; pinned?: boolean } = {}): Promise<ReadResult<StatusCard[]>> =>
  aaRead(`/v1/status-cards?${q(projectId)}${opts.workItemId ? `&work_item_id=${encodeURIComponent(opts.workItemId)}` : ""}${opts.pinned ? "&pinned=true" : ""}`, parseCards);
export const createCard = (projectId: string, body: { title: string; work_item_id?: string | null; refresh_minutes?: number }): Promise<WriteResult> =>
  obsWrite("/v1/status-cards", { project_id: projectId, ...body }, press("card:new", "Add card"));
export const refreshCard = (projectId: string, cardId: string): Promise<WriteResult> =>
  obsWrite(`/v1/status-cards/${encodeURIComponent(cardId)}/refresh`, { project_id: projectId }, press(`card:${cardId}`, "Refresh"));
export const deleteCard = (projectId: string, cardId: string): Promise<WriteResult> =>
  obsWrite(`/v1/status-cards/${encodeURIComponent(cardId)}?${q(projectId)}`, {}, press(`card:${cardId}`, "Remove"), "DELETE");

export const searchWork = (projectId: string, query: string, signal?: AbortSignal) =>
  aaRead(`/v1/search?${q(projectId)}&q=${encodeURIComponent(query)}&limit=12`, parseSearch, null, signal);

