# BOSS live UI functional audit — half-baked / broken inventory

**Date:** 2026-08-11/12  
**Plane:** BOSS-337  
**Deploy:** `boss-app` on goldeneye (`boss.local.sean.network` / `boss-api.local.sean.network`)  
**Edition:** `community` · images api/web `view2` · worker `norebuild`  
**Method:** live feed probe (100+ paths), functional action probes, Playwright crawl of 26 Mission Control areas, prior deadletter triage.

Companion: `docs/operations/LIVE_QA_2026-08-11.md`, `.ai/live-qa-memory.md`  
Artifacts (runner): `/tmp/boss-qa/artifacts/{deep-feed-probe,functional-actions,browser-console-crawl}.json`

---

## How to read this catalog

| Grade | Meaning |
|---|---|
| **DEAD** | UI exists; primary backend routes 404 / not mounted on this edition |
| **BROKEN** | Backend partially present but feature fails in production dogfood |
| **SHELL** | API 200 but empty / unconfigured; forms look real, nothing works end-to-end |
| **FLAKY** | Sometimes works; high failure rate in real runs |
| **OK** | Core happy path verified live |

Grades are **live dogfood truth**, not “exists in git”.

---

## Executive summary

Mission Control is a **wide console shell** over many product surfaces. On community dogfood:

- **Core control plane** (auth, tasks, events, budget, metatron, command intake, agent chat) is **OK**.
- **Large product areas are DEAD or SHELL**: ProviderOps/System Configuration, Self-Improvement, Enterprise/Account Connect, Sandbox host, GitHub Mission Control (unwired), CI/CD deploy targets, Observability stack, Agent Studio library, A2A.
- **Code Tasks / agent execution is FLAKY**: data exists but deadletters show max-turns, model TPM/credits, missing git in worker, code-task step 500s.
- **React hydration error #418** on multiple areas (SSR/client HTML mismatch).
- **Previews** often advertise `http://127.0.0.1:…` URLs — not usable from the operator browser.

The product *looks* complete; many panels are **UI-first without a working live loop**.

---

## P0 — looks finished, does not work

### 1. System Configuration / ProviderOps — **DEAD** (edition mismatch)

- **UI:** `?area=infra` + global SSR fan-out on every area.
- **Evidence:** permanent `apiReachable=false` — Provider catalog/accounts/Resources/ChangeSets/receipts/worker pools/scale plans all **HTTP 404**.
- **Root cause:** `BOSS_EDITION=community` omits enterprise routes (`/v1/providers`, `/v1/provider-definitions`, …) per `test_edition_routers.py`. Web still fetches them on every SSR.
- **Same class of bug** as delivery/edition split tests warn about: UI does not degrade — it looks broken.

**Fix:** edition-aware console (skip fan-out) and/or promote read-only ProviderOps to community for dogfood.

### 2. Self-Improvement (Ops panel) — **DEAD**

- **UI:** full proposal / canary / rollout / rollback forms (`phase2-ui`, actions in `actions.ts`).
- **Client:** `listSelfImprovementProposals` → `/v1/self-improvement/proposals`.
- **Live:** **404** list and create. OpenAPI has **no** `/v1/self-improvement/*` (only `/v1/improvement-programs/*` — different product).
- **Impact:** Ops “Self-Improvement” CTAs cannot work; actions 404.

### 3. Sandboxes execution plane — **BROKEN**

- **API:** `GET /v1/sandboxes` → `200` but  
  `host.connected: false`, `unavailable: "[Errno 111] Connection refused"`, `sandboxes: []`.
- Helpers same connection refused.
- **Impact:** Sandboxes area and any code-path that needs the sandbox agent host cannot inspect/exec live sandboxes.
- Config keys exist (`BOSS_SANDBOX_AGENT_URL`, …) but host is not reachable from api container.

### 4. Preview “View app” URLs — **BROKEN**

- `GET /v1/previews` returns **50** previews.
- Sample URLs: `http://127.0.0.1:41635` (process mode / local bind).
- **Impact:** Operator on tailnet/browser cannot open “View” targets; previews are host-local only. (Related recent commits on main mention view/preview fixes — live `view2` may predate them.)

---

## P1 — major surfaces half-baked

### 5. GitHub Mission Control — **SHELL**

- Routes exist: connections, repository-bindings, branch policies/leases/sync/merges.
- Live dogfood: **0 connections**, **0 bindings**, empty policies/leases/sync/merges.
- UI forms to create connection/binding present; project not wired to a real GitHub App connection in MC (despite `github_configured: true` for software-factory token path).
- Repo activity read may work via token; **governed GitHub MC workflow is empty**.

