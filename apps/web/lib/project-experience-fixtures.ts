/**
 * The contract's sample project (docs/product/PROJECT_EXPERIENCE_CONTRACT.md, the wireframes'
 * HelpDesk AI Demo), as the RAW JSON each route returns. Tests render from these, and `?px_demo=1`
 * shows them with a "Sample data" banner. Never shown otherwise.
 */
const P = "helpdesk-ai-demo";
const href = (extra: Record<string, string>) => `?${new URLSearchParams({ project_id: P, area: "project", ...extra })}`;
const helm = { name: "Helm", role_key: "engineering_captain", avatar: "" };
const juno = { name: "Juno", role_key: "pm", avatar: "", role: "Plans" };
const forge = { name: "Forge", role_key: "software_engineer", avatar: "", role: "Builds" };
const vera = { name: "Vera", role_key: "qa", avatar: "", role: "Tests" };
const beacon = { name: "Beacon", role_key: "sre", avatar: "", role: "Deploys" };
const marshal = { name: "Marshal", role_key: "chief_of_staff", avatar: "", role: "Ranks" };
const STAGES6 = (states: string[]) => ["plan", "design", "build", "review", "pr", "ship"].map((key, i) => ({
  key, label: ["P", "D", "B", "R", "PR", "S"][i], state: states[i] ?? "todo" }));

export const FX_SUMMARY = {
  items: [
    { id: P, name: "HelpDesk AI Demo", initials: "HD", repo: "paramg/helpdesk-ai-demo", stage: "working",
      journey: [{ key: "onboarding", label: "Onboard", state: "done" }, { key: "ready", label: "Ready", state: "done" },
                { key: "working", label: "Working", state: "now" }, { key: "staging", label: "Deployed", state: "done" }],
      needs_you: 2, running: 1, health: "healthy", live_url: "http://203.0.113.20:8004", captain: helm, cost_week: 14.2,
      series: [3, 5, 4, 7, 5, 8, 6], updated_at: "2026-11-06T10:00:00Z", href: href({}) },
    { id: "northpeak-retail", name: "NorthPeak Retail", initials: "NP", repo: "northpeak/saleor", stage: "onboarding",
      journey: [{ key: "onboarding", label: "Onboard", state: "done" }, { key: "ready", label: "Ready", state: "now" },
                { key: "working", label: "Working", state: "todo" }, { key: "staging", label: "Deployed", state: "todo" }],
      needs_you: 0, running: 1, health: "unknown", live_url: "", captain: helm, cost_week: 6.9, series: [1, 2, 2, 3, 2, 4, 3],
      updated_at: "2026-11-06T09:00:00Z", href: `?${new URLSearchParams({ project_id: "northpeak-retail", area: "project" })}` },
    { id: "hello-world", name: "Hello World", initials: "HW", repo: "paramg/hello-world", stage: "staging",
      journey: [{ key: "onboarding", label: "Onboard", state: "done" }, { key: "ready", label: "Ready", state: "done" },
                { key: "working", label: "Working", state: "done" }, { key: "staging", label: "Deployed", state: "done" }],
      needs_you: 0, running: 0, health: "healthy", live_url: "http://203.0.113.20:8100", captain: helm, cost_week: 1.5, series: [1, 1, 0, 0, 1, 0, 0],
      updated_at: "2026-11-02T09:00:00Z", href: `?${new URLSearchParams({ project_id: "hello-world", area: "project" })}` },
  ],
};

