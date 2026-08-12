# Panel 01 — PRD / Product Completeness (GA readiness)

**Date:** 2026-08-12  
**Panel role:** PRD / Product Completeness Expert  
**Branch baseline:** `main` (latest at assessment)  
**Deploy truth:** goldeneye community dogfood (`boss-app`, images api/web `view2`, worker `norebuild` as of LIVE_QA 2026-08-11)  
**Sources:** `docs/product/PRD-v2.0.md`, `docs/product/TECHNICAL_SPEC-v2.0.md`, `docs/product/BOSS_GAP_ANALYSIS.md`, `docs/operations/PRD_CRITICAL_GAPS_2026-08-12.md`, `docs/operations/E2E_COMPLETION_PLAN.md`, `docs/operations/LIVE_QA_2026-08-11.md`, `docs/operations/LIVE_UI_FUNCTIONAL_AUDIT_2026-08-11.md`, `.ai/requirements-traceability.md`, `.ai/gap-backlog.md`, `.ai/release-readiness.md`, `.ai/status.md`, edition router tests (`apps/api/tests/test_edition_routers.py`)

**Rating key**

| Rating | Meaning |
| --- | --- |
| **Complete** | Vertical implemented, tested, and demonstrable for GA intent (local + dogfood or sealed proof) |
| **Partial** | Material implementation exists; glue, reliability, wiring, or edition/deploy gaps prevent sealed demo |
| **Shell** | API and/or UI present; empty, unconfigured, or non-executable on dogfood (looks finished) |
| **Missing** | Required PRD behavior not present in product path |
| **Blocked (external)** | Correctly fail-closed; needs owner creds, IdP, cluster, approval, or process gate |

---

## Executive verdict

BOSS is **not GA-ready as an autonomous company OS on community dogfood**. The durable control-plane kernel (auth, tasks, plans, decisions, budget quote/reserve/release, leases, receipts, events, Metatron, agent chat) is largely **Complete** and live-proven. What fails GA is the **closed product loop**: CEO command → multi-phase execution → reliable sandbox code work → PR decision → operator-reachable previews → optional deploy, with honest edition UX and one automated live gate. Mission Control still presents many finished surfaces that are **Shell**, **Partial**, or **DEAD on live community** (ProviderOps fan-out historically, Self-Improvement enterprise path, empty GitHub MC / Studio / Assurance / CI targets, broken sandbox host and loopback previews). Code on `main` has advanced Community BYOK ProviderOps and orchestrator `next_move` capability beyond some live-image truth—**deploy skew and substrate reliability** are first-class product gaps, not “missing APIs.” Enterprise-only items (Account Connect, live IdP/SCIM, production ProviderOps fleet writes, real Slack/email, GitHub branch-protection process) are correctly **Blocked (external)** and must not be claimed as Community GA. **Verdict: Community dogfood GA = NO; package/source readiness ≠ product closed-loop readiness.**

---

## Capability matrix