### 6. CI/CD Delivery / Deploy — **SHELL**

- Pipeline/health endpoints **200**.
- `delivery/health`: **overall `not_deployed`**, **0 targets**.
- `deployment-targets`, `deployment-runs`, `deployment-promotions`, `deployment-policies`: **empty**.
- Deploy flags: `BOSS_ENABLE_SANDBOX_DEPLOY=false` (correct fail-closed) but UI still presents full deploy machinery.
- **Observability stack:** `deployed: false`, no Grafana/Prometheus.

### 7. Agent Studio — **SHELL / degraded**

- Catalog **200** but `agents=[]`, `panels=[]`, `workflows=[]`, runs empty.
- A2A health: **degraded** (transport degraded).
- Browser signal: **“not configured”**.
- Catalog smoke: 200 with 0 items — smoke does not prove a working studio.

### 8. Code Tasks / coding agent loop — **FLAKY**

- List works; many runs **blocked** (e.g. `awaiting_plan_approval`).
- Jobs sample: **61 succeeded / 39 deadlettered** (almost all `code.task`).
- Deadletter buckets (prior triage): max_turns, Groq TPM, OpenRouter/Claude credits, timeouts, exec allowlist, missing **git** in worker image, `code-tasks/step` 500, NoneType, sandbox 404.
- Worker image **`norebuild`** skew vs api/web `view2`; worker lacks `git`.

### 9. Enterprise / Account Connect / Commercial paid surfaces — **DEAD on community**

- `/v1/edition?project_id=…` → community (OK).
- `/v1/account-connect/*`, `/v1/enterprise/clusters`, enterprise identity/SCIM: **404**.
- UI still renders Enterprise area and commercial trust controls.

### 10. React hydration mismatch — **BROKEN**