const WORK = [
  { id: "wi_visual", run_number: 43, label: "Visual refresh finish", kind: "feature", state: "needs_you", age: "3h", source: "",
    stages: STAGES6(["done", "done", "done", "done", "now", "todo"]), agents: [juno, forge, vera], cost_usd: 2.1,
    links: { workflow_run: `?${new URLSearchParams({ project_id: P, area: "delivery_workflows", wfr: "wfr_43" })}`,
             build: `?${new URLSearchParams({ project_id: P, area: "code_tasks", build: "crun_43" })}`,
             pr: { number: 3, external_url: "https://github.com/paramg/helpdesk-ai-demo/pull/3" }, deploy: "", preview: "http://203.0.113.20:8104" },
    detail: "Ready to merge: tests 148/148, review clean. Only a person merges.", href: href({ tab: "work", drawer: "work:wi_visual" }) },
  { id: "wi_search", run_number: 44, label: "Ticket search filters", kind: "feature", state: "running", age: "12m", source: "roadmap",
    stages: STAGES6(["done", "done", "now"]), agents: [juno, forge], cost_usd: 0.9,
    links: { workflow_run: `?${new URLSearchParams({ project_id: P, area: "delivery_workflows", wfr: "wfr_44" })}`,
             build: `?${new URLSearchParams({ project_id: P, area: "code_tasks", build: "crun_44" })}`, pr: null, deploy: "", preview: "" },
    detail: "Forge is building the filter bar; 3 of 6 steps done.", href: href({ tab: "work", drawer: "work:wi_search" }) },
  // One build, its retries and three stacked checks-failing follow-ups: ONE row, the newest
  // attempt stuck `repairing` at phase done with nothing moving — Stalled, never Running.
  { id: "crun_aml", label: "AML flagged line by kind", title: "AML flagged line by kind", kind: "feature", state: "stalled", age: "2d",
    source: "code_task", source_label: "Build", stalled_since: "2026-11-04T09:30:00Z",
    stages: STAGES6(["skipped", "skipped", "done", "failed", "todo", "skipped"]), agents: [forge], cost_usd: 4.8,
    links: { workflow_run: "", build: `?${new URLSearchParams({ project_id: P, area: "code_tasks", view: "apps", build: "crun_aml_f3" })}`,
             pr: { number: 6, external_url: "https://github.com/paramg/helpdesk-ai-demo/pull/6" }, deploy: "", preview: "" },
    detail: "Auto review-repair round 1 (gates: tests_pass); the repair never started.", href: href({ tab: "work", drawer: "build:crun_aml" }),
    attempt_count: 7, followups: 3, retries: 3,
    attempts: [
      ["crun_aml_f3", "Follow-up 3 · checks failing", "followup", "stalled", "2026-11-04T09:00:00Z", 0.3, 0],
      ["crun_aml_f2r", "Follow-up 2 · retry 1", "retry", "done", "2026-11-03T16:00:00Z", 0.4, 0],
      ["crun_aml_f2", "Follow-up 2 · checks failing", "followup", "handed_on", "2026-11-03T15:00:00Z", 0.6, 0],
      ["crun_aml_f1r", "Follow-up 1 · retry 1", "retry", "done", "2026-11-03T14:00:00Z", 0.5, 0],
      ["crun_aml_f1", "Follow-up 1 · checks failing", "followup", "handed_on", "2026-11-03T13:00:00Z", 0.7, 0],
      ["crun_aml_r1", "Retry 1", "retry", "done", "2026-11-03T12:00:00Z", 0.9, 6],
      ["crun_aml", "Build", "build", "handed_on", "2026-11-03T11:00:00Z", 1.4, 0],
    ].map(([id, label, kind, state, at, cost, pr]) => ({
      id, label, kind, state, created_at: at, cost_usd: cost, stalled_since: state === "stalled" ? "2026-11-04T09:30:00Z" : "",
      pr: pr ? { number: pr, external_url: `https://github.com/paramg/helpdesk-ai-demo/pull/${pr}` } : null,
      build: `?${new URLSearchParams({ project_id: P, area: "code_tasks", view: "apps", build: String(id) })}`,
      href: href({ tab: "work", drawer: `build:${id}` }),
    })) },
  // PLANNED, in Marshal's rank (plan._planned): Next up first, with who plans it; each carries
  // its own start body and the re-rank route.
  ...[
    ["wi_email", "Email-to-ticket intake", "feature", "ticket", "Feature", "next", 1, "", null, "Turn support emails into tickets."],
    ["wi_sla", "SLA timer ignores weekends", "bug", "bug", "Bug", "next", 2, "", 12, "SLA timers keep counting through the weekend."],
    ["wi_tests", "Fix 6 failing tests", "bug", "ci", "CI", "next", 3, "tests/test_notify.py", null, "tests/test_notify.py fails on the default branch."],
    ["wi_index", "Index tickets by status", "chore", "roadmap", "from Roadmap", "later", 1, "", null, "tickets(status) has no index; the list p95 is 1.4 s."],
  ].map(([id, label, kind, origin, originLabel, lane, rank, where, issue, detail], i) => ({
    id, label, title: label, kind, state: "planned", age: "", source: "roadmap", source_label: "Roadmap", stages: STAGES6([]),
    origin, origin_label: originLabel, lane, rank, where, next_up: i === 0, agents: i === 0 ? [juno] : [], cost_usd: 0,
    links: { workflow_run: "", build: "", pr: null, deploy: "", preview: "",
             issue: issue ? { provider: "github", number: issue, label: `#${issue}`, kind: "issue", external_url: `https://github.com/paramg/helpdesk-ai-demo/issues/${issue}` } : null },
    detail, href: href({ tab: "roadmap", drawer: `rec:${id}` }),
    move: { href: `/v1/projects/${P}/plan/${id}`, method: "POST" },
    start: { href: `/v1/projects/${P}/work`, method: "POST",
             body: { kind: kind === "chore" ? "feature" : kind, text: `${label}. ${detail}`, title: label, source_ref: `rec:${id}`,
                     ...(issue ? { source_url: `https://github.com/paramg/helpdesk-ai-demo/issues/${issue}` } : {}) } },
  })),
  { id: "wi_ui", run_number: 41, label: "UI refresh", kind: "feature", state: "done", age: "Wed", source: "", stages: STAGES6(["done", "done", "done", "done", "done", "done"]),
    agents: [juno, forge, vera], cost_usd: 3.4,
    links: { workflow_run: `?${new URLSearchParams({ project_id: P, area: "delivery_workflows", wfr: "wfr_41" })}`, build: "",
             pr: { number: 2, external_url: "https://github.com/paramg/helpdesk-ai-demo/pull/2" }, deploy: href({ tab: "deployments" }), preview: "" },
    detail: "Merged and deployed to staging.", href: href({ tab: "work", drawer: "work:wi_ui" }) },
  { id: "wi_slat", run_number: 38, label: "SLA timers", kind: "feature", state: "done", age: "Mon", source: "", stages: STAGES6(["done", "done", "done", "done", "done", "done"]),
    agents: [juno, forge, vera], cost_usd: 4.8,
    links: { workflow_run: `?${new URLSearchParams({ project_id: P, area: "delivery_workflows", wfr: "wfr_38" })}`, build: "",
             pr: { number: 1, external_url: "https://github.com/paramg/helpdesk-ai-demo/pull/1" }, deploy: href({ tab: "deployments" }), preview: "" },
    detail: "Merged and deployed to staging.", href: href({ tab: "work", drawer: "work:wi_slat" }) },
  // THE ONBOARDING AGENT'S RUN as a piece of work (05-27): Setup, its own mini flow, Helm and Marshal.
  { id: "onb_36", label: "Onboarding", title: "Onboarding", kind: "setup", state: "done", age: "4d", source: "onboarding", source_label: "Setup",
    created_at: "2026-11-02T09:00:00Z", updated_at: "2026-11-02T09:06:00Z",
    flow: [{ key: "discover", label: "Discover", state: "done" }, { key: "ready", label: "Ready", state: "done" }, { key: "backlog", label: "Backlog", state: "done" }],
    stages: STAGES6(["skipped", "skipped", "skipped", "skipped", "skipped", "skipped"]), agents: [helm, marshal], cost_usd: 1.2, waiting: [],
    links: { workflow_run: "", build: "", pr: null, deploy: "", preview: "", onboarding: href({ tab: "work", drawer: "onboarding" }) },
    detail: "Helm onboarded the project.", href: href({ tab: "work", drawer: "onboarding" }) },
];