| # | Capability area | Rating | Evidence paths | Notes |
| --- | --- | --- | --- | --- |
| 1 | **CEO command → task → plan → budget → job → decision → receipt** | **Partial** | Live: `LIVE_QA_2026-08-11.md`, `PRD_CRITICAL_GAPS` §B/D; code: intake, budget, jobs, decisions; SpecPack `packages/specpack/workflows/ceo_command_to_pr.yaml`; worker `agent.orchestrator.next_move` in `apps/worker/boss_worker/runner.py`; OpenAPI kernel routes strong | Command propose → task/job/plan **GREEN** live. Budget quote/reserve/release **GREEN** (consume fail-closed). Decisions inbox API **GREEN** (UX noisy/expired). Receipts/events/DLQ **GREEN**. **Gaps:** sealed multi-phase graph not dogfood-proven; community `NoopBudgetPort` vs Postgres enterprise spend governance (`test_edition_routers.py`); no automated live vertical-slice gate (E2E W3.2 / BOSS-351). |
| 2 | **Plan dispatch & orchestration glue** | **Partial** | `PRD_CRITICAL_GAPS` §A.2; `E2E_COMPLETION_PLAN` W3.1; worker capabilities + `run_orchestrator_next_move`; event subscriptions docs | `dispatch_plan` and reactive `plan.dispatched` path exist; worker now lists `agent.orchestrator.next_move`. Critical-gap doc still flags fragmented multi-phase execution vs explicit code-task/SWF routes. `ceo_command_to_pr` remains SpecPack definition more than sealed WorkflowRun product proof. |
| 3 | **Leased workers / job ledger** | **Complete** (primitive) / **Partial** (substrate) | Jobs/leases tests; LIVE_QA worker healthy; worker image skew + DLQ in LIVE_QA | Lease/TTL/heartbeat/retry/deadletter invariants solid in code+tests. Dogfood: worker `norebuild` vs api `view2`, missing `git`, 39 open deadletters mostly `code.task`. |
| 4 | **Budget Governor (quote/reserve/consume/release/forecast)** | **Partial** | Trace BUD-001..006 Implemented; LIVE budget PASS; `NoopBudgetPort` community composition | API contract and fail-closed consume proven live. Community edition still uses **NoopBudgetPort** for governance composition—spend enforcement on dogfood needs explicit verification before GA claim. Intensity slider / hierarchical paths exist in older gap phases. |
| 5 | **Software Factory (spec → evidence → PR decision)** | **Partial** | `software_factory` services; BOSS_GAP re-audit real PRs; LIVE code-tasks **FLAKY**; PRD_CRITICAL_GAPS §A.1 | Historical real gated PRs and quality gates in code. Live: high DLQ (max turns, TPM, credits, step 500, no git). Not a reliable CEO dogfood narrative. |
| 6 | **GitHub project factory / GHX plane** | **Shell** (dogfood wiring) / **Partial** (code) | Trace BOSS-247–255; LIVE_UI GitHub MC 0 connections/bindings; `PRD_CRITICAL_GAPS` §A.6 | Rich connection/binding/lease/sync/conflict/merge/reconcile APIs and MC UI. Dogfood: **0 connections, 0 bindings** → Mission Control GitHub is a shell; factory may use sandbox token path outside MC. |
| 7 | **ProviderOps BYOK (Community)** | **Partial** | PRD Community includes `basic_providerops_readonly`; `test_edition_routers.py` now community-classifies providers/resources/changesets; LIVE_QA (view2) 404 ProviderOps; E2E W0 BOSS-350 keys | **Code intent on main:** Community BYOK + self-managed infra (catalog, accounts, inventory, ChangeSets, pools). **Live dogfood (2026-08-11):** routes absent / SSR `apiReachable=false`—deploy lag vs main. Product decision: ship keys + redeploy + honesty UI before GA. Production actuation still **Blocked (external)** without creds/flags/approval. |
| 8 | **Resource Plane kernel invariants** | **Partial** | TECH Resource Plane; RPL trace Implemented local; GAP-004 live writes blocked | ChangeSet + reservation + ResourceReceipt design and local tests. Not demonstrable end-to-end on community dogfood inventory/actuate loop. |
| 9 | **Sandboxes** | **Partial** (code) / **Broken live** | `sandbox_*` services; LIVE_UI sandboxes host `connection refused`; E2E W2.2 BOSS-340 | APIs return 200 with empty list + host disconnected. Blocks execution-plane inspection and reliable code work. |
| 10 | **Operator previews** | **Partial** (code) / **Broken live** | Previews APIs; LIVE 50 previews many `127.0.0.1`; E2E W2.3 BOSS-341 | “View app” not operator-reachable on tailnet. Core feedback loop broken. |
| 11 | **Delivery / CI/CD / deploy** | **Shell** | LIVE delivery health `not_deployed`, 0 targets; community deployment routes in edition tests; E2E W1.4/W4.1 BOSS-344 | APIs community-mounted; dogfood empty. Production deploy flags correctly fail-closed. UI still full machinery without honest empty-state GA bar. |
| 12 | **Observability stack (Prometheus/Grafana)** | **Shell** | LIVE observability `deployed: false`; E2E W4.2 | Monitoring area not a live product loop. |
| 13 | **Agent Studio (definitions, panels, wizard)** | **Shell** (dogfood) / **Partial** (code) | Trace AST/BOSS-256–268, BOSS-268 wizard; LIVE agents/panels/workflows empty, A2A degraded; E2E W4.3 BOSS-347 | Runtime + commissioning path implemented in repo; dogfood seed/first-run missing → “not configured.” |
| 14 | **Workflow registry & runtime** | **Partial** | WFL-001 BOSS-134–143 Done in Plane/trace; SpecPack + API; dogfood orchestration not sealed | Durable workflows Implemented for registry/runs; product dogfood does not yet treat `ceo_command_to_pr` as sealed GA proof. |
| 15 | **Skills / Integration registry** | **Partial** | SKL/INT traces; LIVE packs/roles/skills/MCP inventories PASS | Catalog and gauntlet live for lists; real breadth of worker-driven skill invocation and healthy integrations partial. |
| 16 | **Model Entitlement Broker / capacity routing** | **Partial** | MEB traces; BOSS-335 gateway Implemented pending merge notes in status; LIVE LLM via proxy configured | Local/router/budget-pressure exist; live multi-lane health/fallback and community key UX incomplete (W0). |
| 17 | **Assurance / ground-truth** | **Shell** (dogfood) / **Partial** (code) | AST-001, BOSS-175–214 contracts/workflows; LIVE metrics/cases empty; E2E W4.5 | APIs exist; no seeded proof case for dogfood “proof gates” demo. |
| 18 | **Earned autonomy + value-based scheduling** | **Partial** / open principle | `BOSS_GAP_ANALYSIS` G1–G3; autonomy_engine upgrade/downgrade exists; E2E W4.6 BOSS-348 | Auto upgrade/downgrade + trust score **partial**. **Missing:** value-per-dollar/SLA claim order under budget pressure (G1); trust→risk-tier gate (G2); full weighted signals (G3). |
| 19 | **Self-improvement / improvement programs** | **Partial** + packaging split | SIM enterprise-gated (404 community); `/v1/improvement-programs/*` community (BOSS-334); LIVE SIM dead, programs empty; E2E W1.2/W4.4 BOSS-339 | PRD moat path present as enterprise SIM + community trial ledger. Ops UI still exposes dead SIM on community → packaging honesty gap. |
| 20 | **Comms (outbox/inbox ACK)** | **Partial** | COM traces Implemented policy path; LIVE empty; real Slack/email fail-closed | Constitution §5 not dogfood-visible without dry-run demo or hide. |
| 21 | **Mission Control honesty / Community edition trust** | **Partial** | PRD §Community “not crippled demo”; LIVE_UI DEAD/SHELL panels; hydration #418; SSR 6.6MB | Core OK surfaces exist; many areas look complete while dead/empty/flaky. Edition mismatch was class-P0 on live. |
| 22 | **Enterprise identity (SSO/SCIM/SAML)** | **Blocked (external)** | GAP-003; release-blockers; ENT partial local fail-closed | Surfaces fail-closed without IdP. **Not Community GA requirement.** |
| 23 | **BOSS Account Connect / Cloud billing / marketplace** | **Blocked (external)** | GAP-002; COMPKG partial local | Correct Cloud/enterprise SKU. **Exclude from Community GA DoD.** |
| 24 | **Live production ProviderOps fleet / cluster proof** | **Blocked (external)** | GAP-004; `BOSS_INFRA_REAL_PROVISION_ALLOWED` | Local/sandbox design complete; live production writes require approval+creds. |
| 25 | **GA process (branch protection, Actions CI)** | **Blocked (external)** | `.ai/status.md` GitHub 403 protection; Actions disabled; release-readiness | Process gate, not product feature. Do not publish GA until owner enables. |
| 26 | **OpenAPI / contract parity for dogfood paths** | **Partial** | PRD_CRITICAL_GAPS §E; code-tasks/delivery/sandboxes runtime-ahead | Kernel OpenAPI strong; agent/customer contract debt on code-tasks lifecycle, delivery, sandboxes. |