Playwright page errors (React **#418** HTML mismatch) on:

`work`, `code_tasks`, `shepherd`, `reports`, `pipeline`, `org`

SSR HTML does not match client render — real interactivity risk (hydration rewrite / broken event binding).

---

## P2 — empty shells / incomplete UX (API up, product loop missing)

| Surface | Live signal | Notes |
|---|---|---|
| Workflows registry | 0 definitions, 0 runs | Ops workflow UI empty |
| Canaries | list empty; `POST /v1/canaries/run` works | Can run once; no standing canary program |
| Chaos | list empty; run needs valid scenario | Valid: deadletter_storm, lease_reclaim, worker_crash |
| Comms inbox/outbox | empty; slack render works | No real channel policies / external send (flags false) |
| Break-glass grants | empty | |
| Health snapshots | empty | |
| KPIs | empty | |
| Assurance metrics/cases/surfaces | empty lists (overview shell OK) | Assurance Center mostly empty proof data |
| Improvement programs | empty | Replaces? self-improvement but UI still old SIM |
| Integration registry | empty definitions/connections | |
| FinOps commitments | empty until reconcile | Reconcile POST works |
| A2A channels | empty; health degraded | |
| Agent groups | empty | |
| Model gateway leases/invocations | empty (usage has 4 rows) | |
| Software factory rollouts | empty | |
| Budget allocations | empty | Intensity slider needs integer intensity (float 422) |
| Meetings | 16 rooms/meetings exist | Needs deeper run/tally E2E (not fully exercised) |
| Discovery | 8 runs | Needs deepen/promote E2E |
| Approvals inbox | 54 decisions (mostly expired) | Noisy history; few live pending |

---

## What actually works (verified live)

| Capability | Evidence |
|---|---|
| Auth + project scope | 401 unauth; dogfood project present |
| Health | DB + GitHub token + OpenAI/llm-proxy configured |
| Metatron status Q&A | Budget answer with $ available/reserved/actual |
| CEO command intake | `POST /v1/commands/propose` → task+job+plan accepted |
| Agent chat | `POST /v1/agent/chat` and `/v1/engine/agent/chat` 200 |
| Budget quote/status/forecast | Green; ~$89.77 available (post actual) |
| Tasks / events / receipts / artifacts / jobs | Lists populate |
| Deadletters visible | 39 open (visibility OK; backlog bad) |
| Packs / roles / skills / MCP / modules | Non-empty inventories |
| Pipeline profiles | 4 profiles |
| Delivery defaults + repos list | Present |
| Change journal | 25 items |
| Reliability incidents list | 2 incidents |
| Anomaly scan / evals run / canary run / finops reconcile | Action POSTs succeed with valid bodies |
| Comms Slack **render** (local text) | Works; real send disabled |
| All 26 areas HTTP 200 | Shell navigable |

---

## Area-by-area scorecard (Mission Control)

| Area | Grade | Headline |
|---|---|---|
| Command Center (overview) | OK / heavy | Loads; 6.6MB SSR; ProviderOps 404 noise in payload |
| BOSS Agent | OK | Chat works; some thread content references dead preview URLs |
| Mission Dashboard | OK | Dashboard JSON healthy |
| Approvals | SHELL/noisy | Inbox mixes expired/approved/rejected |
| Task Board | FLAKY | React #418; tasks exist |
| Work Allocation | SHELL | Overview OK; limited live allocation action proof |
| Discovery | PARTIAL | List OK; deepen/promote not fully proven |
| Meetings | PARTIAL | Rooms/meetings exist; multi-agent PRD run not fully proven |
| Code Tasks | FLAKY | List + blocked runs; high DLQ; hydration error |
| CI/CD | SHELL | No targets; not_deployed |
| Monitoring | SHELL | Observability not deployed |
| Task Shepherd | PARTIAL | Overview OK; hydration error |
| Reliability | PARTIAL | Incidents exist; remediate path not proven |
| Evidence / Reports | PARTIAL | Summary OK; hydration error |
| Budget | OK | Status/quote OK; intensity API picky (integer) |
| Coding Pipeline | PARTIAL | Profiles OK; hydration error |
| Communications | SHELL | Empty inbox/outbox; render-only |
| Operations | DEAD pieces | Self-improvement 404; deadletters OK; workflows empty |
| Assurance | SHELL | Overview shell; empty proof sets |
| Agent Roster / Org | PARTIAL | Roles/modules OK; hydration error |
| GitHub | SHELL | MC unwired (0 connections/bindings) |
| System Configuration | **DEAD** | ProviderOps edition mismatch |
| Capabilities | OK | Skills/MCP/tools visible |
| Agent Studio | SHELL | Empty library; A2A degraded |
| Sandboxes | **BROKEN** | Host connection refused |
| Enterprise | **DEAD** | Routes not on community |
| Foundations / Components | OK | Design system only |

---

## Functional action matrix (sample)

| Action | Result |
|---|---|
| Metatron question | **OK** — real budget narrative |
| Command propose | **OK** — accepted task/job/plan |
| Agent chat | **OK** |
| Canary run | **OK** |
| Evals run | **OK** |
| Anomalies scan | **OK** |
| FinOps reconcile | **OK** (201) |
| Provider health poll | **OK** (empty snapshots) |
| Self-improvement create/list | **404 DEAD** |
| Account connect health | **404 DEAD** |
| Enterprise clusters | **404 DEAD** |
| Budget set intensity (0.5 float) | **422** — wants integer |
| Chaos run `noop` | **422** — unknown scenario |
| Sandbox list | **200 but host down** |

---

## Ranked bake-out backlog (what to finish first)

1. **Edition-aware Mission Control** — stop mounting dead enterprise feeds; honest empty states.  
2. **Self-Improvement vs Improvement Programs** — either mount SIM routes or rewire UI to improvement-programs.  
3. **Sandbox agent host** — fix connection refused; make Sandboxes truthful.  
4. **Preview public/tailnet URLs** — never hand out 127.0.0.1 to operators.  
5. **Worker image parity + git + allowlists** — cut code-task DLQ classes.  
6. **GitHub MC wiring** for dogfood (connection + binding) or hide until configured.  
7. **React #418 hydration** on work/code_tasks/shepherd/reports/pipeline/org.  
8. **CI/CD empty-state UX** when zero targets / deploy disabled.  
9. **Agent Studio seed or setup wizard** — empty library looks broken.  
10. **Durable `scripts/qa/live`** covering this catalog so regressions cannot ship dark.

---

## Explicitly not fully exercised yet

- Full multi-agent meeting → PRD → approval → code task → green PR → merge → deploy  
- Real Slack/email send (correctly fail-closed)  
- Provider ChangeSet writes  
- Reliability remediate/apply  
- Discovery deepen/promote end-to-end  
- Budget intensity UI control with integer mapping  
- Browser form submit for every CTA (only API-shaped probes + visual crawl)

Those remain the next depth layer after the P0/P1 bake-outs above.
