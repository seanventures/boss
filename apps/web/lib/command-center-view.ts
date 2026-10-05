/**
 * The Command Center's one read: GET /v1/command-center/view (command-center spec §6.2).
 *
 * THE FROZEN CONTRACT (Phase 0). The engine (boss_engine.services.command_center_view), the
 * graphics (app/command-center/*.tsx, lib/command-center-graphics.ts) and the page
 * (command-center-ui.tsx) build against these shapes at once: change them with the lead, never
 * inside a stream. The wire is snake_case; `parseCommandCenterView` is the only place it is
 * mapped, and it never invents a field the server did not send (no percent, no ETA).
 *
 * COMPOSITION, NOT RESTATEMENT. `attention`, `missions`, `outcomes`, `teams` and `agents` ARE
 * Agents › Live's (agents_live_board.compose), parsed by Live's own parsers, so a card here and
 * its twin on Live are the same card. `counts.needsYou` is the one needs-you number.
 */
import {
  parseLiveBoard,
  type AttentionItem, type BoardAgent, type LiveBoard, type Mission, type Outcome, type TeamSummary,
} from "./agents-live-board";
import { usePolled, type Polled } from "./factory-floor";
import type { HostAdvice } from "../app/host-advice";
import { TZ_COOKIE, viewUrl } from "./command-center-view-server";

export { TZ_COOKIE, viewUrl };

// ── Shapes ──────────────────────────────────────────────────────────────────────────────────

/** The dot beside the state sentence (§4.5). `grey` = can't reach BOSS. */
export type StatusTone = "green" | "amber" | "red" | "accent" | "grey";

/** One line of state, chosen by the server so the pill, the favicon and this cannot disagree.
 *  Keys: unreachable | budget_stopped | host_critical | stopped | needs_you | host_slow |
 *  budget_high | running | setup | first_build | quiet. */
export type ViewStatus = { key: string; text: string; tone: StatusTone; tip: string };

export type ViewCounts = {
  agents: number; working: number; needsYou: number; stopped: number; setup: number;
  /** Live runs that moved in the last 7 days; a run with no progress for longer is `stalled`. */
  running: number; stalled: number; expiredAnswerable: number;
};

/** One hour in the viewer's zone. Buckets 0-23 are yesterday, 24-47 today. Money is the budget's
 *  own measure (MONEY_SQL over cost_receipts): a plan-covered call is $0 and its API value is
 *  `includedMicros`. */
export type HourBucket = {
  start: string; work: number; runsFinished: number; runsFailed: number;
  moneyMicros: number; includedMicros: number;
};

export type Today = {
  hours: HourBucket[]; now: string;
  runsFinished: number; runsFailed: number; moneyMicros: number; includedMicros: number;
  lastWorkAt: string | null;
  /** Any top-level product run, ever. False on a fresh install: `Ready for first build`. */
  everRan: boolean;
};

/** One day of the team's history (the pulse heatmap): finished agent steps that day. */
export type PulseDay = { date: string; n: number };

/** One of the team's latest moves, one per app or run: "Atlas planned Interview Scorecard". */
export type PulseEvent = {
  roleKey: string; verb: string; subject: string;
  verdict: "approved" | "changes_asked" | null;
  at: string; runId: string | null; kind: "build" | "run";
};

/** THE PULSE (served by the API, aggregated in SQL from the acts ledger): the team's last 14
 *  weeks by day, its busiest day, its five most active agents and its latest moves. */
export type Pulse = {
  weeks: number; start: string; days: PulseDay[]; total: number;
  busiest: PulseDay | null;
  top: { roleKey: string; n: number }[];
  latest: PulseEvent[];
  lastActAt: string | null;
};

export type WeekDay = { date: string; completed: number; failed: number };

/** `runs` means WORKFLOW runs; code tasks are `builds`. Counted over every row in the window. */
export type Week = {
  days: WeekDay[]; runs: number; passed: number; failed: number;
  builds: number; buildsPassed: number; prsOpened: number; jobs: number; deployments: number;
  moneyMicros: number; tokens: number; modelHours: number;
};

