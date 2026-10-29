/**
 * The Command Center's apps: GET /v1/factory/apps, the Factory's own rows (one per app), read
 * with the shared poll (usePolled: ETag, paused while hidden or Paused). The hero's numbers —
 * apps built, PRs ready to review, spent on builds — and the "Built by your team" shelf are
 * counted from these rows, so they are the Factory's numbers, never a second count.
 */
import { useState } from "react";

import { usePolled, type Polled } from "./factory-floor";

export type ShelfState = "pr" | "needs" | "failed" | "stopped" | "building" | "done";

export type CcApp = {
  key: string; app: string; runId: string; builders: string[];
  /** Every build of the app (its lineage), newest first. */
  runIds: string[];
  /** Each attempt's state, by run id. */
  attemptStates: Record<string, string>;
  /** The Factory's state key and word (`pr_open` / `PR open`, `needs_plan` / `Needs you · plan`). */
  state: string; word: string;
  prNumber: number | null; prUrl: string | null; prState: string | null;
  costUsd: number; included: boolean; builds: number; at: string | null;
  /** The latest attempt's state (what the app is doing now). */
  latestState: string;
  /** The build the card opens: the attempt that needs you, else the latest. */
  openRunId: string;
};

type W = Record<string, unknown>;
const obj = (w: unknown): W => (w && typeof w === "object" && !Array.isArray(w) ? (w as W) : {});
const arr = (w: unknown): unknown[] => (Array.isArray(w) ? w : []);
const str = (w: unknown): string => (typeof w === "string" ? w : w == null ? "" : String(w));
const num = (w: unknown): number => (typeof w === "number" && Number.isFinite(w) ? w : Number(w) || 0);

const NEEDS = new Set(["needs_plan", "needs_waiver", "needs_person", "budget_refused"]);

export function parseCcApps(w: unknown): CcApp[] {
  return arr(obj(w).apps).map((x) => {
    const o = obj(x);
    const attempts = arr(o.attempts).map(obj);
    const runId = str(o.run_id) || str(o.key);
    // THE CARD AGREES WITH NEEDS YOU (independent test): it opens the attempt that waits on
    // you, else the latest — never an older attempt's PR.
    const needing = attempts.find((a) => NEEDS.has(str(a.state)));
    const latest = attempts[0];
    return {
      key: str(o.key) || runId, app: str(o.app), runId,
      builders: arr(o.builders).map(str).filter(Boolean),
      runIds: [...new Set([...arr(o.run_ids).map(str), ...attempts.map((x) => str(x.run_id))].filter(Boolean))],
      attemptStates: Object.fromEntries(attempts.map((x) => [str(x.run_id), str(x.state)])),
      state: str(o.state), word: str(o.word),
      prNumber: o.pr_number == null ? null : num(o.pr_number) || null,
      prUrl: typeof o.pr_url === "string" ? o.pr_url : null,
      prState: typeof o.pr_state === "string" ? o.pr_state : null,
      costUsd: num(o.cost_usd), included: o.included === true,
      builds: num(o.builds) || attempts.length || 1,
      at: typeof o.at === "string" ? o.at : null,
      latestState: str(latest?.state) || str(o.state),
      openRunId: str(needing?.run_id) || str(latest?.run_id) || runId,
    };
  });
}

/** What an app waits on you for, and which build: ONE RULE for its chip and where its card
 *  goes (re-check). Read from Needs you's own items first — a plan or a merge decision about
 *  any build in the app's lineage — then from the builds' own states (a plan wait). */
export type AppNeed = { kind: "plan" | "merge" | "other"; runId: string };
type NeedItem = { kind: string; title: string; build?: { runId: string; planUrl?: string; prUrl?: string } };

export function appNeed(a: CcApp, items: NeedItem[] = []): AppNeed | null {
  const runs = new Set(a.runIds.length ? a.runIds : [a.runId]);
  for (const item of items) {
    const b = item.build;
    if (!b || !runs.has(b.runId)) continue;
    if (item.kind === "approval" && (b.planUrl || /plan/i.test(item.title))) return { kind: "plan", runId: b.runId };
    if (item.kind === "approval" && (b.prUrl || /merge/i.test(item.title))) return { kind: "merge", runId: b.runId };
    return { kind: "other", runId: b.runId };
  }
  const waiting = Object.entries(a.attemptStates).find(([, st]) => st === "needs_plan");
  if (waiting) return { kind: "plan", runId: waiting[0] };
  const other = Object.entries(a.attemptStates).find(([, st]) => NEEDS.has(st));
  return other ? { kind: "other", runId: other[0] } : null;
}

/** Which chip a card wears: what it waits on you for, else the Factory's state. */
export function shelfState(a: CcApp, need: AppNeed | null = appNeed(a)): ShelfState {
  if (need) return "needs";
  if (a.state === "pr_open" || (a.prNumber && a.prState === "open" && /^PR open/.test(a.word))) return "pr";
  if (a.state === "needs_plan" || a.state === "needs_waiver" || a.state === "needs_person" || a.state === "budget_refused" || /^Needs you/.test(a.word)) return "needs";
  if (a.state === "failed" || /^Failed/.test(a.word)) return "failed";
  if (a.state === "cancelled" || /^Stopped/.test(a.word)) return "stopped";
  if (["merged", "deployed", "done_no_pr"].includes(a.state)) return "done";
  return "building";
}

/** The chip's words: `PR #205 open`, `Plan waiting on you`, `Failed`, `Stopped`. */
export function shelfWord(a: CcApp, need: AppNeed | null = appNeed(a)): string {
  const s = shelfState(a, need);
  if (s === "pr") return a.prNumber ? `PR #${a.prNumber} open` : "PR open";
  if (s === "needs") return need?.kind === "plan" ? "Plan waiting on you" : need?.kind === "merge" ? "Merge waiting on you" : "Needs you";
  if (s === "failed") return "Failed";
  if (s === "stopped") return "Stopped";
  return a.word || "Building";
}

const ORDER: Record<ShelfState, number> = { needs: 0, pr: 1, building: 2, done: 3, failed: 4, stopped: 5 };

/** What needs you first, then open PRs (newest first), then the rest. */
export function shelfOrder(apps: CcApp[], needOf: (a: CcApp) => AppNeed | null = (a) => appNeed(a)): CcApp[] {
  return apps.map((a, i) => ({ a, i })).sort((x, y) =>
    ORDER[shelfState(x.a, needOf(x.a))] - ORDER[shelfState(y.a, needOf(y.a))]
    || (y.a.prNumber ?? 0) - (x.a.prNumber ?? 0) || x.i - y.i).map(({ a }) => a);
}

export const APPS_POLL_MS = 15_000;

/** The poll, seeded with the server's first paint (and its ETag) when there is one. */
export function useCcApps(projectId: string, initial?: { wire: unknown; etag: string | null } | null): Polled<CcApp[]> {
  const [seed] = useState(() => (initial?.wire ? { initial: parseCcApps(initial.wire), etag: initial.etag } : undefined));
  return usePolled(projectId ? `/v1/factory/apps?project_id=${encodeURIComponent(projectId)}` : null,
                   parseCcApps, () => APPS_POLL_MS, seed);
}
