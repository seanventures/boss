# QA Agent 1 — Mission Control UI panel walk

**Date:** 2026-08-13  
**Agent:** QA Agent 1 (Mission Control UI functional testing)  
**Method:** production-qa — walk real journeys, signal-driven, **no false reds**, no GA claim  
**Repo branch:** `ai/grok/community-ga`  
**Live:** https://boss.local.sean.network (web) · https://boss-api.local.sean.network (API)  
**Org / project:** `org_boss` / `boss-dogfood`  
**Edition:** `community` (`GET /v1/edition` → `edition_id=community`)  
**Images:** `boss-api` / `boss-web` / `boss-worker` **`3e6c906`** (shared tag)  
**Plane:** comment on umbrella **BOSS-337**; related **BOSS-338** (edition honesty), **BOSS-347** (Studio seed)  

**How this was walked**

1. Inventoried every `AreaId` in `apps/web/app/ConsoleShell.tsx` (28 areas).
2. Fetched the live org gate (`/` → OrgGate) then the full SSR console (`/?org=org_boss&area=overview`, 7.3 MB, all `data-area` sections in one document).
3. Probed corresponding live GET APIs with the deploy key (never printed). POSTs only for classify / catalog-smoke / seed shape — **no cloud provision, no spendy model jobs**.
4. Graded what an operator actually sees. Empty-and-honest is **not** a red. 4xx on paid-only routes is **expected**. 5xx is a bug.

**Grade key**

| Result | Meaning |
|---|---|
| **works** | Primary job completes; empty/error states honest |
| **4xx expected** | Fail-closed / edition / method / validation — not a product defect |
| **5xx bug** | Control plane threw; UI looks broken or degrades to “API returned 500” |
| **empty-but-honest** | API 200, no rows, copy says so — not a red |
| **incomplete** | UI without a working API loop, or API without a usable control |

**Special checks (asked)**

| Check | Live signal | Verdict |
|---|---|---|
| Community hides Account Connect / marketplace / clusters | `PaidSurfaceGate` cards (`data-paid-surface=account_connect\|marketplace\|enterprise_clusters`); CTAs replaced by honesty copy | **PASS** |
| SSO / identity still visible | Enterprise nav relabeled **SSO & Editions**; IdP packs section `data-community-included`; 10 IdP packs (Okta…Ping) | **PASS** |
| Studio shows 13 seeded agents | Catalog **13/13 active** + 3 panels; UI button **“Smoke test all 13 agents”** | **PASS** |
| Deadletter / decisions inbox load | `GET /v1/deadletters` 200 · 41 items; `GET /v1/decisions/inbox` 200 · 77 items (18 pending) | **PASS** (noisy, not broken) |
| Budget intensity / status | `budget_state=green`, `operational_intensity=25`, cap $129, actual $39.23, util 30.4%, 0 reservations | **PASS** |
| GitHub connections list | **2 active** connections (`dogfood-github-pat`, `easy-connect-proof-968bc47`) · **2 bindings** (`seanheiney/boss` primary, `paramg/boss-sandbox` source) | **PASS** |

**Do not claim GA.** Community dogfood console is **navigable and largely honest**. Nested infra/token/autoscaling panels still 500 on missing tables. Factory/DLQ loop is still the reliability hole.

---

## Environment notes (not defects)

- `/` without `?org=` is the **org picker** (BOSS · 38 tasks · 16 meetings + “Onboard a new organization”). This is the intended gate, not a missing console.
- `GET /ready` 404 — no readiness route mounted. `GET /health` 200.
- `GET /v1/commands` 404 / `GET /v1/metatron/query` 405 — intake is **POST** (`/v1/commands/propose`, `/v1/metatron/query`). Not a red.
- `POST /v1/agent-studio/catalog/seed` exists (GET 405). Catalog already seeded; no seed POST executed (would be idempotent but catalog is already 13/13).
- `POST /v1/agent-studio/catalog-smoke` 422 on empty body (`project_id` required in JSON) — validation, not a crash. **Not exercised** (would run 13 agent checks; may spend).
- Sandbox host **connected** (`http://10.100.9.1:8077`, Firecracker, capacity 8/8) with **`claims_frozen=true`** — honest freeze, 0 live sandboxes.