---

## Vertical slice grades (composite)

| Step | Grade | Product rating |
| --- | --- | --- |
| CEO command | GREEN | Complete (step) |
| Plan + budget | GREEN / governance caveat | Partial overall |
| Task graph auto-execution | YELLOW | Partial |
| Leased workers | GREEN primitive / flaky substrate | Partial |
| Sandbox code + artifacts/tests | YELLOW–RED live | Partial |
| Decisions | GREEN API / UX YELLOW | Partial |
| Receipts / audit / DLQ | GREEN (backlog dirty) | Complete (visibility) |
| PR / GitHub MC | YELLOW–RED wiring | Shell–Partial |
| Previews | RED live | Partial/Broken |
| ChangeSet / deploy | RED community dogfood | Shell + Blocked prod |
| Learn / SIM | RED community SIM | Partial (programs only) |

**Composite: YELLOW — not GA.** Kernel GREEN; orchestration/dogfood execution YELLOW–RED; ProviderOps/SIM packaging historically RED on live.

---

## Top 10 GA blockers (ranked)

| Rank | Priority | Blocker | Why it blocks GA | Primary refs |
| ---: | --- | --- | --- | --- |
| 1 | **P0** | Sealed CEO→PR vertical slice not reliable on dogfood | Primary product thesis unproven under live conditions | PRD_CRITICAL_GAPS §A.1; E2E W3.2 BOSS-351 |
| 2 | **P0** | Execution substrate: worker image skew, missing `git`, DLQ classes, model credit/TPM death | Code work fails visibly but at volume that kills dogfood trust | LIVE_QA P1; E2E W2.1/W2.4 BOSS-342 |
| 3 | **P0** | Sandbox agent host down | No live sandboxes; execution plane inspect/exec broken | LIVE_UI sandboxes; E2E W2.2 BOSS-340 |
| 4 | **P0** | Preview URLs loopback-only | Operator cannot “view the app” | LIVE_UI previews; E2E W2.3 BOSS-341 |
| 5 | **P0** | Mission Control edition honesty (ProviderOps/SIM/Enterprise 404 fan-out; dead panels) | Community looks broken/crippled vs PRD Constitution §14–16 | LIVE_QA P0; PRD Community; E2E W1 BOSS-338/339 |
| 6 | **P0** | Community ProviderOps/BYOK keys path not proven live post-main | Catalog/accounts may be code-ready; dogfood must show BYOK after deploy (W0) | E2E W0 BOSS-350; edition tests vs LIVE_QA skew |
| 7 | **P0** | Orchestration glue not sealed as product path | Command creates pieces; automatic multi-phase graph not GA-proven | E2E W3.1 BOSS-345; SpecPack `ceo_command_to_pr` |
| 8 | **P1** | GitHub Mission Control unwired (0 connection/binding) | Governed factory/merge plane undemoable in MC | LIVE_UI §5; E2E W3.3 BOSS-343 |
| 9 | **P1** | No automated live dogfood gate in release path | Regressions not release-blocking | E2E W5 BOSS-351/live_dogfood_gate; Actions disabled |
| 10 | **P1** | Decision inbox UX + hydration #418 + CI/CD empty honesty | CEO cockpit noisy; interactivity risk; deploy shell pretends full | LIVE_UI; E2E W1.4–W1.6, W1.5 BOSS-346 |

