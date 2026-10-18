/**
 * BOSS fixes itself, the console tests' shared sample: Replay A (the held port of run
 * wfr_f383…, docs/product/BOSS_FIXES_ITSELF.md §4.2) in the API's own shapes, and a fake
 * /api/self-fix answering each named view and recording each op.
 */
import type {
  AutonomyView, Card, ListView, OneView, RecommendationsView, RunAutonomyView, Send, SettingsView,
  StepSelfFix, UpstreamView,
} from "../../lib/self-fix";
import { stubFetch, type Call } from "../_helpers/multirepo-fixtures";

export const SFX = "sfx_7c2a91";
const AT = "2026-10-17T08:12:00Z";

export function card(extra: Partial<Card> = {}): Card {
  return {
    self_fix_id: SFX, title: "fix(deploy): a release that leaves the resources' project retires its app there",
    tier: 3, flags: ["host_effect"], what_broke: "Port 8000 was held by this app's own old container",
    why: "the old release ran in the resources' project; nothing retired it there",
    rejected: [{ hypothesis: "fix the single-container retire step", why: "the holder was in the shared project" }],
    confidence: { level: "high", reasons: ["BOSS's own triage places it on BOSS", "seen 2 times"] },
    change: { files: ["engine/boss_engine/services/stack_deploy.py"], added: 58, removed: 1,
              host_effect: ["removes this app's containers (docker rm -f, never volumes)"] },
    tests: { new: ["engine/tests/test_a_release_leaving_the_shared_project_retires_its_app.py"],
             red_on_base: true, green_with_fix: true, affected: 41, p0: 120, none_weakened: true },
    review: { review_code: { verdict: "pass", engine: "codex" }, security_check: { verdict: "pass", engine: "claude_code",
              findings: ["names checked", "quoting checked"] },
              regression: { verdict: "pass" }, consistency_check: { verdict: "pass" } },
    security_on_another_engine: true, review_note: "", cost_usd: 3.12, minutes: 14, engine: "codex",
    on_approve: "Builds BOSS with this fix, restarts it, checks its health, then resumes the step.",
    undo: "Automatic if health drops or the step fails the same way; or Remove, any time.",
    people: "Two people approve on a shared install; the single-operator waiver makes it one here.",
    command: `boss self-fix install ${SFX}`, ...extra,
  };
}

export function stepView(extra: Partial<StepSelfFix> = {}): StepSelfFix {
  const order = ["found", "reproduced", "fixing", "checks", "review", "you", "install", "resume"];
  const at = order.indexOf("fixing");
  return {
    id: SFX, status: "fixing", chip: "Building", label: "Fixing BOSS", stage: "fixing",
    stages: order.map((s, i) => ({ stage: s, state: i < at ? "done" : i === at ? "now" : "next" })),
    line: "Mender: the new test fails on BOSS as it is (as expected). Making it pass.",
    needs_you: false, decision_id: null,
    facts: { files: ["engine/boss_engine/services/stack_deploy.py"], tests_added: 1, cost_usd: 1.84, budget_usd: 10, minutes: 6 },
    ...extra,
  };
}

export function item(extra: Record<string, unknown> = {}) {
  return {
    id: SFX, status: "verified", chip: "Verified", stage: "resume", area: "deploy", signature: "deploy:port_held:8000",
    class_signature: "deploy:port_held", run_id: "wfr_f383649a8b024d97a80c", step_id: "deploy", confidence: "high",
    tier: 3, flags: ["host_effect"], refusal: null, refusal_detail: null,
    diff_stat: { files: [{ path: "engine/boss_engine/services/stack_deploy.py", added: 58, removed: 1 }], added: 58, removed: 1 },
    cost_micros: 3_120_000, budget_usd_micros: 10_000_000, decision_id: "dec_sfx1", upstream_state: "local_only",
    upstream_url: null, stack_state: "on_stack",
    pr: { title: "fix(deploy): retire app from the old shared project", labels: ["source:boss-agent"], branch: `boss/self-fix/${SFX}-retire` },
    created_at: AT, updated_at: AT, line: "Verified: the deploy step passed with BOSS's own fix installed.",
    ...extra,
  };
}

