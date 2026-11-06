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
  { id: "wi_visual", label: "Visual refresh finish", kind: "feature", state: "needs_you", age: "3h", source: "",
    stages: STAGES6(["done", "done", "done", "done", "now", "todo"]), agents: [juno, forge, vera], cost_usd: 2.1,
    links: { workflow_run: `?${new URLSearchParams({ project_id: P, area: "delivery_workflows", wfr: "wfr_43" })}`,
             build: `?${new URLSearchParams({ project_id: P, area: "code_tasks", build: "crun_43" })}`,
             pr: { number: 3, external_url: "https://github.com/paramg/helpdesk-ai-demo/pull/3" }, deploy: "", preview: "http://203.0.113.20:8104" },
    detail: "Ready to merge: tests 148/148, review clean. Only a person merges.", href: href({ tab: "work", drawer: "work:wi_visual" }) },
  { id: "wi_search", label: "Ticket search filters", kind: "feature", state: "running", age: "12m", source: "roadmap",
    stages: STAGES6(["done", "done", "now"]), agents: [juno, forge], cost_usd: 0.9,
    links: { workflow_run: `?${new URLSearchParams({ project_id: P, area: "delivery_workflows", wfr: "wfr_44" })}`,
             build: `?${new URLSearchParams({ project_id: P, area: "code_tasks", build: "crun_44" })}`, pr: null, deploy: "", preview: "" },
    detail: "Forge is building the filter bar; 3 of 6 steps done.", href: href({ tab: "work", drawer: "work:wi_search" }) },
  { id: "wi_email", label: "Email-to-ticket intake", kind: "feature", state: "planned", age: "", source: "roadmap", stages: STAGES6([]),
    agents: [juno], cost_usd: null, links: { workflow_run: "", build: "", pr: null, deploy: "", preview: "" }, detail: "Next up, ranked by Marshal.",
    href: href({ tab: "work", drawer: "work:wi_email" }) },
  { id: "wi_sla", label: "SLA timer ignores weekends", kind: "bug", state: "planned", age: "", source: "issue #12", stages: STAGES6([]),
    agents: [], cost_usd: null, links: { workflow_run: "", build: "", pr: null, deploy: "", preview: "" }, detail: "GitHub issue #12.",
    href: href({ tab: "work", drawer: "work:wi_sla" }) },
  { id: "wi_tests", label: "Fix 6 failing tests", kind: "chore", state: "planned", age: "", source: "ci", stages: STAGES6([]),
    agents: [], cost_usd: null, links: { workflow_run: "", build: "", pr: null, deploy: "", preview: "" }, detail: "tests/test_notify.py fails on the default branch.",
    href: href({ tab: "work", drawer: "work:wi_tests" }) },
  { id: "wi_ui", label: "UI refresh", kind: "feature", state: "done", age: "Wed", source: "", stages: STAGES6(["done", "done", "done", "done", "done", "done"]),
    agents: [juno, forge, vera], cost_usd: 3.4,
    links: { workflow_run: `?${new URLSearchParams({ project_id: P, area: "delivery_workflows", wfr: "wfr_41" })}`, build: "",
             pr: { number: 2, external_url: "https://github.com/paramg/helpdesk-ai-demo/pull/2" }, deploy: href({ tab: "deployments" }), preview: "" },
    detail: "Merged and deployed to staging.", href: href({ tab: "work", drawer: "work:wi_ui" }) },
  { id: "wi_slat", label: "SLA timers", kind: "feature", state: "done", age: "Mon", source: "", stages: STAGES6(["done", "done", "done", "done", "done", "done"]),
    agents: [juno, forge, vera], cost_usd: 4.8,
    links: { workflow_run: `?${new URLSearchParams({ project_id: P, area: "delivery_workflows", wfr: "wfr_38" })}`, build: "",
             pr: { number: 1, external_url: "https://github.com/paramg/helpdesk-ai-demo/pull/1" }, deploy: href({ tab: "deployments" }), preview: "" },
    detail: "Merged and deployed to staging.", href: href({ tab: "work", drawer: "work:wi_slat" }) },
];

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
      action: { label: "Review PR", href: "https://github.com/paramg/helpdesk-ai-demo/pull/3", decision_id: "dec_merge3" } },
    { id: "dec_prodhost", kind: "deploy", label: "Pick a prod host", detail: "Beacon can't choose this: production runs where you say.",
      mini: [], action: { label: "Pick host", href: href({ tab: "deployments" }), decision_id: "" } },
  ],
  tiles: {
    open_work: { count: 9, by_state: { running: 1, needs_you: 1, planned: 4, done: 3 }, href: href({ tab: "work" }) },
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
  repos: FX_OVERVIEW.project.repos,
  dossier: { stack: ["Python 3.11", "FastAPI", "React 18", "Vite", "Postgres 16", "Docker"],
             commands: { install: "make setup", test: "make test", build: "npm run build", run: "docker compose up" },
             tests: { passed: 142, total: 148 },
             hotspots: [{ path: "app/tickets/sla.py", label: "SLA engine", why: "Changed in 9 runs", heat: 9 },
                        { path: "web/src/pages/TicketList.tsx", label: "Ticket list", why: "Changed in 6 runs", heat: 6 },
                        { path: "app/notify/email.py", label: "Email notify", why: "Changed in 4 runs", heat: 4 }],
             built_at: "3h ago", href: "?area=knowledge" },
  graph: { nodes: [{ id: "ticket", label: "Ticket", kind: "entity" }, { id: "customer", label: "Customer", kind: "entity" },
                   { id: "org", label: "Org", kind: "entity" }, { id: "sla", label: "SLA policy", kind: "rule" },
                   { id: "slapy", label: "sla.py", kind: "code" }, { id: "esc", label: "Escalation", kind: "rule" }],
           edges: [{ from: "ticket", to: "customer", label: "" }, { from: "customer", to: "org", label: "" }, { from: "ticket", to: "sla", label: "" },
                   { from: "ticket", to: "slapy", label: "" }, { from: "sla", to: "esc", label: "breach → escalate" }, { from: "ticket", to: "esc", label: "" }],
           counts: { nodes: 412, rules: 9, learned: 14 }, href: "?area=knowledge" },
  docs: [{ label: "PRD · SLA timers", kind: "prd", sub: "Juno · approved Nov 3", href: "?area=meetings" },
         { label: "PRD · Visual refresh", kind: "prd", sub: "Juno · approved Nov 5", href: "?area=meetings" },
         { label: "ADR-001 · Postgres queue", kind: "adr", sub: "from repo /docs", href: "https://github.com/paramg/helpdesk-ai-demo/tree/main/docs" }],
  decisions: [{ label: "Staging deploys on merge", at: "Nov 2", href: "?area=budget" }],
  learned: [{ label: "Tests need TZ=UTC", sub: "Run #38 · reused 2×", state: "used", href: "?area=knowledge" },
            { label: "Migrate before restart", sub: "Run #41 · reused 1×", state: "used", href: "?area=knowledge" },
            { label: "UI tokens in theme.ts", sub: "Run #43", state: "new", href: "?area=knowledge" }],
};

