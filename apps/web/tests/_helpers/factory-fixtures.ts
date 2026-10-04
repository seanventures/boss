/**
 * Wire fixtures (snake_case, as the API sends them) for every Factory state (spec §4.7).
 * Phase 0: the Floor (S2) and the cockpit (S3) render these until the engine (S1) lands, and
 * their tests keep rendering them after. A shape here is the contract in lib/factory-floor.ts
 * and lib/cockpit.ts — change all three together.
 */

type W = Record<string, unknown>;

export const T0 = "2026-10-04T09:12:00.000Z";
export const at = (min: number): string => new Date(Date.parse(T0) + min * 60_000).toISOString();

const ORDER = ["plan", "tests", "build", "check", "review", "pr", "preview", "ship"] as const;
const LABEL: Record<string, string> = {
  plan: "Plan", you_plan: "You", tests: "Tests", build: "Build", check: "Check", review: "Review",
  pr: "PR", preview: "Preview", ship: "Ship",
};
/** Who stands at each station once it has an act (the role the engine would sign). */
const ROLE: Record<string, string | null> = {
  plan: "chief_architect", tests: "qa", build: "software_engineer", check: "qa", review: "adversarial_reviewer",
  pr: null, preview: null, ship: null,
};
const HOLLOW: Record<string, string | null> = {
  plan: "chief_architect", tests: "qa", build: null, check: null, review: "adversarial_reviewer",
  pr: null, preview: null, ship: null,
};
const WORKING_WORD: Record<string, string> = {
  plan: "Planning", tests: "Testing", build: "Coding", check: "Testing", review: "Reviewing",
  pr: "Working", preview: "Deploying", ship: "Deploying",
};

export type StationOverride = Partial<W> & { key: string };

/** Stations for a build at `here` with `status` there; before it done, after it pending. */
export function stations(here: string | null, status: string = "working", over: StationOverride[] = [],
                         extra: { youPlan?: string } = {}): W[] {
  const idx = here ? ORDER.indexOf(here as typeof ORDER[number]) : -1;
  const out: W[] = [];
  ORDER.forEach((key, i) => {
    let s: string = i < idx ? "done" : i === idx ? status : "pending";
    if (here === null) s = "pending";
    const machine = ROLE[key] === null || (key === "check" && s === "pending");
    let face: string;
    let role_key: string | null = null;
    let hollow_role: string | null = null;
    if (key === "ship" && s !== "pending") face = "you";
    else if (s === "pending") {
      hollow_role = HOLLOW[key];
      face = hollow_role ? "hollow" : key === "pr" || key === "preview" || key === "check" ? "boss" : "none";
    } else if (machine) face = "boss";
    else { face = "agent"; role_key = ROLE[key]; }
    const word = s === "done" ? "Done" : s === "pending" ? "Next" : s === "stuck" ? "Stuck"
      : s === "waiting" ? "Waiting" : s === "needs_you" ? "Needs you" : s === "skipped" ? "Skipped" : WORKING_WORD[key];
    out.push({
      key, label: LABEL[key], status: s, face, role_key, hollow_role,
      act_id: role_key ? `act_${key.toUpperCase()}0000000001` : null,
      act_ids: role_key ? [`act_${key.toUpperCase()}0000000001`] : [],
      seats: [], word,
      tip: face === "boss" ? "BOSS machinery. No agent at this station."
        : face === "hollow" ? `Next: ${key} · ${LABEL[key]}` : null,
      started_at: s === "pending" ? null : at(i * 2), ended_at: s === "done" ? at(i * 2 + 2) : null,
      edge_label: i === 0 ? null : "Next stage", rounds: 0, loop_with: null, links: [],
    });
    if (key === "plan" && extra.youPlan) {
      out.push({ key: "you_plan", label: "You", status: extra.youPlan, face: "you", role_key: null, hollow_role: null,
                 act_id: null, act_ids: [], seats: [], word: extra.youPlan === "done" ? "Done" : "Needs you",
                 tip: "Approve the plan to start building", rounds: 0, loop_with: null, links: [] });
    }
  });
  for (const o of over) {
    const s = out.find((x) => x.key === o.key);
    if (s) Object.assign(s, o);
  }
  return out;
}

