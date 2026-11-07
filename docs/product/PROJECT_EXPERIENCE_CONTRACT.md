# Project experience — API contract (BOSSAPP-913 / 914)

The console (Projects nav, guided first run, Project page, drawers, ⌘K) and the engine build against
THIS contract in parallel. Change it only together: edit this file in the same commit as the code and
say so in your report. Mockups: `docs/product/wireframes/project-experience/` (README has the click map,
"where do I find X" table and the Getting-started parity table).

Rules every response follows:
- Diagram-first: every label a node shows is ≤ 4 words (`label`); longer text lives in `detail`
  (drawer only). States are enums, never prose.
- Every item that can be opened carries `href` (a console URL built with the shared `consoleHref`
  helper's query shape, e.g. `?project_id=…&area=…&…`) and, where it exists outside BOSS, `external_url`.
- Read endpoints are cheap (one round trip per tab) and permission-scoped like existing project reads
  (`Scope.project`); writes use existing permissions and step-up rules.
- Works with the ambient preview OFF (`BOSS_AMBIENT_COS=off`): onboarding and the captain are explicit
  per-project actions, not background autonomy.

Times: every ISO time the console shows on these screens is formatted in the VIEWER's zone
(`apps/web/lib/local-time.ts` `whenLocal`/`sinceLocal`); fields already in words ("2h ago") show as sent.

## Shared enums
- `stage`: `onboarding | ready | backlog | working | staging | production`
- `state` (step/node): `done | now | waiting | todo | failed | skipped`
- `work_state`: `planned | running | stalled | needs_you | done | failed` (`stalled`: claims to work, nothing has moved — never counted as running)
- `health`: `healthy | degraded | down | unknown`

## 1. Projects list — `GET /v1/projects/summary`
```json
{"items": [{"id": "helpdesk-ai-demo", "name": "HelpDesk AI Demo", "initials": "HD",
  "repo": "paramg/helpdesk-ai-demo", "stage": "working",
  "journey": [{"key": "onboarding", "label": "Onboarded", "state": "done"}, …],
  "needs_you": 2, "running": 1, "health": "healthy", "live_url": "http://203.0.113.20:8004",
  "captain": {"name": "Helm", "role_key": "engineering_captain", "avatar": "…"},
  "updated_at": "…", "href": "?project_id=helpdesk-ai-demo&area=project"}]}
```

## 2. Project overview — `GET /v1/projects/{project_id}/overview`
```json
{"project": {"id": "…", "name": "…", "initials": "HD", "stage": "working",
   "repos": [{"full_name": "paramg/helpdesk-ai-demo", "branch": "main",
              "access": {"read": true, "write": true, "pr": true}, "external_url": "…"}],
   "live_url": "…", "status_label": "Working"},
 "journey": [{"key": "onboarding|ready|backlog|working|staging|production", "label": "Onboarded",
              "state": "done|now|waiting|todo", "sub": "Nov 2", "href": "…"}],
 "needs_you": [{"id": "dec_…", "kind": "merge|deploy|plan|question|setup", "label": "Merge PR #3",
                "detail": "…", "mini": [{"label": "Tests 148", "state": "done"}, …],
                "action": {"label": "Review PR", "href": "…", "decision_id": "dec_…"}}],
 "tiles": {"open_work": {"count": 9, "by_state": {"running": 1, "needs_you": 1, "planned": 4, "done": 3}, "href": "…"},
           "running": {"count": 1, "current": {"label": "Search filters", "progress": [3, 6], "agent": {…}}, "href": "…"},
           "staging": {"health": "healthy", "uptime_pct": 99.9, "p95_ms": 180, "series": [..], "href": "…"},
           "cost_week": {"usd": 14.2, "delta_pct": -18, "cap_usd": 50, "series": [..], "href": "…"}},
 "captain": {"agent_id": "…", "name": "Helm", "role_key": "engineering_captain", "avatar": "…",
             "status": "Watching 1 build", "squad": [{"name": "Juno", "role": "Plans", "avatar": "…"}]},
 "recent_work": [<WorkItem>, …(≤5)],
 "next": [{"id": "…", "label": "Email-to-ticket intake", "source": "roadmap|ticket|bug|ci",
           "state": "next|proposed", "href": "…", "accept": {"href": "…"}}],
 "try_it": {"url": "…", "demo_logins": [{"user": "agent@demo.test", "label": "Agent",
            "reveal": {"href": "…", "requires": "step_up"}}]},
 "week": [{"at": "…", "label": "PR #1 merged", "kind": "merge|deploy|idea|pr|incident"}]}
```

## 3. Work — `GET /v1/projects/{project_id}/work?state=&q=`
`WorkItem`:
```json
{"id": "…", "label": "Ticket search filters", "kind": "feature|bug|ticket|chore|incident",
 "state": "planned|running|needs_you|done|failed", "age": "12m",
 "stages": [{"key": "plan|design|build|review|pr|ship", "label": "P", "state": "done|now|todo|failed"}],
 "agents": [{"name": "Helm", "avatar": "…"}], "cost_usd": 2.1,
 "links": {"workflow_run": "…href", "build": "…href", "pr": {"number": 3, "external_url": "…"},
           "deploy": "…href", "preview": "…external_url"},
 "detail": "…", "href": "…"}
```
`POST /v1/projects/{project_id}/work` `{ "kind": "feature|bug|ticket", "text": "…" }` → starts the
project's normal path (the same entry the existing "What should we add?" box uses: workflow/start or
code task) and returns `{ "work": <WorkItem>, "href": "…" }`. Step-up/consent errors pass through
unchanged so the console's existing sheets handle them.

