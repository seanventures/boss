/**
 * The Log's pure pieces: what each stream and level is called, how repeated lines fold, how
 * the list flattens into fixed-height display rows (so it can be windowed), and where a line
 * links to. No React here — everything is testable from fixtures.
 */
import type { ActivityLogItem } from "../../lib/activity-log";
import { dayLabel } from "../../lib/log-time";
import type { LogToken } from "../../lib/log-url";
import { nameOf } from "../agents/run-face";
import { consoleHref } from "../url-intent";

export const ROW_H = 32;
export const HEAD_H = 28;

/** The ten streams' names, as the rail and the drawer say them, with the hint as tooltip. */
export const STREAMS: { key: string; label: string; hint: string }[] = [
  { key: "event", label: "Events", hint: "The audit stream: what happened, by whom" },
  { key: "workflow_step", label: "Workflow steps", hint: "Each step attempt, with its duration and error" },
  { key: "process_step", label: "Process steps", hint: "Role handoffs inside an agent process" },
  { key: "agent_turn", label: "Agent talk", hint: "What an agent was told, said and ran" },
  { key: "llm_call", label: "Model calls", hint: "One line per model call: model, time, cost" },
  { key: "app_log", label: "App output", hint: "Output from BOSS and the apps it runs" },
  { key: "deployment", label: "Deploys", hint: "What was deployed where, and whether it came up" },
  { key: "job", label: "Jobs", hint: "BOSS's own work queue" },
  { key: "deadletter", label: "Gave up", hint: "Work BOSS stopped retrying" },
  { key: "receipt", label: "Receipts", hint: "Proof an outside effect happened once" },
];
export const STREAM_LABEL: Record<string, string> = Object.fromEntries(STREAMS.map((s) => [s.key, s.label]));

export const LEVELS: { key: string; label: string }[] = [
  { key: "error", label: "Error" }, { key: "warn", label: "Warn" },
  { key: "info", label: "Info" }, { key: "verbose", label: "Verbose" },
];
export const LEVEL_LABEL: Record<string, string> = Object.fromEntries(LEVELS.map((l) => [l.key, l.label]));

export const STATUSES: { key: string; label: string }[] = [
  { key: "failed", label: "Failed" }, { key: "waiting", label: "Waiting" }, { key: "stuck", label: "Stuck" },
  { key: "ok", label: "OK" }, { key: "info", label: "Info" },
];

export const SERVICE_LABEL: Record<string, string> = { api: "API", worker: "Worker" };

/** Kinds that are a limit stopping work: they carry the `Limit` badge and link to Govern. */
export const REFUSAL_KINDS = new Set(["budget.refused", "budget.reservation_denied",
                                      "budget.allocation_reservation_denied"]);

/** The Govern row a refusal came from. */
export function governFocusOf(item: ActivityLogItem): string | null {
  if (!REFUSAL_KINDS.has(item.kind)) return null;
  const kind = String(item.data.kind ?? "");
  const period = String(item.data.period ?? "");
  if (kind === "burst" || kind === "headroom") return "burst";
  if (period === "month") return "monthly_cap";
  return "daily_cap";
}

/** Money a line spent: only a metered model call, only when > 0 (a subscription call is not money). */
export function costOf(item: ActivityLogItem): number {
  if (item.source !== "llm_call" || item.data.included_in_subscription === true) return 0;
  const usd = Number(item.data.cost_usd ?? 0);
  return Number.isFinite(usd) && usd > 0 ? usd : 0;
}

/** How long it took, in seconds, when the line knows. */
export function durationOf(item: ActivityLogItem): number | null {
  const d = item.data.duration_seconds;
  if (typeof d === "number" && Number.isFinite(d)) return d;
  const ms = item.data.latency_ms;
  if (item.source === "llm_call" && typeof ms === "number" && ms > 0) return ms / 1000;
  return null;
}

/** The first line of a body, for the faint preview after the title. */
export function firstLine(body: string): string {
  const line = (body || "").split("\n").find((l) => l.trim()) ?? "";
  return line.length > 140 ? `${line.slice(0, 140)}…` : line;
}

/** The run a line belongs to, as the chip names it: a workflow run, else a build, else nothing. */
export function runChip(item: ActivityLogItem, labels: Record<string, string>): { id: string; label: string } | null {
  const id = item.workflowRunId || item.runId;
  if (!id) return null;
  return { id, label: labels[id] || "" };
}

/** A token as the query bar shows it: names, never raw ids, when a name is known. */
export function tokenText(t: LogToken, labels: Record<string, string>, names: { step?: string; run?: string },
                          wfr: string | null): string {
  const value = (() => {
    switch (t.key) {
      case "level": return LEVEL_LABEL[t.value] ?? t.value;
      case "src": return STREAM_LABEL[t.value] ?? t.value;
      case "svc": return SERVICE_LABEL[t.value] ?? t.value;
      case "run": case "wfr": return labels[t.value] || names.run || t.value;
      case "step": return (wfr && labels[`${wfr}/${t.value}`]) || names.step || t.value;
      case "act": return labels[t.value] || t.value;
      case "agent": return nameOf(t.value).name;
      default: return t.value;
    }
  })();
  const key = t.key === "act" ? "work" : t.key;
  return `${t.negate ? "-" : ""}${key}: ${value}`;
}

// ── Folding and flattening ──────────────────────────────────────────────────────────────────

export type Fold = { key: string; items: ActivityLogItem[] };