export function buildRow(over: W = {}): W {
  return {
    run_id: "crun_inv2", app: "Invoice export", title: "Add CSV export to invoices", attempt: 2,
    root_run_id: "crun_inv1", actor: "ceo",
    state: "building", word: "Coding", tip: "Build · Implement", verb: "Coding", station: "build",
    stations: stations("build"), k: 2, n: 8, rounds: 0,
    started_at: at(0), station_since: at(6), updated_at: at(9),
    cost_usd: 0.31, metered: true, attention_id: null, preview: null,
    origin: { kind: "workflow_run", id: "wfr_182", label: "Ship it #182" },
    ...over,
  };
}

const seats2 = (status: string): W[] => [
  { act_id: "act_SEAT10000000001", role_key: "adversarial_reviewer", seat: 1, status, verdict: null },
  { act_id: "act_SEAT20000000001", role_key: "security_reviewer", seat: 2, status, verdict: null },
];

/** One BuildRow per §4.7 state, keyed by the state's name in the spec's table. */
export const ROWS: Record<string, W> = {
  queued: buildRow({ run_id: "crun_q", app: "Billing fix", state: "queued", word: "Queued", tip: "Waiting for a worker",
                     verb: "Waiting", station: null, stations: stations(null), k: 0, cost_usd: 0, metered: false }),
  starting: buildRow({ run_id: "crun_s", state: "queued", word: "Starting", tip: "Cloning the repo", verb: "Working",
                       station: "plan", stations: stations("plan", "waiting", [{ key: "plan", face: "boss", role_key: null }]), k: 0 }),
  planning: buildRow({ run_id: "crun_p", state: "building", word: "Planning", verb: "Planning", station: "plan",
                       stations: stations("plan"), k: 0 }),
  needs_plan: buildRow({ run_id: "crun_np", state: "needs_plan", word: "Needs you · plan", tip: "Approve the plan to start building",
                         verb: "Needs you", station: "you_plan", stations: stations("tests", "pending", [], { youPlan: "needs_you" }),
                         k: 1, attention_id: "dec_plan1" }),
  writing_tests: buildRow({ run_id: "crun_wt", word: "Testing", verb: "Testing", station: "tests", stations: stations("tests"), k: 1 }),
  implementing: buildRow({}),
  checking: buildRow({ run_id: "crun_ck", word: "Testing", verb: "Testing", station: "check", stations: stations("check"), k: 3 }),
  reviewing: buildRow({ run_id: "crun_rv", word: "Reviewing", verb: "Reviewing", station: "review", k: 4,
                        stations: stations("review", "working", [{ key: "review", seats: seats2("working") }]) }),
  repairing: buildRow({ run_id: "crun_rp", app: "Team inbox", word: "Coding · round 2", verb: "Coding", station: "build", k: 2, rounds: 2,
                        stations: stations("build", "working", [{ key: "build", rounds: 2, loop_with: "adversarial_reviewer" }]) }),
  checks_failed: buildRow({ run_id: "crun_cf", app: "Team inbox", state: "needs_waiver", word: "Checks failed",
                            tip: "Review passed · Security clean", verb: "Stuck", station: "review", k: 4,
                            stations: stations("review", "stuck", [{ key: "review", seats: seats2("stuck") }]),
                            attention_id: "build:crun_cf:gates" }),
  budget_wait: buildRow({ run_id: "crun_bw", app: "Billing fix", state: "budget_wait", word: "Waiting · budget",
                          tip: "Waiting for budget to free up. Costs nothing meanwhile.", verb: "Waiting", station: "build",
                          stations: stations("build", "waiting"), k: 2 }),
  budget_refused: buildRow({ run_id: "crun_br", app: "Billing fix", state: "budget_refused", word: "Budget refused",
                             tip: "Over the build budget. Raise it or retry.", verb: "Blocked", station: "build",
                             stations: stations("build", "stuck", [{ key: "build", face: "boss", role_key: null }]),
                             k: 2, attention_id: "build:crun_br:budget" }),
  quiet: buildRow({ run_id: "crun_qt", state: "quiet", word: "Quiet · 24m", tip: "No progress for 24m. It may be stuck.",
                    verb: "Coding", station: "build", stations: stations("build", "working"), k: 2, updated_at: at(-24) }),
  blocked: buildRow({ run_id: "crun_bl", state: "blocked", word: "Blocked", tip: "The repo could not be cloned",
                      verb: "Blocked", station: "build", stations: stations("build", "stuck"), k: 2 }),
};