export const FX_DEPLOYMENTS = {
  ship_path: [{ label: "PR merged", state: "done" }, { label: "Image built", state: "done" }, { label: "Staging · auto", state: "done" },
              { label: "Smoke test", state: "done" }, { label: "Prod · you approve", state: "waiting" }],
  environments: [{ key: "staging", label: "Staging", url: "http://203.0.113.20:8004", health: "healthy", host: "203.0.113.20",
                   host_href: "?area=infra&host=h_202", uptime_pct: 99.9, p95_ms: 180, error_rate: 0.1,
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
  state: "needs_you",
  now: "Beacon is checking staging port 8004",
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
  backlog: [{ id: "b1", label: "Fix 6 failing tests", source: "ci", state: "proposed" }, { id: "b2", label: "SLA ignores weekends", source: "issue #12", state: "proposed" },
            { id: "b3", label: "SLA timers", source: "issue #9", state: "proposed" }, { id: "b4", label: "UI refresh", source: "TODO ×7", state: "proposed" }],
  first_work: { label: "Fix 6 failing tests", source: "ci", estimate_min: 15, estimate_usd: 1.5, start: { href: "/v1/projects/helpdesk-ai-demo/work" }, others_href: href({ tab: "roadmap" }) },
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
                   slo: { target: 99.5, burn: "ok" }, href: "?area=observability&app=helpdesk" }],
  alerts: [{ id: "al_5xx", label: "5xx on /api/tickets", severity: "page", state: "resolved", since: "Thu", href: "?area=observability&view=alerts" }],
  incidents: [{ id: "wi_inc1", label: "tickets 5xx 6.6%", state: "resolved", graph_href: "?area=agent_ops&item=wi_inc1",
                mini: [{ label: "Alert", state: "done" }, { label: "Beacon triaged", state: "done" }, { label: "Rollback", state: "done" }, { label: "Resolved", state: "done" }] }],
  monitoring_pack: { rules: 6, managed_in: "repo", href: "?area=observability&view=rules" },
  links: { grafana: "", logs: "?area=activity", observability: "?area=observability&app=helpdesk" },
};

/** Sample mode: a project id picks its own card; anything else is the HelpDesk sample. */
export const FX_PROJECT_ID = P;
export const FX_ONBOARDING_PROJECT = "northpeak-retail";