/** GET /v1/projects/{id}/work/plan: Helm's plan for a Feature, from Minor Feature's real steps and
 *  this project's history (3 runs: ≈ 25 min, ≈ $3). */
export const FX_WORK_PLAN = {
  kind: "feature", planner: helm,
  workflow: { key: "minor_feature", name: "Minor Feature", chosen: "default", why: "the default for a change",
              href: `?${new URLSearchParams({ project_id: P, area: "delivery_workflows", tab: "library", wf: "minor_feature" })}` },
  nodes: [
    { key: "plan", label: "Plan", type: "agent", agent: juno }, { key: "approve", label: "You approve", type: "you" },
    { key: "build", label: "Build", type: "todo", agent: helm }, { key: "review", label: "Review", type: "todo" },
    { key: "pr", label: "PR", type: "todo" }, { key: "ship", label: "Staging", type: "you" },
  ],
  estimate: { minutes: 25, minutes_basis: "history", minutes_runs: 3, usd: 3, usd_basis: "history", usd_runs: 3, metered: true },
  options: [
    { key: "minor_feature", name: "Minor Feature", detail: "A small addition to something that already exists.", work_class: "feature.minor", fits: true },
    { key: "major_feature", name: "Major Feature", detail: "A feature large enough to deserve research and a spec.", work_class: "feature.major", fits: true },
    { key: "fast_fix", name: "Fast Fix / Hotfix", detail: "Something is broken now.", work_class: "bugfix", fits: false },
  ],
};

export const FX_WORK = { items: WORK };