export function attentionPlan(): W {
  return {
    id: "dec_plan1", kind: "approval", decision_kind: "general", decision_id: "dec_plan1",
    title: "Approve the plan?", detail: "Atlas wrote the plan.", role_key: "chief_architect",
    run: null, actions: [
      { action: "approve", label: "Approve", tone: "approve", effect: "The build starts.", needs: [] },
      { action: "reject", label: "Reject", tone: "reject", effect: "The build stops.", needs: [] },
    ], answers: [], decide_inline: true, content_hash: "h1", at: at(-4),
    build: { run_id: "crun_np", app: "Invoice export", where: "Plan", failing: [], retryable: false, plan_url: "/v1/code-tasks/crun_np" },
  };
}

export function attentionGates(): W {
  return {
    id: "build:crun_cf:gates", kind: "gate_override", title: "2 checks failed", detail: "Review passed · Security clean",
    role_key: "adversarial_reviewer", run: null, actions: [], answers: [], decide_inline: false, at: at(-12),
    build: {
      run_id: "crun_cf", app: "Team inbox", where: "Review · round 2", retryable: true,
      failing: [
        { name: "code_review_passed", label: "Review passed", waivable: true },
        { name: "security_clean", label: "Security clean", waivable: true },
        { name: "app_serves", label: "App starts", waivable: false, reason: "Can't be waived: the app must start" },
      ],
    },
  };
}

export function attentionBudget(): W {
  return {
    id: "build:crun_br:budget", kind: "budget_refused", title: "Budget refused", detail: "Over the build budget.",
    role_key: null, run: null, actions: [], answers: [], decide_inline: false, at: at(-6),
    build: { run_id: "crun_br", app: "Billing fix", where: "before Build", failing: [], retryable: true },
  };
}

export function attentionPr(): W {
  return {
    id: "dec_pr842", kind: "approval", decision_kind: "pull_request", decision_id: "dec_pr842",
    title: "Merge PR #842?", detail: "Forge opened PR #842.", role_key: "software_engineer", run: null,
    actions: [{ action: "approve", label: "Merge", tone: "approve", effect: "Merges the PR.", needs: [] }],
    answers: [], decide_inline: false, at: at(-30),
    build: { run_id: "crun_pr", app: "Invoice export", where: "PR open", failing: [], retryable: false,
             pr_url: "https://github.com/acme/app/pull/842", preview_url: "http://127.0.0.1:4100/" },
  };
}

export function floor(over: W = {}): W {
  const builds = [ROWS.needs_plan, ROWS.implementing, ROWS.repairing, ROWS.checks_failed, ROWS.budget_wait, ROWS.queued];
  return {
    project_id: "boss-dogfood", etag: "W/\"f1\"", generated_at: at(10),
    counts: { building: builds.length, needs_you: 3, shipped_7d: 3, stopped: 1, apps: 4 },
    attention: [attentionPlan(), attentionGates(), attentionPr()],
    builds,
    outcomes: [
      { kind: "pr_merged", run_id: "crun_m", app: "Invoice export", role_key: "software_engineer", text: "PR #842 merged", url: "https://github.com/acme/app/pull/842", at: at(-60) },
      { kind: "deployed", run_id: "crun_d", app: "Team inbox", role_key: null, text: "Deployed · dev", at: at(-120) },
      { kind: "pr_opened", run_id: "crun_o", app: "Search", role_key: "software_engineer", text: "PR #839 opened", url: "https://github.com/acme/app/pull/839", at: at(-180) },
    ],
    stopped: [
      { run_id: "crun_f", app: "Search revamp", title: "Faster search", state: "failed", word: "Failed · Build",
        tip: "Failed · retry may fix", role_key: "software_engineer", station: "build", retryable: true, at: at(-200) },
    ],
    pre_build: [],
    ...over,
  };
}

export function emptyFloor(): W {
  return floor({ counts: { building: 0, needs_you: 0, shipped_7d: 0, stopped: 0, apps: 0 },
                 attention: [], builds: [], outcomes: [], stopped: [], pre_build: [] });
}

// ── Cockpit ─────────────────────────────────────────────────────────────────────────────────

const act = (id: string, role: string | null, status: string, verb: string, label: string, start: number, end: number | null): W => ({
  act_id: id, role_key: role, kind: "stage", status, verb, label, started_at: at(start),
  ended_at: end === null ? null : at(end), cost_usd: 0.05,
});