/** Old system-doctor findings, folded into one row with one safe fix (Clean up). */
export type Cleanup = { count: number; since: string; ids: string[]; title: string; detail: string };

export type BudgetState = "unknown" | "green" | "yellow" | "orange" | "red";

export type ViewBudget = {
  state: BudgetState; period: string; capUsdMicros: number; actualUsdMicros: number;
  utilizationRatio: number | null; resetsAtLabel: string; budgetTimezone: string;
  /** The day period, when the budget has one (a cap of 0 = no day cap). */
  day: { capUsdMicros: number; actualUsdMicros: number };
};

export type SystemKey = "models" | "repo" | "budget" | "host" | "queue" | "previews" | "deploys";
export type SystemTone = "green" | "amber" | "red" | "grey";

/** One Systems dot (§4.11). `word` is the label (`Models`); `state` the one word for its tone
 *  (`OK`, `Slow`, `Struggling`, `No cap`, `Not set up`, `Unknown`…); `tip` the server's reason
 *  (≤ 14 words); `href` a console query (`?area=setup&focus=models`). */
export type SystemDot = {
  key: SystemKey; tone: SystemTone; word: string; state: string; tip: string; href: string;
};

/** A finished run in the last 7 days (command_center recent[], ≤ 8). */
export type RecentRun = {
  runId: string; name: string; workflowName: string; outcome: string;
  finishedAt: string | null; error: string | null;
};

/** The budget refuses new work at the door (its own cap_refusal): drawn as the first card. */
export type BudgetRefusal = {
  period: string; capUsdMicros: number; actualUsdMicros: number; resetsAtLabel: string;
  title: string; detail: string;
};

export type CommandCenterView = {
  projectId: string; etag: string; generatedAt: string; tz: string;
  status: ViewStatus;
  counts: ViewCounts;
  attention: AttentionItem[];
  missions: Mission[];
  outcomes: Outcome[];
  recent: RecentRun[];
  teams: TeamSummary[];
  agents: BoardAgent[];
  today: Today;
  week: Week;
  pulse: Pulse | null;
  cleanup: Cleanup | null;
  budget: ViewBudget;
  host: HostAdvice;
  systems: SystemDot[];
  budgetRefusal: BudgetRefusal | null;
};

/** What the page holds: the last good view (or none) and whether the API answered last time. */
export type ViewState = {
  view: CommandCenterView | null;
  /** The reason the last read failed, while it failed. The last good view stays on screen. */
  error: string | null;
  /** When the last good view was read (ms), for `Showing what BOSS last said, at <time>`. */
  readAt: number | null;
  refresh: () => void;
};

// ── Parse ───────────────────────────────────────────────────────────────────────────────────

type W = Record<string, unknown>;
const obj = (w: unknown): W => (w && typeof w === "object" && !Array.isArray(w) ? (w as W) : {});
const arr = (w: unknown): unknown[] => (Array.isArray(w) ? w : []);
const str = (w: unknown, d = ""): string => (typeof w === "string" ? w : w === null || w === undefined ? d : String(w));
const num = (w: unknown): number => (typeof w === "number" && Number.isFinite(w) ? w : Number(w) || 0);
const optStr = (w: unknown): string | null => (typeof w === "string" && w ? w : null);

const TONES = new Set(["green", "amber", "red", "accent", "grey"]);
const SYS_TONES = new Set(["green", "amber", "red", "grey"]);
const BUDGET_STATES = new Set(["unknown", "green", "yellow", "orange", "red"]);

export function parseHour(w: unknown): HourBucket {
  const o = obj(w);
  return { start: str(o.start), work: num(o.work), runsFinished: num(o.runs_finished),
           runsFailed: num(o.runs_failed), moneyMicros: num(o.money_micros),
           includedMicros: num(o.included_micros) };
}