export const FX_OVERVIEW = {
  project: { id: P, name: "HelpDesk AI Demo", initials: "HD", stage: "working", status_label: "Working", health: "healthy",
             repos: [{ full_name: "paramg/helpdesk-ai-demo", branch: "main", access: { read: true, write: true, pr: true },
                       external_url: "https://github.com/paramg/helpdesk-ai-demo" }],
             live_url: "http://203.0.113.20:8004" },
  journey: [
    { key: "onboarding", label: "Onboarded", state: "done", sub: "Nov 2", href: href({ drawer: "stage:onboarding" }) },
    { key: "ready", label: "Ready", state: "done", sub: "6/6", href: href({ drawer: "stage:ready" }) },
    { key: "backlog", label: "Backlog", state: "done", sub: "7 of 12", href: href({ tab: "roadmap" }) },
    { key: "working", label: "Working", state: "now", sub: "3 merged", href: href({ tab: "work" }) },
    { key: "staging", label: "Staging", state: "done", sub: "2h ago", href: href({ tab: "deployments" }) },
    { key: "production", label: "Production", state: "waiting", sub: "needs you", href: href({ tab: "deployments" }) },
  ],
  needs_you: [
    { id: "dec_merge3", kind: "merge", label: "Merge PR #3", detail: "Tests 148/148 and review are clean. Only a person merges.",
      mini: [{ label: "Tests 148", state: "done" }, { label: "Review", state: "done" }, { label: "Merge", state: "waiting" }],
      work_id: "wi_visual", action: { label: "Review PR", href: "https://github.com/paramg/helpdesk-ai-demo/pull/3", decision_id: "dec_merge3" } },
    { id: "dec_prodhost", kind: "deploy", label: "Pick a prod host", detail: "Beacon can't choose this: production runs where you say.",
      mini: [], action: { label: "Pick host", href: href({ tab: "deployments" }), decision_id: "" } },
  ],
  tiles: {
    open_work: { count: 10, by_state: { running: 1, stalled: 1, needs_you: 1, planned: 4, done: 3 }, href: href({ tab: "work" }) },
    running: { count: 1, current: { label: "Search filters", progress: [3, 6], agent: forge }, href: href({ tab: "work", drawer: "work:wi_search" }) },
    staging: { health: "healthy", uptime_pct: 99.9, p95_ms: 180, series: [180, 175, 190, 182, 170, 185, 178, 180, 176, 181], href: href({ tab: "health" }) },
    cost_week: { usd: 14.2, delta_pct: -18, cap_usd: 50, series: [2, 1.5, 3, 2.2, 1.8, 2.4, 1.3], href: "?area=budget" },
  },
  captain: { agent_id: "ag_helm", name: "Helm", role_key: "engineering_captain", avatar: "", status: "Watching 1 build",
             squad: [juno, forge, vera, beacon, marshal] },
  recent_work: WORK.slice(0, 2).concat(WORK.slice(5, 6)),
  next: [
    { id: "wi_email", label: "Email-to-ticket intake", source: "roadmap", state: "next", href: href({ tab: "work", drawer: "work:wi_email" }) },
    { id: "rec_weekend", label: "SLA weekend pause test", source: "roadmap", state: "proposed", href: href({ tab: "roadmap", drawer: "rec:rec_weekend" }),
      accept: { href: "/v1/ambient/roadmap/rec_weekend/accept" } },
    { id: "rec_alert", label: "Alert on /api/tickets 5xx", source: "roadmap", state: "proposed", href: href({ tab: "roadmap", drawer: "rec:rec_alert" }),
      accept: { href: "/v1/ambient/roadmap/rec_alert/accept" } },
  ],
  try_it: { url: "http://203.0.113.20:8004",
            demo_logins: [{ user: "admin@helpdesk.demo", label: "Admin", reveal: { href: "/v1/apps/helpdesk/demo-logins?project_id=helpdesk-ai-demo&env=deploy", requires: "step_up" } }] },
  week: [
    { at: "Mon", label: "PR #1 merged", kind: "merge" }, { at: "Mon", label: "Staging deploy", kind: "deploy" },
    { at: "Wed", label: "PR #2 merged", kind: "merge" }, { at: "Thu", label: "5 ideas found", kind: "idea" },
    { at: "Today", label: "PR #3 waiting", kind: "pr" },
  ],
};

