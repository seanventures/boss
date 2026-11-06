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

## Shared enums
- `stage`: `onboarding | ready | backlog | working | staging | production`
- `state` (step/node): `done | now | waiting | todo | failed | skipped`
- `work_state`: `planned | running | needs_you | done | failed`
- `health`: `healthy | degraded | down | unknown`

## 1. Projects list — `GET /v1/projects/summary`
```json
{"items": [{"id": "helpdesk-ai-demo", "name": "HelpDesk AI Demo", "initials": "HD",
  "repo": "paramg/helpdesk-ai-demo", "stage": "working",
  "journey": [{"key": "onboarding", "label": "Onboarded", "state": "done"}, …],
  "needs_you": 2, "running": 1, "health": "healthy", "live_url": "http://2.29.2.202:8004",
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

## 4. Repo & knowledge — `GET /v1/projects/{project_id}/knowledge`
```json
{"repos": [<repo as in §2>], "dossier": {"stack": ["Node", "React"], "commands": {"build": "…", "test": "…", "run": "…"},
 "hotspots": [{"path": "server/sla.mjs", "label": "SLA engine", "why": "…"}], "todo_clusters": [..],
 "owners": [..], "built_at": "…", "href": "…"},
 "graph": {"nodes": [..], "edges": [..], "href": "?area=knowledge_graph&project_id=…"},
 "docs": [{"label": "PRD: SLA timers", "kind": "prd|adr|readme|runbook", "href": "…"}],
 "decisions": [{"label": "Timer from created_at", "at": "…", "href": "…"}]}
```

## 5. Deployments — `GET /v1/projects/{project_id}/deployments`
```json
{"environments": [{"key": "staging", "label": "Staging", "url": "…", "health": "healthy",
  "host": "2.29.2.202", "last_deploy": {"commit": "f3f130c", "at": "…", "by": "…", "href": "…"},
  "demo_logins": [{"user": "agent@demo.test", "label": "Agent", "reveal": {"href": "…"}}],
  "policy": "auto|person", "href": "…"}], "production": {"state": "none|needs_host|ready", "action": {…}}}
```
Demo-login reveal: owner/admin with step-up (existing secrets reveal path), returns user + password.

## 6. Roadmap — existing `GET /v1/ambient/recommendations?project_id=…&status=open`
(+ `/{id}/accept {as}`, `/{id}/reject {reason, category}`, `/{id}/snooze {days}`). Must work with the
ambient preview off: listing/accept/reject are reads/writes on stored items; generation is the captain's.

## 7. Captain
- `GET /v1/projects/{project_id}/captain` → captain + squad (§2 shape) + `last_said`.
- `POST /v1/projects/{project_id}/captain {"agent_id": "…"}` (owner/admin) — change captain.
- Chat: the existing Ask BOSS dock with subject `{"kind": "project", "id": project_id}` — one agent
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