---

## Area table

Area | Control | API | Result | Severity | Notes
---|---|---|---|---|---
**Org gate** (`/` no `?org=`) | Org cards (BOSS / Enter →); Onboard organization form (name + submit) | `GET /v1/orgs` 200 · 1 org | **works** | — | Deep links require `?org=org_boss`. Picker is honest.
**foundations** | Design-system color/type/motion/status showcase; no forms | none (static) | **works** | — | Platform chrome only.
**components** | Library buttons: Assign Outcome, Primary, Secondary, Approve; composer/orbs/overlays | none (static) | **works** | — | No control-plane dependency.
**overview** (Command Center) | Outcome composer **Assign →**; KPI tiles (Outcomes in flight, Agents active, Awaiting you, Verified today); live coordination graph | `GET /v1/status` 200 (`open_tasks=30`, `pending_decisions=18`, `deadletters=41`, `budget_state=green`, `api_reachable=true`); command is POST `/v1/commands/propose` | **works** | — | Command submit not fired (creates work). Snapshot is live.
**agent** (BOSS Agent) | Chat composer **Send**; General-chat workspace switch; last-10-days thread list | Threads require `actor` (`GET /v1/agent/threads` 422 without it — **expected**). Chat is POST `/v1/engine/agent/chat` (not probed — spendy) | **works** | — | SSR panel present; chat path is client POST. Empty thread CTAs honest.
**dashboard** | KPI snapshot (budget, queue, incidents, canary/eval); no mutating controls | `GET /v1/dashboard` 200 | **works** | — | Cross-phase numbers match status/ops queue.
**decisions** (Approvals) | Decision cards: **Approve / Request changes / Reject / Delegate**; Approval Rules view | `GET /v1/decisions/inbox` 200 · **77** (pending **18**, expired 40, approved 13, rejected 6); `GET /v1/approval-rules` 200 | **works** | P2 | Inbox **loads**. Noisy: many pending are “Missing active reservation for OpenAI call” / “Model capacity exhausted” — product loop, not a UI 5xx.
**work** (Task Board) | Mission task board columns (Backlog 1+); task-run trace; What Changed / receipts | `GET /v1/tasks` 200; `GET /v1/jobs` 200; `GET /v1/receipts` 200; `GET /v1/events` 200 | **works** | — | Board populated. Residual hydration #418 historically on this area (BOSS-346) — not re-crawled in a browser this pass.
**portfolio** (Work Allocation) | Ranked work / quota / tracker / bot / channel forms (connect tracker, upsert quota, plan allocations, upsert bot, status channel); Release planning board | `GET /v1/work-orchestration/overview` 200; `GET /v1/release-planning/{status,board,releases}` 200 | **works** / **empty-but-honest** | P2 | Overview 200; jobs=0 in the orchestration slice. Release board present. No spendy plan-allocation POST.
**discovery** | **Run discovery** form (signal → dossier) | `GET /v1/discoveries` 200 (seeded runs exist) | **works** | P2 | List/create UI live. Deepen/promote not exercised (can enqueue research).
**meetings** | **New meeting**; Manage roles; completed meeting cards | `GET /v1/meetings` 200; `GET /v1/meeting-rooms` 200 (16 meetings in org meta) | **works** | P2 | Rooms/history load. Multi-agent PRD run not started.
**code_tasks** | **＋ New code task**; View BugBox; Build it again; run/preview cards | `GET /v1/code-tasks` 200; `GET /v1/previews` 200 (`open_url` absolute on public-base; 0 running, many expired) | **works** | P1 | List/UI works. Execution still flaky (see DLQ). New-task POST not fired (spendy). Preview View HTML unproven (0 running).
**delivery** (CI/CD) | Honest **No deployment targets** + **+ Onboard an app**; CICD vendor catalog | `GET /v1/delivery/health` 200 `overall=not_deployed` · 0 targets; `GET /v1/delivery/pipeline` 200; `GET /v1/cicd/{status,catalog}` 200 | **empty-but-honest** | — | `data-empty-state=no-targets`. Not a red.
**monitoring** | Combined **Not configured** card; Observability suite (catalog / recommend / health-check) | `GET /v1/observability-suite/{catalog,status,graph-summary}` 200; `GET /v1/observability` 404 (wrong path — UI uses suite + delivery health) | **empty-but-honest** | — | `data-empty-state=monitoring-unconfigured`. 404 on `/v1/observability` is **not** the UI path.
**shepherd** | Reasoning cards; sweep / answer-escalation actions | `GET /v1/shepherd/overview` 200 (class names + escalations present; reasoning/ledger empty arrays) | **works** / **empty-but-honest** | P2 | Overview loads. Quiet reasoning list is honest. Escalations exist.
**reliability** | On-call status; incident list; knowledge-graph **Seed from infra** | `GET /v1/oncall-sre/status` 200; `GET /v1/reliability/{incidents,learning,policy}` 200 (2 incidents); `GET /v1/knowledge/graph/{health,nodes}` 200; graph walk requires `service` (422 without — expected) | **works** | P2 | Incidents load. Remediate/verify POSTs not fired.
**reports** (Evidence) | Window tabs **Today / Last 7 days / Last 30 days**; throughput / spend / scorecards | `GET /v1/reports/summary` 200 (today 6 jobs all succeeded; week spend $0.27; month 191 jobs) | **works** | — | Evidence is live, not a shell.
**budget** | **Apply intensity** slider; spend/reserved/forecast/TTC; allocations empty copy; cost insights; FinOps card; token-efficiency panel | `GET /v1/budget/status` 200 green + intensity 25; forecast 200; allocations 200 **0 items**; insights 200; finops/summary 200; **token-efficiency/scorecard 200** but **policies + consumption 500** | **works** + **5xx bug** (token nested) | P1 | Core budget **works**. Intensity is on `/v1/budget/status` (there is no `/v1/budget/intensity` — 404 there is **not** the UI path). Token policies/consumption 500 (missing `token_budget_policies`).
**pipeline** (Coding Pipeline) | Preset profile cards (lean/balanced/strict/vag); custom-profile form **Enterprise-only honesty** | `GET /v1/pipeline-profiles` 200 · 4 presets, default `balanced`. `/v1/pipeline/profiles` 404 is **not** the UI path | **works** | — | Community correctly hides custom-profile create.
**comms** | **Create outbox draft**; channel/subject/body; **Render Slack message**; ACK card; channel policy form | `GET /v1/comms/{inbox,outbox,policies}` 200 | **empty-but-honest** / **incomplete** | P2 | APIs up. No real Slack/email loop (flags fail-closed). Draft form looks complete.
**ops** | Workflow health; **Run canaries**; registry; health snapshots; safety/evals; chaos; audit/replay; **Self-Improvement honesty card**; deadletter **Requeue job** | `GET /v1/ops/queue` 200; workflows 200 **0 defs**; health-snapshots 200; canaries 200 (1 item); chaos 200; improvement-programs 200 **0**; `GET /v1/self-improvement/proposals` **404**; deadletters 200 **41**; `GET /v1/audit/summary` 404; `GET /v1/break-glass` 404 | **works** (DLQ/canary) + **4xx expected** (SIM) + **incomplete** (audit/break-glass paths) | P2 | SIM 404 is **edition-expected**; UI shows Enterprise-only card. Nav **Paid** badge on Ops. Audit-summary 404 is a real path miss if the UI still fans it out (degrades, not 5xx).
**assurance** | Count cards (metrics/cases/surfaces/verifiers); security-scan card (Semgrep/Trivy/OSV/Gitleaks) | `GET /v1/assurance/{overview,cases,metrics,surfaces}` 200 · **0** cases/metrics; `GET /v1/security-scan/scanners` 200 (6); `GET /v1/security-scan/latest` 404 | **empty-but-honest** | P2 | Overview chrome honest zeros. Latest-scan 404 = none yet (empty, not crash). Not demoable as a proof program.
**org** (Agent Roster) | Roster of operators/roles; packs; local modules; **hosted marketplace gated**; break-glass / members forms | `GET /v1/roles` 200; packs 200; members 200; marketplace catalog **404** (gated in UI) | **works** + **4xx expected** | — | Marketplace CTAs hidden. Local modules remain.
**github** | Easy connect (env auth); Test access; Reconcile now; bind repo; snapshot/plan/approve/authorize/enqueue/repair; activity | `GET /v1/github/connections` 200 **2**; bindings 200 **2**; policies/leases/sync/conflicts/merges/recon 200; repository-activity 200; ref-states 200 | **works** | — | Connected. Dual-plane (settings token vs binding) honesty is a factory residual (BOSS-343), not a list-load fail.
**infra** (System Configuration) | ProviderOps suite; MCP servers; worker pools; FinOps; **elastic / Goldeneye / usage / autoscaling panels** | providers 200 (incl. planned Azure BYOK); provider-definitions 200; resources/changesets/receipts/secret-refs/worker-pools 200; multi-cloud/catalog 200; MCP servers 200; **elastic/status 500**; **goldeneye/status 500**; **provider-usage/summary 500**; **worker-autoscaling/{status,policies,history} 500** | **works** (kernel) + **5xx bug** (nested fabric) | **P1** | Community ProviderOps kernel is **up**. Nested fabric/usage/autoscaling throw `UndefinedTable` (see bugs). UI will show “API returned 500” — that **is** a false red vs unconfigured.
**capabilities** | Native-tool / skill catalog; MCP register / enable / delete forms | `GET /v1/capabilities` 200; skills 200; MCP servers 200 | **works** | — | Engine label present (OpenAI). Inventory is live.
**studio** (Agent Studio) | Metrics (published agents / panels / workflows / runs / A2A); library tabs Agents/Panels/Workflows; filter; **Smoke test all 13 agents**; create-workflow fields; A2A beacon | `GET /v1/agent-studio/catalog` 200 · **13 agents + 3 panels**; agents/panels 200; `GET /v1/a2a/health` 200 **degraded** (`REDIS_URL` unset); workflows in catalog (separate `/v1/agent-studio/workflows` 404 is **not** the catalog path) | **works** + **incomplete** (A2A) | P1 | **13 seeded specialists visible.** Smoke not run (would execute tests). A2A transport degraded — coordination empty, beacon honest-ish (“degraded”).
**sandboxes** | 0 live sandboxes copy; tree/exec/preserve/destroy (when a sandbox exists); break-glass reason | `GET /v1/sandboxes` 200 · host.connected=true · **0 sandboxes** · `claims_frozen=true`; helpers 422 (`SandboxTaskMissing` fail-closed) | **empty-but-honest** | P2 | Not “host refused” anymore. Helpers 422 without a task is fail-closed, not a 5xx.
**enterprise** (SSO & Editions) | **Identity provider packs** (Okta, Entra, Google, Auth0, Keycloak, GitHub, GitLab, OneLogin, …); Community edition card; **Account Connect gate**; **Enterprise clusters gate**; Cloud Easy Connect **Connect with device code / Connect in browser**; SCIM upsert still in tree behind cluster gate | `GET /v1/identity-providers` 200 · 10 packs; project-status 200 · 0 configured; `GET /v1/enterprise/identity-providers` 200 · 0; account-connect **404**; clusters **404**; **`GET /v1/cloud/status` 500**; cloud catalog/comparison/capabilities 200 | **works** (SSO) + **4xx expected** (paid) + **incomplete** (Cloud Easy Connect CTAs) + **5xx bug** (`/v1/cloud/status`) | P1 | SSO **kept**. Paid surfaces **gated**. Residual: Easy Connect buttons still submit (not behind `PaidSurfaceGate`). `/v1/cloud/status` 500 is schema drift (`cloud_worker_enrollments`).

