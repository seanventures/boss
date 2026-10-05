/**
 * Command Center view fixtures, in the WIRE shape GET /v1/command-center/view answers
 * (command-center spec §6.2), so tests go through the same parser the page does. The live-board
 * parts ARE Agents › Live's fixture: the view composes the board, and so does this.
 *
 * Clock: "now" is 2026-10-04T15:40:00Z, the viewer is in UTC, so today's buckets are 24-47 and
 * the current hour is index 39 (15:00), 40 minutes in.
 */
import { board, failed, gate, mission } from "./live-board-fixture";

type Wire = Record<string, unknown>;

export const NOW_ISO = "2026-10-04T15:40:00Z";
export const NOW_MS = Date.parse(NOW_ISO);

/** 48 hourly buckets from yesterday 00:00 UTC; `work[i]` / `money[i]` fill hour i. */
export function hours(work: Record<number, number> = {}, money: Record<number, number> = {},
                      included: Record<number, number> = {}): Wire[] {
  const y0 = Date.parse("2026-10-03T00:00:00Z");
  return Array.from({ length: 48 }, (_, i) => ({
    start: new Date(y0 + i * 3_600_000).toISOString(),
    work: work[i] ?? 0, runs_finished: 0, runs_failed: 0,
    money_micros: money[i] ?? 0, included_micros: included[i] ?? 0,
  }));
}

/** The pulse block: 14 weeks of days from Monday 2026-06-29 to today (2026-10-04). */
export function pulse(over: { empty?: boolean } & Wire = {}): Wire {
  const { empty, ...rest } = over;
  const start = Date.parse("2026-06-29T00:00:00Z");
  const days: Wire[] = [];
  for (let t = start; t <= Date.parse("2026-10-04T00:00:00Z"); t += 86_400_000) {
    const date = new Date(t).toISOString().slice(0, 10);
    const n = empty ? 0 : date === "2026-09-03" ? 71 : date >= "2026-09-01" && date <= "2026-09-12" ? 12 : 0;
    days.push({ date, n });
  }
  const total = days.reduce((x, d) => x + Number(d.n), 0);
  return {
    weeks: 14, start: "2026-06-29", days, total,
    busiest: empty ? null : { date: "2026-09-03", n: 71 },
    top: empty ? [] : [
      { role_key: "qa", n: 81 }, { role_key: "software_engineer", n: 56 }, { role_key: "adversarial_reviewer", n: 47 },
      { role_key: "ceo_agent", n: 8 }, { role_key: "staff_engineer", n: 1 },
    ],
    latest: empty ? [] : [
      { role_key: "qa", verb: "checked", subject: "US-Equity Signal Screener", verdict: null, at: "2026-09-11T06:00:00Z", run_id: "crun_1", kind: "build" },
      { role_key: "ceo_agent", verb: "planned", subject: "Interview Scorecard", verdict: null, at: "2026-09-08T03:36:00Z", run_id: "crun_2", kind: "build" },
      { role_key: "adversarial_reviewer", verb: "reviewed", subject: "Changelog Composer", verdict: "changes_asked", at: "2026-09-07T03:36:00Z", run_id: "crun_3", kind: "build" },
      { role_key: "software_engineer", verb: "worked on", subject: "Ship it #182", verdict: null, at: "2026-09-06T03:36:00Z", run_id: "wfr_182", kind: "run" },
    ],
    last_act_at: empty ? null : "2026-10-04T12:40:00Z",
    ...rest,
  };
}

export const systems = (over: Record<string, Wire> = {}): Wire[] => [
  { key: "models", tone: "green", word: "Models", state: "OK", tip: "Claude Code working · 212 calls today", href: "?area=setup&focus=models" },
  { key: "repo", tone: "green", word: "Repo", state: "OK", tip: "acme/shop connected", href: "?area=setup&focus=repository" },
  { key: "budget", tone: "green", word: "Budget", state: "OK", tip: "$84 of $200 this month", href: "?area=budget" },
  { key: "host", tone: "green", word: "Host", state: "OK", tip: "Memory, disk and CPU are fine", href: "?area=infra" },
  { key: "queue", tone: "green", word: "Queue", state: "OK", tip: "Nothing waiting", href: "?area=monitoring" },
  { key: "previews", tone: "green", word: "Previews", state: "OK", tip: "1 preview running", href: "?area=code_tasks&view=apps" },
  { key: "deploys", tone: "grey", word: "Deploys", state: "Not set up", tip: "No deploy target yet", href: "?area=infra" },
].map((s) => ({ ...s, ...(over[s.key as string] ?? {}) }));