export const FX_KNOWLEDGE = {
  repos: FX_OVERVIEW.project.repos.map((r) => ({ ...r, binding_id: "ghb_demo", last_push: "3h ago", via: "GitHub App · paramg", deploys_to: "Staging",
    deploy_check: { label: "Staging", state: "done", reason: "The last deploy to staging is live" } })),
  dossier: { stack: ["Python 3.11", "FastAPI", "React 18", "Vite", "Postgres 16", "Docker"],
             commands: { install: "make setup", test: "make test", build: "npm run build", run: "docker compose up" },
             tests: { passed: 142, total: 148, failing: 6, from: "PR #3 build", files: 38 },
             code_map: { parts: [{ dir: "app", files: 90 }, { dir: "web", files: 75 }, { dir: "tests", files: 32 }, { dir: "infra", files: 17 }], files: 214 },
             hotspots: [{ path: "app/tickets/sla.py", label: "SLA engine", why: "Changed in 9 runs", heat: 9 },
                        { path: "web/src/pages/TicketList.tsx", label: "Ticket list", why: "Changed in 6 runs", heat: 6 },
                        { path: "app/notify/email.py", label: "Email notify", why: "Changed in 4 runs", heat: 4 }],
             by: "Helm", built_ago: "3h ago", built_at: "", href: "?area=knowledge" },
  graph: { hub: "ticket",
           nodes: [{ id: "ticket", label: "Ticket", kind: "table", group: "hub" }, { id: "customer", label: "Customer", kind: "table", group: "data" },
                   { id: "sla", label: "SLA policy", kind: "rule", group: "rule" }, { id: "esc", label: "Escalation", kind: "workaround", group: "learned" },
                   { id: "slapy", label: "sla.py", kind: "file", group: "code" }, { id: "org", label: "Org", kind: "table", group: "data" }],
           edges: [{ from: "ticket", to: "customer", label: "", kind: "references" }, { from: "customer", to: "org", label: "", kind: "references" },
                   { from: "ticket", to: "sla", label: "", kind: "governs" }, { from: "ticket", to: "slapy", label: "", kind: "reads" },
                   { from: "sla", to: "esc", label: "breach → escalate", kind: "escalates" }, { from: "ticket", to: "esc", label: "", kind: "about" }],
           counts: { nodes: 412, rules: 9, learned: 14 }, href: "?area=knowledge" },
  docs: [{ label: "PRD · SLA timers", kind: "prd", sub: "Juno · approved Nov 3", href: "?area=meetings&meeting=m1" },
         { label: "PRD · Visual refresh", kind: "prd", sub: "Juno · approved Nov 5", href: "?area=meetings&meeting=m2" },
         { label: "ADR-001 · Postgres queue", kind: "adr", sub: "from repo /docs", detail: "docs/adr-001-postgres-queue.md", href: "?area=knowledge",
           external_url: "https://github.com/paramg/helpdesk-ai-demo/tree/main/docs" }],
  decisions: [{ label: "Staging deploys on merge", at: "Nov 2", sub: "Policy · you, Nov 2", href: "?area=decisions&decision=d1" }],
  learned: [{ id: "l1", label: "Tests need TZ=UTC", sub: "Run #38 · reused 2×", state: "used", reused: 2, href: "?area=knowledge", agent: { name: "Forge", role_key: "software_engineer" } },
            { id: "l2", label: "Migrate before restart", sub: "Run #41 · reused 1×", state: "used", reused: 1, href: "?area=knowledge", agent: { name: "Vera", role_key: "qa" } },
            { id: "l3", label: "UI tokens in theme.ts", sub: "Run #43", state: "new", reused: 0, href: "?area=knowledge", agent: { name: "Rook", role_key: "adversarial_reviewer" } }],
};