---

## 5xx inventory (live, all `UndefinedTable`)

These are **not** one-line handler typos. The routes exist; **migrations were not applied** on goldeneye. Wrapping them to return empty-200 would hide drift. Schema apply is an ops action (not done this session).

| Path | Missing relation | UI area |
|---|---|---|
| `GET /v1/worker-autoscaling/status` | `worker_autoscaling_policies` | infra |
| `GET /v1/worker-autoscaling/policies` | `worker_autoscaling_policies` | infra |
| `GET /v1/worker-autoscaling/history` | `worker_autoscaling_history` | infra |
| `GET /v1/elastic/status` | `worker_instances` | infra |
| `GET /v1/goldeneye/status` | `goldeneye_nodes` | infra |
| `GET /v1/provider-usage/summary` | `provider_usage_events` | infra |
| `GET /v1/token-efficiency/policies` | `token_budget_policies` | budget |
| `GET /v1/token-efficiency/consumption` | `token_budget_policies` | budget |
| `GET /v1/cloud/status` | `cloud_worker_enrollments` | enterprise |

API logs: `psycopg.errors.UndefinedTable` on `boss-app-api-1`. Scorecard 200 proves the token-efficiency **router** is mounted; only policy/consumption tables are missing.

---

## Expected 4xx (do not file as reds)