export function list(): ListView {
  return { count: 3, self_fixes: [
    item() as never,
    item({ id: "sfx_b2", status: "awaiting_approval", chip: "Needs you", stage: "you", area: "build",
           class_signature: "build:boss:planner", pr: { title: "fix(build): planner reads the app folder" }, stack_state: null }) as never,
    item({ id: "sfx_c3", status: "refused", chip: "Refused", stage: null, refusal: "protected_path",
           refusal_detail: "needs a change to grants", class_signature: "deploy:grant", pr: {}, stack_state: null }) as never,
  ] };
}

export const LINEAGE_KEYS = ["problem", "diagnosis", "fix", "tests", "review", "approved", "installed", "resumed", "upstream"];

export function one(): OneView {
  return {
    self_fix: item() as never,
    events: [{ event_type: "self_fix.claimed", actor: "boss_maintainer", data: { sentence: "Mender: reading BOSS's own code first." }, created_at: AT },
             { event_type: "self_fix.verified", actor: "sys_worker", data: { sentence: "Verified: the deploy step passed." }, created_at: AT }],
    tickets: [], waiters: [], line: "Verified: the deploy step passed with BOSS's own fix installed.", card: card(),
    lineage: LINEAGE_KEYS.map((k, i) => ({ key: k, label: k[0].toUpperCase() + k.slice(1),
      state: k === "upstream" ? "next" as const : "done" as const, at: k === "upstream" ? null : AT,
      detail: k === "problem" ? "deploy:port_held:8000 · run wfr_f383… step deploy" : k === "upstream" ? "not sent" : `step ${i + 1}`,
      ...(k === "problem" ? { href: "?area=delivery_workflows&wfr=wfr_f383649a8b024d97a80c" } : {}) })),
  };
}

export function settings(extra: Partial<SettingsView> = {}): SettingsView {
  return {
    switch: { on: true, stored: true, set_by: "usr_x", at: AT, why: "" },
    settings: { self_fix_min_confidence: "medium", self_fix_budget_cents: 1000, self_fix_month_cents: 5000,
                self_fix_max_minutes: 60, self_fix_attempts_per_signature: 2, self_fix_max_diff_lines: 300,
                self_fix_health_minutes: 10 },
    auto_install: "off", auto_install_why: "Off in v1: a person approves every install.",
    can_install: true, why_not: "",
    stack: [{ position: 1, self_fix_id: SFX, commit_sha: "a".repeat(40), status: "verified", title: "retire app", class_signature: "deploy:port_held" }],
    env_off: false, month_spent_usd: 3.12, installer: { last_seen_at: AT, watching: true }, ...extra,
  };
}

export function send(extra: Partial<Send> = {}): Send {
  return {
    send_id: "sfs_1", status: "previewed", channel: "github_pr", channel_why: "your fork exists",
    target: { upstream: "seanventures/boss", fork: "example-person/boss", pr_url: null },
    title: "fix(deploy): a release that leaves the resources' project retires its app there",
    labels: ["source:boss-agent", "boss:self-fix", "area:deploy", "verified:local", "engine:codex"],
    diff_stat: { files: ["engine/boss_engine/services/stack_deploy.py", "engine/tests/test_x.py"], added: 154, removed: 1 },
    diff: "diff --git a/engine/boss_engine/services/stack_deploy.py b/engine/boss_engine/services/stack_deploy.py\n+    retire()\n",
    body: "## Summary\nA release that leaves the shared project retires its app there.",
    evidence: "Bind for 0.0.0.0:8000 failed: port is already allocated (host [host])",
    trailers: { "BOSS-Self-Fix-Ref": "3fa9c0e1b2d4", "BOSS-Signature": "deploy:port_held" },
    check: { ok: true, findings: [] }, scrubbed: { body: 0, evidence: 1 },
    dedupe: { state: "none", checked_at: AT, match: null, why: "searched by deploy:port_held" },
    sha256: "f".repeat(64), checked: false, created_at: AT, result: null, ...extra,
  };
}