/** Consecutive identical lines (same stream, kind, title and agent, within 60s) fold into one. */
export function fold(items: ActivityLogItem[]): Fold[] {
  const out: Fold[] = [];
  for (const item of items) {
    const last = out[out.length - 1];
    const head = last?.items[last.items.length - 1];
    if (head && head.source === item.source && head.kind === item.kind && head.title === item.title
        && head.agent === item.agent
        && Math.abs(Date.parse(head.createdAt) - Date.parse(item.createdAt)) <= 60_000) {
      last.items.push(item);
    } else {
      out.push({ key: item.id, items: [item] });
    }
  }
  return out;
}

export type DisplayRow =
  | { kind: "day"; key: string; label: string; top: number; height: number }
  | { kind: "run"; key: string; label: string; lines: number; errors: number; top: number; height: number }
  | { kind: "line"; key: string; item: ActivityLogItem; folded: number; foldKey: string; inFold: boolean;
      top: number; height: number; index: number };

/**
 * The list as fixed-height display rows — day separators, optional run headers, lines — each
 * with its offset, so the list renders only what is on screen.
 */
export function flatten(items: ActivityLogItem[], opts: { byRun: boolean; open: ReadonlySet<string>;
                                                        labels: Record<string, string>; now?: Date }): DisplayRow[] {
  const rows: DisplayRow[] = [];
  let top = 0;
  let day = "";
  let index = 0;
  const groups = fold(items);
  const push = (row: DisplayRow) => { rows.push(row); top += row.height; };
  let runOpen = "";
  for (let g = 0; g < groups.length; g += 1) {
    const group = groups[g];
    const head = group.items[0];
    const label = dayLabel(head.createdAt, opts.now);
    if (label !== day) {
      day = label;
      runOpen = "";
      push({ kind: "day", key: `day:${label}:${head.id}`, label, top, height: HEAD_H });
    }
    if (opts.byRun) {
      const run = head.workflowRunId || head.runId;
      if (run && run !== runOpen) {
        // Counted on the lines LOADED: the header's tooltip says so.
        let lines = 0;
        let errors = 0;
        for (let k = g; k < groups.length; k += 1) {
          const it = groups[k].items[0];
          if ((it.workflowRunId || it.runId) !== run || dayLabel(it.createdAt, opts.now) !== day) break;
          lines += groups[k].items.length;
          errors += groups[k].items.filter((i) => i.level === "error").length;
        }
        push({ kind: "run", key: `run:${run}:${head.id}`, label: opts.labels[run] || run, lines, errors, top,
               height: HEAD_H });
      }
      runOpen = run || "";
    }
    const expanded = opts.open.has(group.key);
    push({ kind: "line", key: head.id, item: head, folded: group.items.length, foldKey: group.key,
           inFold: false, top, height: ROW_H, index: index++ });
    if (expanded) {
      for (const inner of group.items.slice(1)) {
        push({ kind: "line", key: inner.id, item: inner, folded: 1, foldKey: group.key, inFold: true, top,
               height: ROW_H, index: index++ });
      }
    }
  }
  return rows;
}

/** The rows on screen (plus overscan) for a scroll position. */
export function windowOf(rows: DisplayRow[], scrollTop: number, height: number, overscan = 480): DisplayRow[] {
  const from = scrollTop - overscan;
  const to = scrollTop + height + overscan;
  // Binary search for the first row ending after `from`.
  let lo = 0;
  let hi = rows.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (rows[mid].top + rows[mid].height < from) lo = mid + 1; else hi = mid;
  }
  const out: DisplayRow[] = [];
  for (let i = lo; i < rows.length && rows[i].top <= to; i += 1) out.push(rows[i]);
  return out;
}

// ── Where a line links ──────────────────────────────────────────────────────────────────────

export type LineLink = { key: string; label: string; href: string; tip: string };

/** The places a line leads, only those that exist. */
export function linksOf(item: ActivityLogItem, search: string): LineLink[] {
  const out: LineLink[] = [];
  const stepId = item.source === "workflow_step" ? item.stage : String(item.data.step_id ?? "");
  if (item.workflowRunId) {
    out.push({ key: "run", label: "Run ↗", tip: "Open the workflow run",
               href: consoleHref(search, { area: "delivery_workflows", wfr: item.workflowRunId }) });
    if (stepId) {
      out.push({ key: "step", label: "Step ↗", tip: "Open this step of the run",
                 href: consoleHref(search, { area: "delivery_workflows", wfr: item.workflowRunId, step: stepId }) });
    }
  }
  if (item.runId) {
    out.push({ key: "build", label: "Build ↗", tip: "Open the build in Factory",
               href: consoleHref(search, { area: "code_tasks", run: item.runId }) });
  }
  if (item.workflowRunId) {
    out.push({ key: "studio", label: "Agent Studio ↗", tip: "What agents did in this run",
               href: consoleHref(search, { area: "workforce", view: "activity", run: item.workflowRunId }) });
  }
  if (item.source === "deployment") {
    out.push({ key: "deploy", label: "Deploy ↗", tip: "Open the deploy",
               href: consoleHref(search, { area: "delivery", deploy: `deployment:${item.entityId}` }) });
  }
  const decision = String(item.data.decision_id ?? "") || (item.entityType === "decision" ? item.entityId : "");
  if (decision) {
    out.push({ key: "decision", label: "Decision ↗", tip: "Open the approval",
               href: consoleHref(search, { area: "decisions", decision }) });
  }
  const focus = governFocusOf(item);
  if (focus) {
    out.push({ key: "govern", label: "Open in Govern ↗", tip: "The limit that stopped it",
               href: consoleHref(search, { area: "budget", focus }) });
  }
  return out;
}