export function parsePulse(w: unknown): Pulse | null {
  if (!w || typeof w !== "object") return null;
  const o = obj(w);
  const day = (x: unknown): PulseDay => { const y = obj(x); return { date: str(y.date), n: num(y.n) }; };
  return {
    weeks: num(o.weeks) || 14, start: str(o.start), days: arr(o.days).map(day), total: num(o.total),
    busiest: o.busiest ? day(o.busiest) : null,
    top: arr(o.top).map((x) => { const y = obj(x); return { roleKey: str(y.role_key), n: num(y.n) }; }),
    latest: arr(o.latest).map((x) => {
      const y = obj(x);
      const v = str(y.verdict);
      return { roleKey: str(y.role_key), verb: str(y.verb), subject: str(y.subject),
               verdict: v === "approved" || v === "changes_asked" ? v : null,
               at: str(y.at), runId: optStr(y.run_id), kind: y.kind === "run" ? "run" : "build" };
    }),
    lastActAt: optStr(o.last_act_at),
  };
}

export function parseCleanup(w: unknown): Cleanup | null {
  if (!w || typeof w !== "object") return null;
  const o = obj(w);
  const count = num(o.count);
  if (!count) return null;
  return { count, since: str(o.since), ids: arr(o.ids).map((x) => str(x)), title: str(o.title),
           detail: str(o.detail) };
}

export function parseSystem(w: unknown): SystemDot {
  const o = obj(w);
  const tone = str(o.tone, "grey");
  return { key: str(o.key) as SystemKey, tone: (SYS_TONES.has(tone) ? tone : "grey") as SystemTone,
           word: str(o.word), state: str(o.state), tip: str(o.tip), href: str(o.href) };
}

export function parseCommandCenterView(w: unknown): CommandCenterView {
  const o = obj(w);
  const s = obj(o.status);
  const c = obj(o.counts);
  const t = obj(o.today);
  const b = obj(o.budget);
  const bd = obj(b.day);
  const h = obj(o.host);
  const board = parseLiveBoard(o);
  const tone = str(s.tone, "green");
  const budgetState = str(b.state, "unknown");
  return {
    projectId: str(o.project_id), etag: str(o.etag), generatedAt: str(o.generated_at), tz: str(o.tz, "UTC"),
    status: { key: str(s.key, "quiet"), text: str(s.text), tone: (TONES.has(tone) ? tone : "green") as StatusTone,
              tip: str(s.tip) },
    counts: { agents: num(c.agents), working: num(c.working), needsYou: num(c.needs_you),
              stopped: num(c.stopped), setup: num(c.setup), running: num(c.running),
              stalled: num(c.stalled), expiredAnswerable: num(c.expired_answerable) },
    // Live's own parser, over Live's own fields: the same cards, lanes, chips and faces.
    attention: board.attention, missions: board.missions, outcomes: board.outcomes,
    recent: arr(o.recent).map((x) => {
      const y = obj(x);
      return { runId: str(y.run_id), name: str(y.name) || str(y.subject) || str(y.workflow_name),
               workflowName: str(y.workflow_name), outcome: str(y.outcome),
               finishedAt: optStr(y.finished_at), error: optStr(y.error) };
    }),
    teams: board.teams, agents: board.agents,
    today: {
      hours: arr(t.hours).map(parseHour), now: str(t.now),
      runsFinished: num(t.runs_finished), runsFailed: num(t.runs_failed),
      moneyMicros: num(t.money_micros), includedMicros: num(t.included_micros),
      lastWorkAt: optStr(t.last_work_at), everRan: t.ever_ran === true,
    },
    week: (() => {
      const wk = obj(o.week);
      return {
        days: arr(wk.days).map((x) => { const y = obj(x); return { date: str(y.date), completed: num(y.completed), failed: num(y.failed) }; }),
        runs: num(wk.runs), passed: num(wk.passed), failed: num(wk.failed),
        builds: num(wk.builds), buildsPassed: num(wk.builds_passed), prsOpened: num(wk.prs_opened),
        jobs: num(wk.jobs), deployments: num(wk.deployments), moneyMicros: num(wk.money_micros),
        tokens: num(wk.tokens), modelHours: num(wk.model_hours),
      };
    })(),
    pulse: parsePulse(o.pulse),
    cleanup: parseCleanup(o.cleanup),
    budget: {
      state: (BUDGET_STATES.has(budgetState) ? budgetState : "unknown") as BudgetState,
      period: str(b.period), capUsdMicros: num(b.cap_usd_micros), actualUsdMicros: num(b.actual_usd_micros),
      utilizationRatio: typeof b.utilization_ratio === "number" ? b.utilization_ratio : null,
      resetsAtLabel: str(b.resets_at_label), budgetTimezone: str(b.budget_timezone, "UTC"),
      day: { capUsdMicros: num(bd.cap_usd_micros), actualUsdMicros: num(bd.actual_usd_micros) },
    },
    host: { show: h.show === true,
            ...(h.severity === "warning" || h.severity === "critical" ? { severity: h.severity } : {}),
            ...(typeof h.headline === "string" ? { headline: h.headline } : {}),
            ...(typeof h.detail === "string" ? { detail: h.detail } : {}),
            ...(typeof h.action === "string" ? { action: h.action } : {}),
            ...(typeof h.cause === "string" ? { cause: h.cause } : {}) },
    systems: arr(o.systems).map(parseSystem),
    budgetRefusal: (() => {
      const r = obj(o.budget_refusal);
      return o.budget_refusal ? {
        period: str(r.period), capUsdMicros: num(r.cap_usd_micros), actualUsdMicros: num(r.actual_usd_micros),
        resetsAtLabel: str(r.resets_at_label), title: str(r.title), detail: str(r.detail),
      } : null;
    })(),
  };
}