| Path | Code | Why |
|---|---|---|
| `/v1/account-connect/status` | 404 | Community — paid surface; UI gated |
| `/v1/enterprise/clusters` | 404 | Community — UI gated |
| `/v1/marketplace/catalog` | 404 | Community — UI gated |
| `/v1/self-improvement/proposals` | 404 | Enterprise automation; honesty card shown |
| `/v1/agent-studio/seed` (wrong path) | 404 | Real seed is `POST /v1/agent-studio/catalog/seed` |
| `/v1/budget/intensity` | 404 | Intensity lives on `/v1/budget/status` |
| `/v1/commands` GET | 404 | POST `/v1/commands/propose` |
| `/v1/metatron/query` GET | 405 | POST-only |
| `/v1/factory/wip` POST | 405 | GET-only |
| `/v1/agent/threads` without `actor` | 422 | Contract |
| `/v1/sandboxes/helpers` with no task | 422 | Fail-closed `SandboxTaskMissing` |
| `/v1/reliability/graph` without `service` | 422 | Contract |
| `/ready` | 404 | Route not mounted; `/health` is the probe |

---

## Studio agent roster (live catalog)

All **active**, source `specpack`:

1. Backend Engineer  
2. Chief Architect  
3. Chief Financial Officer  
4. Chief Operator  
5. Database Administrator  
6. DevOps Engineer  
7. Frontend Engineer  
8. Full Stack Engineer  
9. Product Manager  
10. QA Engineer  
11. Security Engineer  
12. UI Designer  
13. UX Engineer  