**Additional P2 (do not inflate Top 10 but track):** Agent Studio seed (BOSS-347), Assurance seed, earned autonomy G1–G3 (BOSS-348), OpenAPI parity (BOSS-349), comms dry-run honesty, community NoopBudgetPort verification, GA GitHub protection process (**Blocked external**).

---

## Enterprise-only exclusions (do **not** block Community GA)

| Exclusion | Status | GA treatment |
| --- | --- | --- |
| Hosted Account Connect, billing portal, team invites | Blocked (external) | Cloud SKU only; fail-closed OK |
| Live IdP SSO/SAML + SCIM runtime | Blocked (external) | Enterprise; local surfaces OK |
| Managed marketplace / hosted catalog | Blocked (external) | Cloud |
| Production ProviderOps fleet / real infra writes | Blocked (external) + approval | Not Community DoD; sandbox/read path is |
| Full enterprise SIM automation UI | Paid / enterprise-gated | Community uses improvement-programs **or** hide SIM |
| Real Slack / email / Mattermost send | External + flags | Dry-run or hide until configured |
| Multi-worker HA clustering productization | Enterprise monetization | Community single/few workers OK |
| GitHub private-repo branch protection admin | Process external | Owner action before GA **label**, not product code |

---

## Definition of Done gaps vs `E2E_COMPLETION_PLAN.md`

Community founder DoD from E2E plan (success criteria §):

| DoD criterion | Status vs plan | Gap |
| --- | --- | --- |
| 1. Attach third-party model + infra keys in System Configuration | **Open** (W0) | Keys PR/deploy smoke not closed with live evidence; historical live ProviderOps dead |
| 2. CEO command → durable task + plan + budget | **Mostly met** | Live PASS; residual NoopBudgetPort governance check |
| 3. Code work in sandbox → tests → PR + receipts (reliable) | **Open** (W2+W3) | FLAKY DLQ; substrate; no sealed E2E green |
| 4. Open previews from MC (reachable URLs) | **Open** (W2.3) | Loopback URLs |
| 5. Honest UI for unpaid/unconfigured | **Open** (W1) | Permanent banners, dead SIM/Enterprise, empty CI pretending full |
| 6. GitHub MC real connection+binding **or** clear empty-state | **Open** (W3.3) | 0 wired; empty-state honesty incomplete |
| 7. Minimal deploy path **or** honest not-configured CI/CD | **Open** (W1.4/W4.1) | `not_deployed` + full UI |
| 8. One automated live gate on vertical slice | **Open** (W5.1–2) | Gate script planned; not release-enforced |