export function autonomy(): AutonomyView {
  const w = (week: string, problems: number, by_boss: number) =>
    ({ week, problems, by_boss, by_person: problems - by_boss, by_outside_agent: 0, open: 0, rate: problems ? by_boss / problems : null });
  return {
    window: "90d",
    totals: { problems: 21, by_boss: 9, by_person: 6, by_outside_agent: 4, open: 2, rate: 9 / 21, hands_off_rate: 0.3 },
    boss_side: { problems: 13, by_boss: 9, by_person: 0, by_outside_agent: 4, open: 0, rate: 9 / 13 },
    trend: [w("2026-09-21", 8, 0), w("2026-09-28", 5, 1), w("2026-10-05", 4, 2), w("2026-10-12", 8, 5)],
    runs: [],
  };
}

export function runAutonomy(problems = 3): RunAutonomyView {
  return { run_id: "wfr_f383649a8b024d97a80c",
           counts: { problems, by_boss: problems ? 2 : 0, by_person: problems ? 1 : 0, by_outside_agent: 0, open: 0 },
           rate: problems ? 2 / 3 : null, sentence: problems ? "3 problems · BOSS solved 2 · you solved 1" : "", problems: [] };
}

export function recommendations(): RecommendationsView {
  return { count: 2, recommendations: [
    { id: "brec_open1", signature: "build:boss:abc", status: "open", title: "the start check gets no env", symptom: "env missing",
      root_cause: "app_start drops env", proposed_change: null, files: [], occurrences: 1, self_fix_id: null,
      class_signature: "build:boss:abc", attempts: [], created_at: AT, updated_at: AT },
    { id: "brec_done", signature: "deploy:port_held:8000", status: "implemented", title: "port held", symptom: null,
      root_cause: null, proposed_change: null, files: [], occurrences: 2, self_fix_id: SFX, class_signature: "deploy:port_held",
      attempts: [], created_at: AT, updated_at: AT },
  ] };
}

export type FakeState = {
  list: ListView; one: OneView; settings: SettingsView; upstream: UpstreamView; autonomy: AutonomyView;
  run_autonomy: RunAutonomyView; recommendations: RecommendationsView;
  /** What each op answers; default: {} (200). */
  ops: Record<string, { status?: number; body: unknown }>;
};

export function newState(over: Partial<FakeState> = {}): FakeState {
  return { list: list(), one: one(), settings: settings(), upstream: { send: null, telemetry_off: null },
           autonomy: autonomy(), run_autonomy: runAutonomy(), recommendations: recommendations(),
           ops: { preview: { body: send() } }, ...over };
}

/** The fake /api/self-fix. Returns the recorded calls. */
export function fakeSelfFixApi(state: FakeState, other?: (c: Call) => { status?: number; body: unknown } | undefined): Call[] {
  return stubFetch((c) => {
    if (c.path === "/api/self-fix" && c.method === "GET") {
      const view = new URLSearchParams(c.search).get("view") ?? "";
      if (view === "diff") return { body: { diff: "diff --git a/x b/x\n+fixed\n", diff_stat: {}, tests: ["engine/tests/test_new.py"], red: {}, green: {}, gates: {}, review: {} } };
      const v = (state as unknown as Record<string, unknown>)[view];
      return v === undefined ? { status: 400, body: { error: "unknown view" } } : { body: v };
    }
    if (c.path === "/api/self-fix" && c.method === "POST") {
      const op = String((c.body as { op?: string })?.op ?? "");
      return state.ops[op] ?? { body: {} };
    }
    return other?.(c);
  });
}

export const opsOf = (calls: Call[], op: string) =>
  calls.filter((c) => c.method === "POST" && (c.body as { op?: string })?.op === op).map((c) => c.body as Record<string, unknown>);