**One row per piece of work (2026-11-06).** A follow-up ("Follow-up: checks failing — …") and a
retry ("· retry N") are attempts of their root build (finalize_ctx `root_run_id`; a build a
workflow run started joins that run's row), never rows of their own. Each WorkItem also carries
`source_label` (`Build|Workflow|Roadmap`, never the raw `source`), `stalled_since` (ISO, when
`state` is `stalled`), `attempt_count`, `followups`, `retries` and `attempts[]` (newest first, empty
for a single attempt): `{id, label: "Build|Retry 1|Follow-up 2 · checks failing|Follow-up 2 · retry 1",
kind: build|retry|followup, state: work_state|handed_on, status, created_at, stalled_since,
cost_usd, pr, build, href}`. `title` is the root's clean title (no stacked prefixes); `kind` is
`fix` for a build that is itself a checks-failing follow-up. `counts` has `stalled`. The stall
rule is run_liveness.stalled_since (phase done + no live job + 20 min quiet, or silent past the
stall horizon), the same one the Factory floor reads.

## 4. Repo & knowledge — `GET /v1/projects/{project_id}/knowledge`
```json
{"repos": [<repo as in §2>], "dossier": {"stack": ["Node", "React"], "commands": {"build": "…", "test": "…", "run": "…"},
 "hotspots": [{"path": "server/sla.mjs", "label": "SLA engine", "why": "…"}], "todo_clusters": [..],
 "owners": [..], "built_at": "…", "href": "…"},
 "graph": {"nodes": [..], "edges": [..], "href": "?project_id=…&area=knowledge"},
 "docs": [{"label": "PRD: SLA timers", "kind": "prd|adr|readme|runbook", "href": "…"}],
 "decisions": [{"label": "Timer from created_at", "at": "…", "href": "…"}]}
```

## 5. Deployments — `GET /v1/projects/{project_id}/deployments`
```json
{"environments": [{"key": "staging", "label": "Staging", "url": "…", "health": "healthy",
  "host": "203.0.113.20", "host_href": "?area=infra&host=<host id>", "target_id": "tgt_…", "last_deploy": {"commit": "f3f130c", "at": "…", "by": "…", "href": "…"},
  "demo_logins": [{"user": "agent@demo.test", "label": "Agent", "reveal": {"href": "…"}}],
  "policy": "auto|person", "href": "…"}], "production": {"state": "none|needs_host|ready", "action": {…}}}
```
Demo-login reveal: owner/admin with step-up (existing secrets reveal path), returns user + password.
`reveal.href` is a GET (like today's `GET /v1/apps/{app}/demo-logins?env=`) answering `{"password": "…"}` or
`{"demo_logins": [{"user"|"name": "…", "password"|"value": "…"}]}`; a stale second factor answers
`401 step_up_required` and the console opens "Confirm it's you" (console, 2026-11-06).
`environments[].target_id` is the deploy target (Observability's app id): Health shows its alert rules with it.
**Infra cross-link:** `GET /v1/infra/hosts/{id}` (and `/v1/infra/fleet` racks) residents carry
`"project": {"id": "…", "name": "…", "href": "?project_id=…&area=project&tab=deployments"}` so a
shared server lists "Projects on this server", each linking to that Project › Deployments.

## 6. Roadmap — existing `GET /v1/ambient/roadmap?project_id=…&status=open`
(+ `POST /v1/ambient/roadmap/{work_item_id}/accept {as: ticket|epic|prd, note, project_id}`, `/reject {reason,
category}`, `/snooze {until, reason}`; `GET /v1/ambient/roadmap/detectors`). There is no
`/v1/ambient/recommendations` route: the recommendations live at `/v1/ambient/roadmap` (table
`ambient_recommendations`). Must work with the ambient preview off: listing/accept/reject are
reads/writes on stored items; generation is the captain's.

## 7. Captain
- `GET /v1/projects/{project_id}/captain` → captain + squad (§2 shape) + `last_said`.
- `POST /v1/projects/{project_id}/captain {"agent_id": "…"}` (owner/admin) — change captain.
- Chat: the existing Ask BOSS dock with subject `{"kind": "project", "id": project_id}` (console: `project` is in
  `apps/web/lib/dock-subject.ts` SUBJECT_KINDS, so the engine's `turn_subject.RESOLVERS` needs a `project`
  resolver — engine/tests/test_an_ask_about_this_is_answered_from_its_record.py checks it) — one agent
  path (no second chat engine); answers may cite `[act:<id>]` / work hrefs.

## 8. Onboarding agent — `POST /v1/projects/{project_id}/onboarding/start`, `GET …/onboarding`
Started automatically when a project is created (New or Existing) and on demand.
```json
{"state": "running|ready|needs_you|failed",
 "stages": [{"key": "discover|ready|backlog|first_work", "label": "Discover", "state": "done|now|todo|failed",
             "sub": "118 / 214 files"}],
 "discover": {"nodes": [{"label": "Stack found", "state": "done", "sub": "FastAPI · React"},
                        {"label": "Mapping code", "state": "now", "sub": "118 / 214 files"},
                        {"label": "Tests run", "state": "done", "sub": "142 / 148 pass"}]},
 "checklist": [{"key": "repo_access|models|baseline_build|tests|deploy_target|secrets|knowledge",
                "label": "Repo access", "state": "done|now|todo|failed", "owner": "agent|you",
                "action": {"label": "Add", "href": "…"}, "detail": "…"}],
 "only_you": [<checklist items with owner=you and state != done>],
 "first_work": {"label": "Fix 6 failing tests", "source": "ci|ticket|bug|roadmap", "estimate_min": 15,
                "estimate_usd": 1.5, "start": {"href": "…"}, "others_href": "…"},
 "elapsed_s": 112}
```
The agent does every checklist item it can itself (verify repo access, baseline build on the default
branch, discover tests, bind/propose a deploy target on a verified host, declare needed secrets by name,
index the KG / build the dossier) and asks only for what it cannot (`owner: "you"`).

## 9. ⌘K jump — `GET /v1/jump?q=&project_id=`
`{"items": [{"label": "PR #3 Visual refresh", "kind": "pr|run|build|deploy|agent|project|doc", "href": "…", "sub": "…"}]}`

## 10. Navigation
- Left nav: `Projects` (expandable list + Add project) in the Run group; `Factory` → `Code Tasks`
  (label everywhere; old `area=` values keep working).
- Project page: `?project_id=<id>&area=project&tab=overview|work|roadmap|knowledge|deployments`
  with `&drawer=<kind>:<id>` for side drawers (deep-linkable, back button closes).
- First run: `?area=projects&first_run=1` when the install has no project with a repo.

## 11. Project settings move onto the Project page (operator, 2026-11-06)
> "even if you want to refactor some of the project settings move to the newer screen.. especially all
> those demo user password, it can be moved to the new screen"

- A **Settings** tab (6th tab, gear icon) on the Project page holds the project's own settings, moved from
  where they are scattered today: **demo logins** (user names + password reveal, owner/admin with step-up —
  today hidden in a build's Settings sheet and without user names), the app's **deploy values / secrets**
  (names only, set/rotate), **deploy targets & hosts** for this project, **captain** and squad, **autonomy
  level & ambient budget** for this project, repo bindings, notifications.
- The old places keep working but become a single link "Moved to Project › Settings" (parity: nothing is
  lost; the README parity table lists each setting → new home).
- Demo logins also appear read-only (user + reveal) on Overview › Try it and Deployments.
- API: `GET /v1/projects/{project_id}/settings` returns sections with the existing write endpoints as
  `actions` (no new write paths unless one is missing):
```json
{"sections": [{"key": "demo_logins|deploy_values|targets|captain|autonomy|repos|notifications",
  "label": "Demo logins", "icon": "key", "items": [..], "actions": [{"label": "Reveal", "href": "…", "requires": "step_up"}]}]}
```
- Demo users come from the app itself: a `demo_logins:` list in boss.deploy.yaml if present, else the
  app's seed file / login page hint (HelpDesk lists customer@ / agent@ / admin@demo.test); password = the
  generated DEMO_PASSWORD for that environment (preview / staging…), shown per environment.

## 12. Health tab — project-scoped observability (operator, 2026-11-06)
> "it makes sense to open the app through the project page and then go to the deployment tab to see that
> project deployment.. same thing for Observability too right?"

- 7th tab **Health** (`tab=health`, heartbeat icon). Rule: anything about ONE project lives on the Project
  page; anything shared (Infra servers/cloud/plugins, Observability across apps + BOSS's own platform,
  alert sources, contact points, Grafana) stays in its org-wide area. Cross-links both ways.
- Header: a health dot beside the live-URL chip on every tab (opens Health).
- `GET /v1/projects/{project_id}/health`:
```json
{"environments": [{"key": "staging", "label": "Staging", "target_id": "tgt_…", "health": "healthy", "uptime_pct": 99.9,
   "error_rate": 0.4, "p95_ms": 180, "series": {"errors": [..], "latency": [..]},
   "slo": {"target": 99.5, "burn": "ok|warning|breach"}, "href": "…"}],
 "alerts": [{"id": "…", "label": "5xx on /api/tickets", "severity": "page|ticket|info", "state": "firing|resolved",
             "since": "…", "href": "…"}],
 "incidents": [{"id": "wi_…", "label": "order-api 5xx 6.6%", "state": "running|needs_you|resolved",
                "graph_href": "?area=agent_ops&item=…", "mini": [{"label": "Alert", "state": "done"}, …]}],
 "monitoring_pack": {"rules": 6, "managed_in": "repo", "href": "…"},
 "links": {"grafana": "…", "logs": "…", "observability": "?area=observability&app=…"}}
```
Below the summary, the console renders Observability's own per-app alert-rules component for each
`target_id` (rules, Tune, Silence, Respond automatically, + New alert rule, sources, history) — same data,
same actions; on an engine without §12 it uses the project's apps from `GET /v1/observability/apps`.
Built from the existing app alerts / incident squad / monitoring pack / stack health checks; empty states
explain how monitoring gets set up (Beacon's pack on deploy).

## 13. Parity: no feature is killed (operator, 2026-11-06)
> "moving out is fine but we need to have same experience - dont kill these screens / features"

- Observability › app › **Alerts** (rules with query, Every/Window/For/Keep firing/Threshold/Min req/Severity/
  Routes to/Data, state chip, **Respond automatically**, **Tune**, **Silence**, **+ New alert rule**) and
  **Overview** stay as they are. Project › Health reuses the SAME component for its Alerts section (same
  data and every action), scoped to the project's environments; summary cards on top, full rules below.
- The same applies to every screen whose content now also appears on the Project page (Infra deploys,
  Code Tasks build pages, Workflows runs, Setup demo logins, Approvals): the project view reuses the real
  component or links to it; the original screen keeps every capability. The independent tester checks a
  parity list: each moved/duplicated capability works in both places.
## 14. As built (backend, `feat/px-api`) — additions the console can rely on
Every shape above is served as written; these are the extra fields and the decisions taken.
- **Stations.** `WorkItem.stages[]` is always the six keys `plan|design|build|review|pr|ship` with
  `label` `P|D|B|R|PR|S` and `name` `Plan|Decide|Build|Review|PR|Ship`; `state` is the shared step
  enum (`done|now|waiting|todo|failed|skipped`): `waiting` = a person's gate or merge, `skipped` = the
  workflow has no such step. WorkItem also carries `source` (`workflow_run|code_task|roadmap`),
  `title` (full), `status` (raw), `created_at`, `links.decision` and `decision_ids`. `GET …/work` adds
  `counts` (whole project, unfiltered) and `new_work`. `q` matches words or a PR number (`#3`).
- **New work.** `POST …/work` also takes `title`, `source_ref`, `start_anyway`, `accept`, `dedupe_key`
  (the start door's own fields, so the readiness sheet can re-send) and answers
  `{work, run_id, href, duplicate, readiness?, precheck?}`. Permission `workflow_run:start`.
- **Projects list.** `GET /v1/projects/summary` adds `status_label`, `onboarding`, `archived` per item
  and top-level `first_run` / `first_run_href` (`?area=projects&first_run=1` when no project has a repo).
- **Overview.** `project.health` (worst environment), `needs_you[]` also holds the onboarding agent's
  "only you" items (`kind: setup`, `id: onb:<key>`, `action.decision_id: null`); `onboarding {state,
  stages, href}`; `try_it.env` and `try_it.users_from` (`manifest|repository|none`).
- **Captain.** `GET/POST …/captain` add `role`, `chosen` (`chosen|default|none`), `candidates[]` and
  `ask {subject: {kind: "project", id}, role_key, href}`. `POST` needs `project:update` (owner/admin,
  step-up); an unknown or retired agent answers 422 `{"error": "not_an_agent"}`. The Ask BOSS dock now
  knows subject kind `project` (`turn_subject.SUBJECT_KINDS` and `apps/web/lib/dock-subject.ts`).
  The default captain is recorded when a project is created; Marshal (`chief_of_staff`) is never the
  default.
- **Onboarding.** `GET …/onboarding` → `state` may also be `not_started`; adds `run_id`, `trigger`
  (`created|repo_added|manual`), `backlog[]` (ranked, each with `score`, `source`, `kind`, `text`,
  `external_url`), `error`, `started_at`, `finished_at`, `start {href, method}`. Checklist labels:
  Repo access · Models · Code mapped (`knowledge`) · Tests · Baseline build · Deploy target · Secrets.
  `first_work.start = {href: "/v1/projects/{id}/work", method: "POST", body: {kind, text, title,
  source_ref}}`. `POST …/onboarding/start` needs `workflow_run:start` and is idempotent (a moving run is
  returned). It runs as the `project.onboarding` job (worker → `POST /v1/projects/onboarding/execute`,
  worker lease only); a pass that waits for the code graph re-queues itself in 20 s (≤ 15 min, then it
  carries on from GitHub's file list). Baseline build/tests are read from the default branch's CI;
  with no CI the item is `skipped` ("the first build runs the tests in its own sandbox") — BOSS never
  runs a repository's code outside a sandbox. A deploy target is *proposed* on a verified (shared) host
  (`owner: you`, creating one needs the person's step-up). Secrets and demo users are names only.
- **Deployments.** Environments add `target_id`, `app`, `status`, `uptime_pct`, `p95_ms`, `host_status`,
  `paused`, `manage_href`; `production.state` is `ready|none|needs_host`; plus `preview {demo_logins}`,
  `demo_logins` (the §11 listing) and `history[]`.
- **Demo logins.** `GET …/demo-logins` (project:read, names only):
  `{source, users[{user,label,password_key}], environments[{key,label,keys,logins[{user,label,env,
  password_key,reveal}]}]}`. Reveal: `GET /v1/projects/{id}/demo-logins/reveal?env=<key>` —
  `secret:manage` (owner/admin, step-up; 401 `step_up_required` otherwise), `Cache-Control: no-store`,
  audited as `project.demo_logins_revealed`; answers `{env, label, logins[{user,label,password_key,
  password}]}` or 404 `no_demo_login`. Never exposed to agents' MCP tools. `boss.deploy.yaml` accepts
  `demo_logins: [{user, label?, password?: ENV_KEY}]` (or plain user names). The old
  `GET /v1/apps/{app}/demo-logins` now also returns `users`, `users_from` and per-row `users`.
- **Settings.** Each section's `actions[]` is `{label, method, href, requires?, permission?, body?}`
  naming the existing write route; sections add their own fields (`captain`, `candidates`, `ambient`,
  `budget`, `apps`, `users_from`, `hint`).
- **Health.** Environments add `app`, `target_id`, `url`, `status`, `last_checked`, `last_error`,
  `deployed_commit`, `deployed_at`; the SLO target is 99.5 % availability until a rule sets one
  (`burn` from the last 24 h). `links.grafana` is a URL only when BOSS's check says Grafana answers
  (`grafana_state`, `grafana_reason` say why not). `empty {label, detail, href}` when nothing is
  deployed or no rule exists yet.
- **Jump.** Items add `project_id` (and `external_url` for PRs, `full_href` for runs); with no `q`
  the palette gets the newest items; a `project_id` the caller cannot read answers 404.
- **Integration (2026-11-06, feat/project-experience).** Fields the console reads beyond the above:
  Deployments environments add `host_id` and `host_href` (`?area=infra&host=<host id>`, the server's
  Infra drawer) and the response adds `ship_path` (5 nodes: PR merged → Image built → `<env> · auto|you
  approve` → Smoke test → Prod · you approve, states from the environments). Health environments add
  `host` and `host_href`. Projects list items add `cost_week` (the week's dollars, the Overview tile's
  number) and `series` (7 daily values). Settings actions add `open`: the console screen where a
  person makes that change today; the console links only `open`, never an API `href`. Console links
  name views that exist: Infra opens its + Add sheet from `add=host|target` (`add=target&host=<id>`
  preselects the machine), Observability's are `view=alerts|notify` and `inc=`, Govern's
  `?area=budget&tab=autonomy|limits`. Drawer links the engine writes and the console opens:
  `run:<id>` / `build:<id>` (the work drawer), `env:<target id>`, `rec:<id>`, `stage:<key>`,
  `onboarding`; `section:<key>` is Settings at that section, `dossier` / `doc:<path>` is Repo &
  knowledge, `captain` is the captain's Ask dock. The app's own demo-login door
  (`GET /v1/apps/{app}/demo-logins`) now has the reveal's bar: `secret:manage` (owner/admin, step-up),
  audited as `project.demo_logins_revealed`, not an MCP tool. Onboarding's "Accept backlog / Edit"
  (screen 10) stays "See the roadmap": the proposed backlog is candidates (GitHub issues, open PRs,
  failing CI, scan findings), not stored roadmap items, so `/v1/ambient/roadmap/{id}/accept` has
  nothing to accept, and accepting all would start a build each; items are accepted one by one on
  the Roadmap and What's next (`accept` names the roadmap route or THE start door with its body).
- **Area ids.** The console's ids are used as they exist: `knowledge` (not `knowledge_graph`),
  `workforce&role=` for an agent, `delivery_workflows&wfr=`, `code_tasks&view=apps&build=`,
  `decisions&decision=`, `infra&view=deploys&deploy=`, `agent_ops&item=`, `meetings&meeting=`.
  `area=project` / `area=projects` are the console's new ids (§10).

## 15. Models and GitHub accounts are global

Models and GitHub accounts are workspace settings (Setup › Models, Setup › GitHub, routing in Setup › Advanced). The project page never offers a model or account picker. The first-run checklist starts at "Add project"; above it a read-only "Workspace ready" row shows GitHub ✓ and Model ✓ with links to the global pages. If either is missing, "Only you can" shows a card linking to the global page. Onboarding's model check is read-only.

The Projects empty state reads the real status. A connected model or GitHub account shows as configured (✓, name, "Manage ›" to Repo and models), and its strip node ticks. A missing one's button opens the Repo and models page, never an inline picker. The first step not yet done is the primary action. If the status can't be read, the card shows a neutral "Check in Repo and models ›", never a false ✓.