**Optional later SKUs (correctly out of Community DoD):** Account Connect, IdP/SCIM, marketplace, full production ChangeSet fleet, GA branch protection.

**Workstream readiness (plan order):**

| Stream | Status | Completeness note |
| --- | --- | --- |
| W0 Community keys | In flight / not proven live | Blocks honest System Configuration |
| W1 UI honesty | Not Done | Blocks Community trust / PRD open-core claim |
| W2 Runtime substrate | Not Done | Blocks factory reliability |
| W3 Vertical slice sealed | Not Done | Blocks product thesis |
| W4 Product loops (GH/deploy/SIM/Studio/Assurance) | Not Done | P1/P2 after W3 |
| W5 QA gates + Plane Done-with-evidence | Not Done | Blocks “GA” labeling |

**DoD gap summary:** **0 of 8** Community success criteria are fully closed with current live evidence; criterion 2 is closest. Source/package release readiness (v0.11 notes in `.ai/release-readiness.md`) **does not** satisfy E2E dogfood DoD.

---

## What is already solid (do not rebuild)

- Project-scoped auth, orgs/projects, Mission Control shell reachability  
- Command propose → tasks/plans/jobs events  
- Budget quote/reserve/release + fail-closed consume validation  
- Decision inbox API, receipts, events, deadletter visibility  
- Metatron status Q&A, agent chat  
- Packs / roles / skills / MCP inventory listing  
- Kernel tests for leases, dedupe, policy denial visibility  
- Local enterprise/commercial fail-closed posture for blocked externals  
- (Codebase) GHX, workflow runtime, Agent Studio, assurance contracts, work orchestration — **implementation depth high; dogfood population/wiring low**

---

## Panel recommendation

1. Treat **Community closed-loop dogfood** as the GA bar, not OpenAPI surface count or package version tags.  
2. Execute E2E plan **W0 → W1 ∥ W2 → W3 → W5**, then W4 polish.  
3. Re-run live QA after community keys + worker + sandbox + preview deploy; refresh this matrix from new evidence.  
4. Do not declare GA while Top 10 P0 items remain open or while GitHub protection/Actions process is owner-blocked—call that **release process blocked**, separate from product completeness.  
5. Publish edition packaging decision explicitly: Community BYOK ProviderOps **on** + SIM **hidden or rewired to improvement-programs**.

---

## Evidence index

| Document | Role |
| --- | --- |
| `/Volumes/DataSSD1/test/boss/docs/product/PRD-v2.0.md` | Governing product intent, Community inclusions, Constitution |
| `/Volumes/DataSSD1/test/boss/docs/product/TECHNICAL_SPEC-v2.0.md` | Kernel/planes must-haves |
| `/Volumes/DataSSD1/test/boss/docs/product/BOSS_GAP_ANALYSIS.md` | Phase history + G1–G3 earned autonomy |
| `/Volumes/DataSSD1/test/boss/docs/operations/PRD_CRITICAL_GAPS_2026-08-12.md` | Critical missing/broken ranking |
| `/Volumes/DataSSD1/test/boss/docs/operations/E2E_COMPLETION_PLAN.md` | Community DoD + workstreams |
| `/Volumes/DataSSD1/test/boss/docs/operations/LIVE_QA_2026-08-11.md` | Live API/deploy truth |
| `/Volumes/DataSSD1/test/boss/docs/operations/LIVE_UI_FUNCTIONAL_AUDIT_2026-08-11.md` | UI DEAD/SHELL/FLAKY inventory |
| `/Volumes/DataSSD1/test/boss/.ai/requirements-traceability.md` | Requirement → impl map |
| `/Volumes/DataSSD1/test/boss/.ai/gap-backlog.md` | External GAP-002/003/004 |
| `/Volumes/DataSSD1/test/boss/.ai/release-readiness.md` | Package vs live rollout |
| `/Volumes/DataSSD1/test/boss/.ai/status.md` | Integration/GA process notes |
| `/Volumes/DataSSD1/test/boss/apps/api/tests/test_edition_routers.py` | Community BYOK ProviderOps classification |

---

*Panel 01 complete. Coordinate with architecture, live QA, and edition packaging panels before any GA go/no-go.*