export function cockpit(over: W = {}): W {
  return {
    etag: "W/\"c1\"",
    run: { id: "crun_inv2", title: "Add CSV export to invoices", status: "running", phase: "implement",
           created_at: at(0), updated_at: at(9), attempt: 2, root_run_id: "crun_inv1" },
    app: "Invoice export",
    state: { state: "building", word: "Coding", tip: "Build · Implement", verb: "Coding", station: "build", k: 2, n: 8, rounds: 0, preview: null },
    started_at: at(0), station_since: at(6), cost_usd: 0.31, metered: true, live: true,
    stoppable: true, retryable: false, retry_reason: "Still running", failure: null,
    lineage: [
      { run_id: "crun_inv2", attempt: 2, state: "building", word: "Coding", at: at(0) },
      { run_id: "crun_inv1", attempt: 1, state: "failed", word: "Failed · Review", at: at(-120) },
    ],
    origin: { kind: "workflow_run", id: "wfr_182", label: "Ship it #182" },
    agents: [
      { role_key: "software_engineer", state: "working" }, { role_key: "chief_architect", state: "done" },
      { role_key: "qa", state: "done" },
    ],
    stations: stations("build"),
    attention: [],
    story: [
      { type: "boss", id: "b1", station: "plan", what: "cloned", text: "Cloned the repo", at: at(0), ms: 4100 },
      { type: "act", id: "act_PLAN0000000001", station: "plan", act: act("act_PLAN0000000001", "chief_architect", "complete", "Planning", "Plan · Add CSV export", 0, 2),
        title: "Plan", seat: null, round: 1, word: "Done", verdict: null, files: null, tool_calls: 0,
        comments_authored: 0, comments_fixed: 0, said: "Add an export button and a CSV writer.", failure: null },
      { type: "act", id: "act_TESTS0000000001", station: "tests", act: act("act_TESTS0000000001", "qa", "complete", "Testing", "Write tests · Add CSV export", 2, 5),
        title: "Write tests", seat: null, round: 1, word: "Done", verdict: null, files: ["tests/export.test.ts", "tests/csv.test.ts"],
        tool_calls: 12, comments_authored: 0, comments_fixed: 0, said: null, failure: null },
      { type: "you", id: "y1", station: "build", what: "steered", actor: "ceo", text: "Use the shared CSV writer", at: at(5),
        applied_at: at(6), applied_act_id: "act_BUILD0000000001", applied_role: "software_engineer" },
      { type: "act", id: "act_BUILD0000000001", station: "build", act: act("act_BUILD0000000001", "software_engineer", "active", "Coding", "Implement · Add CSV export", 6, null),
        title: "Implement", seat: null, round: 1, word: "Working", verdict: null, files: null, tool_calls: 31,
        comments_authored: 0, comments_fixed: 0, said: null, failure: null },
    ],
    now: { act_id: "act_BUILD0000000001", role_key: "software_engineer", tool: "Edit", arg: "src/invoices/export.ts", at: at(9) },
    outputs: {
      merge_ready: { ready: false, word: "Building" },
      pr: null,
      checks: { word: "After review", advisory: false, gates: [] },
      tests: { word: "2 of 2 commands passed", role_key: "qa", output_tail: "ok\nok",
               commands: [{ command: "npm test", exit_code: 0, ok: true, timed_out: false },
                          { command: "npm run lint", exit_code: 0, ok: true, timed_out: false }] },
      changes: { word: "2 files", tests: [{ path: "tests/export.test.ts", bytes: 812 }, { path: "tests/csv.test.ts", bytes: 400 }],
                 code: [], tests_role_key: "qa", code_role_key: "software_engineer" },
      comments: { word: "None", items: [] },
      preview: null,
      deploy: { word: "Not set up", items: [] },
      cost: { word: "$0.31 · 3 agents", metered_usd: 0.31, subscription_calls: 0,
              by_agent: [{ role_key: "software_engineer", turns: 41, tokens: 182000, usd: 0.22 },
                         { role_key: "qa", turns: 12, tokens: 40000, usd: 0.05 },
                         { role_key: "chief_architect", turns: 4, tokens: 20000, usd: 0.04 }],
              by_station: { plan: 0.04, tests: 0.05, build: 0.22 } },
    },
    ...over,
  };
}