Panels (3): Architecture & Risk Council · Product Delivery Council · Release Readiness Panel.

A2A: `status=degraded`, `REDIS_URL is not configured`, ledger/deliveries all zeros.

---

## Deadletters / decisions (load proof, not a UI defect)

- **Decisions inbox:** 77 rows · **18 pending**. Pending themes: missing OpenAI reservation; model TPM/credits/budget exhausted; plan-draft review from workflow-e2e-gate.
- **Deadletters:** 41 · kinds `job=39`, `resource_receipt=1`, `provider_sync=1`. Sample reasons: missing `BOSS_DIGITALOCEAN_TOKEN`; Claude Code exit 143; executable not allowlisted (`local_subprocess`); Groq/LiteLLM 400; `NoneType.get`. **Requeue** buttons render.

These prove the inboxes **work**. They also prove the factory/model loop is **not** sealed — out of UI-panel scope except as operator-visible noise.

---

## Top 10 bugs / incomplete features (ranked)

1. **P1 — Nested infra 500s (schema drift).** Elastic / Goldeneye / provider-usage / worker-autoscaling all 500 `UndefinedTable`. System Configuration **looks broken** in those panels while ProviderOps kernel is actually healthy. Fix: apply the missing tables on goldeneye (or fail-closed 200-empty **and** apply schema). Related Plane: BOSS-366 / BOSS-362.