export const FX_DEPLOYMENTS = {
  ship_path: [{ label: "PR merged", state: "done" }, { label: "Image built", state: "done" }, { label: "Staging · auto", state: "done" },
              { label: "Smoke test", state: "done" }, { label: "Prod · you approve", state: "waiting" }],
  environments: [{ key: "staging", label: "Staging", url: "http://203.0.113.20:8004", health: "healthy", host: "203.0.113.20",
                   host_href: "?area=infra&host=h_202", uptime_pct: 99.9, p95_ms: 180, error_rate: 0.1,
                   hours: Array.from({ length: 24 }, (_, i) => ({ at: new Date(Date.UTC(2026, 10, 6, i)).toISOString(), n: 12, ok: i === 14 ? 9 : 12, state: i === 14 ? "degraded" : "healthy" })),
                   series: [1, 1, 1, 1, 1, 1, 1, 1, 0.6, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
                   last_deploy: { commit: "a41c9e2", at: "2h ago", by: "Beacon", label: "PR #2", href: href({ tab: "deployments", drawer: "deploy:dep_2" }) },
                   demo_logins: [{ user: "admin@helpdesk.demo", label: "Admin", reveal: { href: "/v1/apps/helpdesk/demo-logins?project_id=helpdesk-ai-demo&env=deploy", requires: "step_up" } },
                                 { user: "agent@helpdesk.demo", label: "Agent", reveal: { href: "/v1/apps/helpdesk/demo-logins?project_id=helpdesk-ai-demo&env=deploy", requires: "step_up" } },
                                 { user: "customer@helpdesk.demo", label: "Customer", reveal: { href: "/v1/apps/helpdesk/demo-logins?project_id=helpdesk-ai-demo&env=deploy", requires: "step_up" } }],
                   policy: "auto", href: href({ tab: "deployments", drawer: "deploy:staging" }) }],
  production: { state: "needs_host", action: { label: "Pick host", href: "?area=infra&view=fleet" } },
  history: [{ id: "dep_2", label: "Staging · PR #2 UI refresh", sub: "Wed 16:40 · 3m 10s · smoke ✓", state: "done", href: href({ tab: "deployments", drawer: "deploy:dep_2" }) },
            { id: "dep_1", label: "Staging · PR #1 SLA timers", sub: "Mon 11:05 · 2m 50s · smoke ✓", state: "done", href: href({ tab: "deployments", drawer: "deploy:dep_1" }) },
            { id: "dep_0", label: "Staging · first deploy", sub: "Nov 2 · port clash → moved to 8004 by Beacon", state: "waiting", href: href({ tab: "deployments", drawer: "deploy:dep_0" }) }],
};

export const FX_ROADMAP = {
  status: "open",
  items: [
    { id: "rec_weekend", title: "Regression test for SLA weekend pause", kind: "hardening", status: "open", rank: 1, score: 82,
      badges: { kind: "Hardening", value: 82, effort: 2, risk: 1.0, evidence: 2, sources: 2 },
      strip: [{ type: "signal", label: "2 incidents" }, { type: "signal", label: "sla.py hot" }, { type: "boss", label: "Incident hotspot" }, { type: "agent", label: "Test + fix" }] },
    { id: "rec_alert", title: "Alert rule for /api/tickets 5xx", kind: "hardening", status: "open", rank: 2, score: 74,
      badges: { kind: "Hardening", value: 74, effort: 1, risk: 1.0, evidence: 1, sources: 1 },
      strip: [{ type: "signal", label: "New endpoint" }, { type: "boss", label: "Alert gap" }, { type: "agent", label: "Add alert" }] },
    { id: "rec_reply", title: "Agent reply suggestions (GPT)", kind: "feature", status: "open", rank: 3, score: 61,
      badges: { kind: "Feature", value: 61, effort: 8, risk: 1.2, evidence: 3, sources: 1 },
      strip: [{ type: "signal", label: "3 asks in PRDs" }, { type: "boss", label: "Feature demand" }, { type: "agent", label: "Write PRD" }] },
    { id: "rec_index", title: "Index tickets(status, created_at)", kind: "refactor", status: "open", rank: 4, score: 55,
      badges: { kind: "Performance", value: 55, effort: 2, risk: 1.0, evidence: 1, sources: 1 },
      strip: [{ type: "signal", label: "List p95 1.4s" }, { type: "boss", label: "Slow query" }, { type: "agent", label: "Migration" }] },
  ],
  counts: { open: 5, accepted: 7, rejected: 2, snoozed: 1 },
};

export const FX_CAPTAIN = { ...FX_OVERVIEW.captain, last_said: "PR #3 is ready for you to merge." };

export const FX_ONBOARDING = {
  state: "running", live: true, title: "Helm is reading your repo",
  now: "Beacon is checking staging port 8004", now_agent: "sre", now_live: true,
  log: [{ at: "2026-11-03T09:00:00Z", agent: "engineering_captain", label: "Started", state: "done", text: "Helm started reading the repository." },
        { at: "2026-11-03T09:03:00Z", agent: "engineering_captain", label: "Repo access", state: "done", text: "Read, branch and pull requests work." },
        { at: "2026-11-03T09:05:40Z", agent: "builder", label: "Builds clean", state: "done", text: "make setup · 1m 40s" },
        { at: "2026-11-03T09:06:10Z", agent: "sre", label: "Staging host", state: "now", text: "checking :8004" }],
  elapsed_s: 372,
  stages: [{ key: "discover", label: "Discover", state: "done", sub: "done · 3m" }, { key: "ready", label: "Ready for work", state: "now", sub: "4 of 6" },
           { key: "backlog", label: "Propose backlog", state: "todo", sub: "drafting" }, { key: "first_work", label: "Work it", state: "todo", sub: "verified loop" },
           { key: "deploy", label: "Deploy", state: "todo", sub: "staging auto" }],
  discover: { nodes: [{ label: "214 files", state: "done", sub: "" }, { label: "FastAPI+React", state: "done", sub: "" },
                      { label: "142/148 tests", state: "done", sub: "" }, { label: "3 hotspots", state: "done", sub: "" }, { label: "4 docs", state: "done", sub: "" }] },
  checklist: [
    { key: "repo_access", label: "Repo access", state: "done", owner: "agent", detail: "read · branch · PR" },
    { key: "baseline_build", label: "Builds clean", state: "done", owner: "agent", detail: "make setup · 1m 40s" },
    { key: "tests", label: "Tests run", state: "done", owner: "agent", detail: "6 failing → backlog" },
    { key: "models", label: "Workflow picked", state: "done", owner: "agent", detail: "Minor feature" },
    { key: "deploy_target", label: "Staging host", state: "now", owner: "agent", detail: "checking :8004" },
    { key: "secrets", label: "App secret", state: "todo", owner: "you", detail: "OPENAI_API_KEY — only you", action: { label: "Add", href: "?area=infra&view=deploys" } },
  ],
  // §14's shapes: ranked candidates (kind, text for the work, ref) and first work's own start.
  backlog: [{ id: "cand_b1", label: "Fix 6 failing checks", detail: "CI fails on the default branch: 6 tests.", source: "ci", kind: "bug", ref: "ci:main", state: "proposed" },
            { id: "cand_b2", label: "SLA ignores weekends", detail: "#12 SLA ignores weekends", source: "bug", kind: "bug", ref: "issue:12", state: "proposed" },
            { id: "cand_b3", label: "SLA timers", detail: "#9 SLA timers", source: "ticket", kind: "ticket", ref: "issue:9", state: "proposed" },
            { id: "cand_b4", label: "Clear a TODO cluster", detail: "7 TODOs in web/", source: "roadmap", kind: "chore", ref: "signal:todo", state: "proposed" }],
  first_work: { label: "Fix 6 failing tests", kind: "bug", source: "ci", estimate_min: 15, estimate_usd: 1.5,
                start: { href: "/v1/projects/northpeak-retail/work", method: "POST",
                         body: { kind: "bug", text: "Make the default branch's CI pass again. Failing: 6 tests.", title: "Fix 6 failing tests", source_ref: "ci:main" } },
                others_href: href({ tab: "roadmap" }) },
};

export const FX_JUMP = {
  items: [
    { label: "PR #3 · Visual refresh finish", kind: "pr", href: "https://github.com/paramg/helpdesk-ai-demo/pull/3", sub: "needs you" },
    { label: "Run #43 · workflow run", kind: "run", href: `?${new URLSearchParams({ project_id: P, area: "delivery_workflows", wfr: "wfr_43" })}`, sub: "Workflows" },
    { label: "Build · Visual refresh finish", kind: "build", href: `?${new URLSearchParams({ project_id: P, area: "code_tasks", build: "crun_43" })}`, sub: "Code Tasks" },
    { label: "Staging · 203.0.113.20:8004", kind: "deploy", href: "http://203.0.113.20:8004", sub: "open" },
  ],
};

export const FX_SETTINGS = {
  sections: [
    { key: "demo_logins", label: "Demo logins", icon: "key", moved_from: "App setup › Demo logins",
      items: [{ label: "Admin", value: "admin@helpdesk.demo", sub: "Staging", reveal: { href: "/v1/apps/helpdesk/demo-logins?project_id=helpdesk-ai-demo&env=deploy", requires: "step_up" } },
              { label: "Agent", value: "agent@helpdesk.demo", sub: "Staging", reveal: { href: "/v1/apps/helpdesk/demo-logins?project_id=helpdesk-ai-demo&env=deploy", requires: "step_up" } },
              { label: "Customer", value: "customer@helpdesk.demo", sub: "Staging", reveal: { href: "/v1/apps/helpdesk/demo-logins?project_id=helpdesk-ai-demo&env=deploy", requires: "step_up" } }],
      actions: [] },
    { key: "deploy_values", label: "Deploy values", icon: "lock", moved_from: "App setup › Settings",
      items: [{ label: "OPENAI_API_KEY", value: "not set", sub: "secret", href: "?area=infra&view=deploys" },
              { label: "DATABASE_URL", value: "generated", sub: "secret", href: "?area=infra&view=deploys" },
              { label: "DEMO_PASSWORD", value: "generated", sub: "secret", href: "?area=infra&view=deploys" }],
      actions: [{ label: "Set values", href: "?area=infra&view=deploys", requires: "" }] },
    { key: "targets", label: "Targets & hosts", icon: "server", moved_from: "Infra › Targets",
      items: [{ label: "Staging", value: "203.0.113.20:8004", sub: "auto on merge", href: "?area=infra&host=h_202" },
              { label: "Production", value: "not set up", sub: "a person approves", href: "?area=infra&view=fleet" }],
      actions: [{ label: "Add target", href: "?area=infra&view=fleet", requires: "" }] },
    { key: "captain", label: "Captain & crew", icon: "robot", moved_from: "",
      items: [{ label: "Captain", value: "Helm", sub: "Engineering Captain", href: "" }, { label: "Crew", value: "Juno · Forge · Vera · Beacon", sub: "default crew", href: "?area=workforce" }],
      actions: [] },
    { key: "autonomy", label: "Autonomy & budget", icon: "gauge", moved_from: "Govern",
      items: [{ label: "Autonomy", value: "Ask before merge", sub: "level 1", href: "?area=agent_ops" }, { label: "Budget", value: "$50 / week", sub: "28% used", href: "?area=budget" }],
      actions: [{ label: "Change in Govern", href: "?area=budget", requires: "" }] },
    { key: "repos", label: "Repositories", icon: "git", moved_from: "Repo and models",
      items: [{ label: "paramg/helpdesk-ai-demo", value: "main", sub: "read · branch · PR", href: "https://github.com/paramg/helpdesk-ai-demo" }],
      actions: [{ label: "Add repo", href: "?area=setup&focus=repository", requires: "" }] },
    { key: "notifications", label: "Notifications", icon: "bell", moved_from: "",
      items: [{ label: "Needs you", value: "Bell + email", sub: "", href: "?area=skills" }],
      actions: [] },
  ],
};

export const FX_HEALTH = {
  environments: [{ key: "staging", label: "Staging", target_id: "tgt_staging", health: "healthy", uptime_pct: 99.9, error_rate: 0.4, p95_ms: 180,
                   series: { errors: [0.2, 0.3, 0.1, 0.6, 0.4, 0.2, 0.3, 0.5, 0.4, 0.3], latency: [170, 180, 175, 190, 182, 176, 181, 185, 178, 180] },
                   slo: { target: 99.5, burn: "ok", budget_used_pct: 22 }, href: "?area=observability&app=helpdesk" }],
  alerts: [
    { id: "al_5xx", label: "5xx on /api/tickets", condition: "Custom PromQL", severity: "page", state: "resolved", since: "2026-11-06T09:26:00Z", quiet: "", href: "?area=observability&view=alerts" },
    { id: "al_p95", label: "p95 latency", condition: "> 800 ms for 5 min", severity: "ticket", state: "ok", since: "", quiet: "quiet 3d", href: "?area=observability&view=alerts" },
    { id: "al_hc", label: "Health check", condition: "3 failed probes in 10 min", severity: "page", state: "ok", since: "", quiet: "quiet 5d", href: "?area=observability&view=alerts" },
  ],
  incidents: [
    { id: "wi_inc1", label: "5xx on /api/tickets", state: "resolved", since: "2026-11-06T09:12:00Z", duration_s: 840, outcome_word: "resolved by the squad",
      graph_href: "?area=agent_ops&item=wi_inc1",
      mini: [{ label: "Alert", state: "done", type: "signal" }, { label: "SRE squad", state: "done", type: "agent" }, { label: "Pool exhausted", state: "done", type: "cause" },
             { label: "Pool 10→20", state: "done", type: "fix" }, { label: "Recovered", state: "done" }] },
    { id: "wi_inc2", label: "Slow ticket list", state: "needs_you", since: "2026-11-04T10:00:00Z", duration_s: null, outcome_word: "fix needs your approval",
      graph_href: "?area=agent_ops&item=wi_inc2",
      mini: [{ label: "p95 1.4s", state: "done", type: "signal" }, { label: "SRE squad", state: "done", type: "agent" }, { label: "Missing index", state: "done", type: "cause" },
             { label: "Approve fix", state: "waiting", type: "decision" }, { label: "Recovered", state: "todo" }] },
  ],
  monitoring_pack: { rules: 6, managed_in: "repo", href: "?area=observability&view=alerts", kinds: ["Uptime", "5xx", "Latency", "Disk", "DB conns", "TLS"],
                     file: { path: "monitoring/alerts.yaml", external_url: "https://github.com/paramg/helpdesk-ai-demo/blob/main/monitoring/alerts.yaml" } },
  links: { grafana: "", logs: "?area=activity", observability: "?area=observability&app=helpdesk" },
};

/** Sample mode: a project id picks its own card; anything else is the HelpDesk sample. */
export const FX_PROJECT_ID = P;
export const FX_ONBOARDING_PROJECT = "northpeak-retail";