/** A busy afternoon: 2 running, 1 gate, 1 stopped, hand-offs in the last hour. */
export function view(over: Wire = {}): Wire {
  const b = board();
  return {
    ...b,
    etag: "\"cc-e1\"", generated_at: NOW_ISO, tz: "UTC",
    status: { key: "stopped", text: "1 stopped", tone: "red", tip: "Failed steps or builds waiting for you" },
    counts: { agents: 34, working: 2, needs_you: 1, stopped: 1, setup: 0, running: 2, stalled: 0, expired_answerable: 3 },
    attention: [failed(), gate()],
    missions: (b.missions as Wire[]),
    recent: [
      { run_id: "wfr_170", name: "Audit deps", workflow_name: "Audit", outcome: "failed",
        finished_at: "2026-10-04T12:40:00Z", error: "npm audit found 2 critical issues" },
      { run_id: "wfr_169", name: "Ship it #179", workflow_name: "Ship it", outcome: "succeeded",
        finished_at: "2026-10-04T11:10:00Z", error: null },
    ],
    teams: [
      { key: "leadership", label: "Leadership", members: ["ceo_agent", "pm"], working: 0, needs_you: 0, in_panel: 0 },
      { key: "engineering", label: "Engineering", members: ["software_engineer", "staff_engineer"], working: 1, needs_you: 0, in_panel: 0 },
      { key: "quality", label: "Quality", members: ["qa", "adversarial_reviewer"], working: 1, needs_you: 0, in_panel: 0 },
      { key: "media", label: "Media", members: ["media_publisher"], working: 0, needs_you: 1, in_panel: 0 },
    ],
    agents: [
      { role_key: "ceo_agent", name: "Chief of Staff", callsign: "Atlas", team: "leadership", state: "idle", line: "Idle", since: "2026-10-04T12:00:00Z" },
      { role_key: "pm", name: "Product Manager", callsign: "Juno", team: "leadership", state: "idle", line: "Idle", since: "2026-10-04T15:36:00Z" },
      { role_key: "software_engineer", name: "Software Engineer", callsign: "Forge", team: "engineering", state: "working", line: "Now: Coding · Build · Ship it #182" },
      { role_key: "staff_engineer", name: "Staff Engineer", callsign: "Vera", team: "engineering", state: "idle", line: "Idle", since: "2026-10-04T10:00:00Z" },
      { role_key: "qa", name: "QA", callsign: "Pilot", team: "quality", state: "working", line: "Now: Testing · Test · Ship it #181" },
      { role_key: "adversarial_reviewer", name: "Reviewer", callsign: "Sentry", team: "quality", state: "idle", line: "Idle" },
      { role_key: "media_publisher", name: "Media Publisher", callsign: "Herald", team: "media", state: "needs_you", line: "Waiting on you · Publish" },
    ],
    today: {
      hours: hours({ 9: 2, 10: 4, 14: 3, 20: 2, 33: 2, 34: 3, 36: 4, 38: 2, 39: 3 },
                   { 34: 1_200_000, 36: 2_000_000, 39: 920_000 }),
      now: NOW_ISO, runs_finished: 4, runs_failed: 1, money_micros: 4_120_000, included_micros: 3_800_000,
      last_work_at: "2026-10-04T15:36:00Z", ever_ran: true,
    },
    week: {
      days: [
        { date: "2026-09-28", completed: 6, failed: 1 }, { date: "2026-09-29", completed: 4, failed: 0 },
        { date: "2026-09-30", completed: 7, failed: 1 }, { date: "2026-10-01", completed: 5, failed: 0 },
        { date: "2026-10-02", completed: 8, failed: 1 }, { date: "2026-10-03", completed: 5, failed: 0 },
        { date: "2026-10-04", completed: 3, failed: 1 },
      ],
      runs: 42, passed: 38, failed: 4, builds: 9, builds_passed: 7, prs_opened: 6, jobs: 128,
      deployments: 3, money_micros: 12_400_000, tokens: 2_100_000, model_hours: 3.4,
    },
    pulse: pulse(),
    cleanup: null,
    budget: {
      state: "green", period: "month", cap_usd_micros: 200_000_000, actual_usd_micros: 84_000_000,
      utilization_ratio: 0.42, resets_at_label: "00:00 UTC on 1 November", budget_timezone: "UTC",
      day: { cap_usd_micros: 0, actual_usd_micros: 4_120_000 },
      subscription: true,
    },
    host: { show: false },
    systems: systems(),
    ...over,
  };
}