2. **P1 — `/v1/cloud/status` 500 + Easy Connect CTAs still live.** Account Connect / clusters / marketplace are gated correctly, but **Connect with device code / Connect in browser** still submit on Community while the status route crashes (`cloud_worker_enrollments` missing). Operator can click a paid-looking loop that 500s. Related: BOSS-365, BOSS-338 residual.

3. **P1 — Token-efficiency policies/consumption 500.** Scorecard works; Budget area’s token policy/consumption cards degrade to 500. Same class as #1 (`token_budget_policies`).

4. **P1 — A2A transport degraded.** Studio is seeded (13/13) but coordination beacon is degraded (`REDIS_URL` unset). Library works; “watch agents work together” does not. Related: BOSS-347 residual (seed **done**; live fabric not).

5. **P1 — Code-task execution / DLQ noise.** Code Tasks UI and lists work; 41 deadletters + 18 pending capacity/reservation decisions mean “Build it again” is not a reliable dogfood path. Not a panel 5xx — it is the highest-severity **product** incompleteness behind the UI. Related: BOSS-351.

6. **P2 — Cloud Easy Connect vs Account Connect split honesty.** Community copy correctly hides hosted Account Connect, then still offers Cloud device/browser connect. Two stories on one page. Gate Easy Connect or label it as optional Cloud link that is **unconfigured**, not a working CTA.

7. **P2 — Assurance / workflows / allocations empty.** Honest zeros. Fine for fail-closed; not a demoable Assurance Center. Optional seed (living plan B5).

8. **P2 — Sandboxes honest-empty + helpers 422.** Host connected and claims frozen (correct). 0 sandboxes so the governed console cannot be walked. Helpers 422 without a task is fail-closed.

9. **P2 — Communications is a local draft shell.** Inbox/outbox/policies 200 and empty. Render-Slack + draft forms look finished; no live channel. Acceptable if claims freeze says “no external send.”

10. **P2 — Residual path/fan-out misses.** `GET /v1/audit/summary` 404, `GET /v1/break-glass` 404 — UI still has Audit & Replay / break-glass chrome that degrades. Not 5xx. Confirm the live web client’s exact paths or hide the cards.

**Not ranked as bugs:** Org picker, SIM 404, paid 404s, delivery 0-target honesty, monitoring unconfigured honesty, GitHub list (connected), budget status/intensity, 13 Studio agents, SSO IdP catalog.

---

## What works (operator-true)

- Org gate → `?org=org_boss` console; edition chip **Community**; nav **SSO & Editions**.
- Command Center, Agent chat chrome, Mission Dashboard, Task Board, Approvals inbox, Evidence, Budget core, Coding Pipeline presets, Capabilities, GitHub MC (2/2), ProviderOps kernel, Studio library (13+3), Shepherd overview, Reliability incidents, Discovery/Meetings lists, Code-task list, CI/CD + Monitoring **honest empty**, Sandboxes **honest empty + host connected**.
- Paid surfaces **Account Connect / marketplace / clusters** replaced by honesty cards.
- Self-hosted SSO/IdP **visible** with Community-included badge.

## What was not done (by design)

- No browser Playwright hydration crawl (BOSS-346 still open historically).
- No Approve/Reject, no command Assign, no New code task, no Studio smoke, no Cloud connect, no deploy onboard, no intensity Apply (would mutate).
- No schema apply / redeploy.
- No GA language.

---

## Evidence pointers

- Live HTML: `/?org=org_boss` (7.3 MB SSR, `data-edition=community`, `data-community=true`).
- Probe extracts on goldeneye: `/tmp/qa-ui-probe.jsonl`, `/tmp/qa-ui-probe2.jsonl` (keys never stored).
- API logs: `docker logs boss-app-api-1` `UndefinedTable` traces for the 9 paths above.
- Prior companion audits: `docs/operations/LIVE_UI_FUNCTIONAL_AUDIT_2026-08-11.md` (superseded on ProviderOps 404, Studio empty, GitHub 0-conn, sandbox refused — those four are **fixed** on `3e6c906`).
