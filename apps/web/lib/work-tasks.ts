/**
 * THE WORK LIST BY TASK, SORTED BY WHO ACTS (Work panel v2, 2026-11-22).
 *
 * One row per task, never per attempt: a re-run of the same work (the engine's `rerun_of`, or
 * a workflow run started again with the same title) is an earlier attempt of ONE task, and the
 * task shows its latest. Tabs, latest first:
 *
 *   Needs you     waits on a person, and work BOSS gave up on (failed, stalled) — red lives here only
 *   BOSS working  running, including a failure BOSS is still retrying (never red)
 *   Done          finished
 *   Stopped       cancelled or replaced, and failed / unanswered work older than 7 days
 *   Planned       not started yet (Marshal's rank), shown only when there is any
 *
 * The tab counts are the lengths of these same lists: one source.
 */
import type { NeedsYou, WorkItem } from "./project-experience";
import { readyAt } from "./work-stages";

export type WorkTask = {
  /** The latest attempt's id (what the row opens). */
  id: string;
  latest: WorkItem;
  /** Earlier attempts, latest first. */
  earlier: WorkItem[];
};

export type WorkTab = "needs_you" | "working" | "done" | "stopped" | "planned";
export const WORK_TABS: WorkTab[] = ["needs_you", "working", "done", "stopped", "planned"];
export const TAB_WORDS: Record<WorkTab, string> = {
  needs_you: "Needs you", working: "BOSS working", done: "Done", stopped: "Stopped", planned: "Planned",
};
export const STOP_AFTER_MS = 7 * 86_400_000;

/** When the work last really moved: since it waits on a person, its last finished step (the
 *  engine touches a waiting run's updated_at each time it looks), else its last update. */
const at = (w: WorkItem) => Date.parse((w.state === "needs_you" ? readyAt(w) : w.state === "stalled" ? w.stalled_since || w.updated_at : w.updated_at) || w.created_at || "") || 0;
const born = (w: Pick<WorkItem, "created_at">) => Date.parse(w.created_at || "") || 0;
const titleKey = (w: WorkItem) => (w.title || w.label || "").trim().toLowerCase().replace(/\s+/g, " ");

/** Group the work into tasks: workflow runs joined by `rerun_of`, then by the same title. */
export function groupTasks(items: WorkItem[]): WorkTask[] {
  const byId = new Map(items.map((w) => [w.id, w]));
  const root = (w: WorkItem): WorkItem => {
    let cur = w; const seen = new Set<string>();
    while (cur.rerun_of && byId.has(cur.rerun_of) && !seen.has(cur.id)) { seen.add(cur.id); cur = byId.get(cur.rerun_of)!; }
    return cur;
  };
  const groups = new Map<string, WorkItem[]>();
  for (const w of items) {
    const r = root(w);
    const key = w.source === "workflow_run" && w.state !== "planned" && titleKey(r) ? `t:${titleKey(r)}` : `id:${r.id}`;
    const g = groups.get(key);
    if (g) g.push(w); else groups.set(key, [w]);
  }
  return [...groups.values()].map((g) => {
    const sorted = [...g].sort((a, b) => born(b) - born(a));
    return { id: sorted[0].id, latest: sorted[0], earlier: sorted.slice(1) };
  });
}

/** Which tab a task belongs on, by its latest attempt. */
export function tabOf(t: WorkTask, nowMs = Date.now()): WorkTab {
  const w = t.latest;
  // Unknown time is never "old": only work we know has waited 7 days moves to Stopped.
  const old = at(w) > 0 && nowMs - at(w) > STOP_AFTER_MS;
  switch (w.state) {
    case "needs_you": case "stalled": return old ? "stopped" : "needs_you";
    case "failed":
      if (w.replaced_by) return "stopped";
      // A failure BOSS is still retrying is BOSS working, never red.
      if (w.why?.who === "boss") return "working";
      return old ? "stopped" : "needs_you";
    case "running": return "working";
    case "planned": return "planned";
    case "done": return /cancel|closed|abandon|rejected|dismissed/i.test(w.status ?? "") ? "stopped" : "done";
    default: return "done";
  }
}

/** The tasks on each tab, latest first (Planned keeps the order it came in: Marshal's rank). */
export function bucketTasks(tasks: WorkTask[], nowMs = Date.now()): Record<WorkTab, WorkTask[]> {
  const out = Object.fromEntries(WORK_TABS.map((k) => [k, [] as WorkTask[]])) as Record<WorkTab, WorkTask[]>;
  for (const t of tasks) out[tabOf(t, nowMs)].push(t);
  for (const k of WORK_TABS) if (k !== "planned") out[k].sort((a, b) => at(b.latest) - at(a.latest));
  return out;
}

/** The task an id names: its latest attempt or any earlier one. */
export function taskOf(tasks: WorkTask[], id: string): WorkTask | null {
  return tasks.find((t) => t.id === id || t.earlier.some((e) => e.id === id)) ?? null;
}

/** THE NEEDS-YOU COUNT, ONE SOURCE (the project's Work-tab badge and the Needs you tab): the
 *  tasks on Needs you, plus the cards that are no piece of work's (Pick a prod host). */
export function needsYouTab(items: WorkItem[], cards: NeedsYou[], nowMs = Date.now()): { tasks: WorkTask[]; loose: NeedsYou[]; count: number } {
  const listed = items.filter((w) => w.origin !== "ticket_import");
  // A card is a row's when it names the row (its work_id, or an id that is the row's own).
  const tied = (c: NeedsYou) => c.work_id || c.id.replace(/^work:/, "");
  // A FAILED BUILD's card carries its answers (Retry, See what happened): it stands for its row.
  const failedCard = new Set(cards.filter((c) => c.kind === "failed").map(tied));
  const tasks = bucketTasks(groupTasks(listed), nowMs).needs_you
    .filter((t) => !failedCard.has(t.latest.id) && !t.latest.attempts.some((a) => failedCard.has(a.id)));
  const waiting = new Set(listed.filter((w) => w.state === "needs_you").map((w) => w.id));
  const loose = cards.filter((c) => c.kind === "failed" || !waiting.has(tied(c)));
  return { tasks, loose, count: tasks.length + loose.length };
}

/** The one Needs-you number every surface says (the Work-tab badge, the Overview, the Needs you
 *  tab): from the work list once it is read, else the cards alone. */
export function needsYouCount(cards: NeedsYou[], items: WorkItem[] | null | undefined): number {
  return items ? needsYouTab(items, cards).count : cards.length;
}