/** The cockpit of a build whose checks failed (Review stuck, a needs-you strip, Checks open). */
export function cockpitChecksFailed(): W {
  const base = cockpit();
  return {
    ...base,
    run: { ...(base.run as W), id: "crun_cf", status: "blocked", phase: "awaiting_gate_override" },
    state: { state: "needs_waiver", word: "Checks failed", tip: "Review passed · Security clean", verb: "Stuck",
             station: "review", k: 4, n: 8, rounds: 1, preview: null },
    live: false, stoppable: true, retryable: true, retry_reason: null,
    stations: stations("review", "stuck", [{ key: "review", seats: seats2("stuck") }]),
    attention: [attentionGates()],
    now: null,
    outputs: {
      ...(base.outputs as W),
      merge_ready: { ready: false, word: "Not ready · 1 open blocker" },
      checks: { word: "2 failed", advisory: false, gates: [
        { name: "code_written", label: "Code written", ok: true, role_key: "software_engineer", station: "build", waivable: true },
        { name: "code_review_passed", label: "Review passed", ok: false, role_key: "adversarial_reviewer", station: "review", evidence: "1 blocker open", waivable: true },
        { name: "security_clean", label: "Security clean", ok: false, role_key: "security_reviewer", station: "review", waivable: true },
      ] },
      comments: { word: "1 open · 0 fixed", items: [
        { id: "rc1", round: 1, severity: "blocker", body: "CSV injection: cells starting with = are not escaped.",
          path: "src/invoices/export.ts", line: 42, author: "Rook", author_role_key: "adversarial_reviewer", status: "open" },
      ] },
    },
  };
}

/** A finished build with an open PR and a live preview. */
export function cockpitPrOpen(): W {
  const base = cockpit();
  return {
    ...base,
    run: { ...(base.run as W), id: "crun_pr", status: "completed", phase: "done",
           pull_request_url: "https://github.com/acme/app/pull/842", pull_request_number: 842, decision_id: "dec_pr842" },
    state: { state: "pr_open", word: "PR open", verb: "Needs you", station: "ship", k: 7, n: 8, rounds: 0, preview: "running" },
    live: false, stoppable: false, retryable: true,
    stations: stations("ship", "needs_you", [
      { key: "pr", links: [{ label: "#842 ↗", url: "https://github.com/acme/app/pull/842" }] },
      { key: "preview", links: [{ label: "Open ↗", url: "http://127.0.0.1:4100/" }] },
    ]),
    attention: [attentionPr()],
    now: null,
    outputs: {
      ...(base.outputs as W),
      merge_ready: { ready: true, word: "Ready to merge" },
      pr: { number: 842, url: "https://github.com/acme/app/pull/842", files_url: "https://github.com/acme/app/pull/842/files",
            decision_id: "dec_pr842", decision_status: "pending", made_by: 4, word: "#842 · Waiting on you", role_key: "software_engineer" },
      checks: { word: "All 3 passed", advisory: false, gates: [
        { name: "code_written", label: "Code written", ok: true, role_key: "software_engineer", station: "build", waivable: true },
        { name: "code_review_passed", label: "Review passed", ok: true, role_key: "adversarial_reviewer", station: "review", waivable: true },
        { name: "app_serves", label: "App starts", ok: true, role_key: null, station: "check", waivable: false },
      ] },
      preview: { id: "prev_1", status: "running", word: "Live ↗", url: "http://127.0.0.1:4100/", expires_at: at(240), steps_done: 3, steps_total: 3 },
    },
  };
}

/** A failed build: the Story's last act carries the failure sentence. */
export function cockpitFailed(): W {
  const base = cockpit();
  const story = (base.story as W[]).map((r) => (r.id === "act_BUILD0000000001"
    ? { ...r, act: { ...(r.act as W), status: "error", ended_at: at(14) }, word: "Stuck",
        failure: { line: "Failed · retry may fix", tip: "npm test exited 1" } }
    : r));
  return {
    ...base,
    run: { ...(base.run as W), status: "failed", phase: "implement", error: "npm test exited 1" },
    state: { state: "failed", word: "Failed · Build", tip: "Failed · retry may fix", verb: "Stuck", station: "build", k: 2, n: 8, rounds: 0, preview: null },
    live: false, stoppable: false, retryable: true, retry_reason: null,
    failure: { line: "Failed · retry may fix", tip: "npm test exited 1" },
    stations: stations("build", "stuck"), story, now: null,
  };
}

/** A build the operator stopped. */
export function cockpitCancelled(): W {
  const base = cockpitFailed();
  return {
    ...base,
    run: { ...(base.run as W), status: "cancelled" },
    state: { state: "cancelled", word: "Stopped by you", tip: "Cancelled at Build", verb: "Stuck", station: "build", k: 2, n: 8, rounds: 0, preview: null },
    failure: null,
    stations: stations("build", "skipped", ["check", "review", "pr", "preview", "ship"].map((key) => ({ key, status: "skipped", word: "Skipped", face: "none" }))),
    story: [...(base.story as W[]), { type: "you", id: "y2", station: "build", what: "stopped", actor: "ceo", at: at(15) }],
  };
}