/** A fresh install that is set up: nothing has ever run. */
export function freshView(over: Wire = {}): Wire {
  const v = view();
  return view({
    status: { key: "first_build", text: "Ready for first build", tone: "accent", tip: "Describe something to build below" },
    counts: { agents: 7, working: 0, needs_you: 0, stopped: 0, setup: 0, running: 0, stalled: 0, expired_answerable: 0 },
    attention: [], missions: [], outcomes: [], recent: [], pulse: pulse({ empty: true }),
    agents: (v.agents as Wire[]).map((a) => ({ ...a, state: "idle", line: "Idle", since: undefined })),
    teams: (v.teams as Wire[]).map((t) => ({ ...t, working: 0, needs_you: 0 })),
    today: { hours: hours(), now: NOW_ISO, runs_finished: 0, runs_failed: 0, money_micros: 0, included_micros: 0,
             last_work_at: null, ever_ran: false },
    budget: { state: "unknown", period: "month", cap_usd_micros: 0, actual_usd_micros: 0, utilization_ratio: null,
              resets_at_label: "", budget_timezone: "UTC", day: { cap_usd_micros: 0, actual_usd_micros: 0 } },
    systems: systems({ budget: { tone: "grey", state: "No cap", tip: "No spending cap set" },
                       previews: { tone: "grey", state: "Not set up", tip: "No preview yet" } }),
    ...over,
  });
}

/** A quiet afternoon: runs earlier, none now, nothing waiting. */
export function quietView(over: Wire = {}): Wire {
  const v = view();
  return view({
    status: { key: "quiet", text: "All quiet", tone: "green", tip: "Nothing running or waiting. Last work 3h ago." },
    counts: { agents: 7, working: 0, needs_you: 0, stopped: 0, setup: 0, running: 0, stalled: 0, expired_answerable: 0 },
    attention: [], missions: [],
    agents: (v.agents as Wire[]).map((a) => ({ ...a, state: "idle", line: "Idle", since: "2026-10-04T12:40:00Z" })),
    teams: (v.teams as Wire[]).map((t) => ({ ...t, working: 0, needs_you: 0 })),
    ...over,
  });
}

export { board, failed, gate, mission };

/** /v1/factory/apps rows (the hero numbers and the shelf). */
export function apps(): Wire {
  const row = (key: string, app: string, state: string, word: string, pr: number | null, cost: number, builders: string[]) => ({
    key, app, run_id: key, builders, pr_number: pr, pr_url: pr ? `https://github.com/acme/shop/pull/${pr}` : null,
    pr_state: pr ? "open" : null, cost_usd: cost, included: false, state, word, builds: 2, run_ids: [key],
    at: "2026-09-08T03:42:13Z", attempts: [{ run_id: key, state }],
  });
  return { project_id: "p1", etag: "e", apps: [
    row("crun_a", "Interview Scorecard", "needs_plan", "Needs you · plan", 192, 3.58, ["ceo_agent", "qa"]),
    row("crun_b", "Feature-Flag Expiry Tracker", "pr_open", "PR open", 207, 3.82, ["software_engineer", "qa"]),
    row("crun_c", "Subscription-Audit Tool", "pr_open", "PR open", 205, 4.02, ["software_engineer"]),
    row("crun_d", "Slugify util", "cancelled", "Stopped", null, 0.4, ["software_engineer"]),
    row("crun_e", "Docs freshness board", "failed", "Failed · Build", null, 1.2, ["qa"]),
  ] };
}