/** The view as Agents › Live's board, for the shared pieces that take one (TeamZones). */
export function boardOf(v: CommandCenterView): LiveBoard {
  return {
    projectId: v.projectId, etag: v.etag, generatedAt: v.generatedAt,
    counts: { agents: v.counts.agents, working: v.counts.working, needsYou: v.counts.needsYou,
              running: v.counts.running, stopped: v.counts.stopped, building: 0 },
    attention: v.attention, missions: v.missions, builds: [], outcomes: v.outcomes,
    teams: v.teams, agents: v.agents,
  };
}

// ── Read ────────────────────────────────────────────────────────────────────────────────────

/** The view's poll (§6.4): every 10s while the page is visible and the console is Live. */
export const VIEW_POLL_MS = 10_000;

/** The viewer's zone, from the browser; `UTC` where there is none (the server, a test). */
export function viewerZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}


/** The poll, seeded with the server's first paint so nothing flashes and the first poll is a 304. */
export function useCommandCenterView(projectId: string, tz: string, initial: CommandCenterView | null,
                                     viewer?: string | null): Polled<CommandCenterView> {
  return usePolled(projectId ? viewUrl(projectId, tz, viewer) : null, parseCommandCenterView,
                   () => VIEW_POLL_MS, { initial, etag: initial?.etag ?? null });
}

// ── Web-only state (§4.5 row 1, the Paused chip) ────────────────────────────────────────────

/** The state sentence the page shows: the server's, except when BOSS could not be reached (row
 *  1, which only the web can know). `paused` adds the `Paused` chip; it never changes the words. */
export function statusOf(view: CommandCenterView | null, reachable: boolean, readAt: number | null,
                         paused: boolean): ViewStatus & { paused: boolean } {
  if (!reachable || !view) {
    const at = readAt ? new Date(readAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : null;
    return { key: "unreachable", text: "Can't reach BOSS", tone: "grey",
             tip: at ? `Showing what BOSS last said, at ${at}` : "Showing what BOSS last said", paused };
  }
  return { ...view.status, paused };
}

/** How much the page may move (§5.2): `full`; `reduced` (the viewer asked for less motion:
 *  edges light instead of comets, rings hold still); `none` (Paused, a hidden tab, BOSS
 *  unreachable, or the host struggling: nothing moves at all). */
export type MotionMode = "full" | "reduced" | "none";

// ── Server first paint ──────────────────────────────────────────────────────────────────────

/** The server render's read (page.tsx): one call for the whole landing page. A failure is said
 *  (`error`), never drawn as a confident empty factory. */
