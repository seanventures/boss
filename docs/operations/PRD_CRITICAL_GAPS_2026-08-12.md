# PRD critical features vs reality — 2026-08-12

**Method:** Six parallel explore agents read PRD-v2 / technical specs / gap docs / OpenAPI / vertical-slice code paths / live goldeneye QA.  
**Deploy truth:** community `boss-app` on goldeneye (see `LIVE_QA_2026-08-11.md`, `LIVE_UI_FUNCTIONAL_AUDIT_2026-08-11.md`).  
**Plane:** BOSS-337.

This is **product gap analysis**, not a full re-test. Use it with the live UI audit.

---

## One-line verdict

**Control-plane primitives are largely built and tested. The closed-loop “autonomous company OS” dogfood story is not.**  
Mission Control presents many finished surfaces; community dogfood cannot demonstrate several PRD-critical loops (ProviderOps, reliable code→PR, sandboxes/previews, self-improvement, deploy/monitor, GitHub MC).

Vertical-slice composite grade: **YELLOW**  
(kernel GREEN · orchestration glue YELLOW · dogfood execution RED/YELLOW · ProviderOps/SIM live RED)

---

## Priority legend

| Class | Meaning |
|---|---|
| **CRITICAL-MISSING** | PRD P0/P1; not demonstrable end-to-end on dogfood (or missing entirely) |
| **CRITICAL-BROKEN** | Built, but live fails / flaky / edition-dead while UI claims it |
| **PACKAGING-MISMATCH** | PRD/Community promises disagree with edition gates |
| **EXTERNAL-BLOCKED** | Fail-closed by design; needs creds/IdP/cluster/approval |
| **DOC-STALE** | Docs claim shipped; dates/versions disagree — verify before re-building |

---

## A. Critical missing / broken for “BOSS as company OS” (ranked)

### 1. End-to-end Software Factory loop — **CRITICAL-BROKEN**

**PRD:** CEO command → spec → plan → budget → sandbox code → tests/review packet → PR decision → (optional) deploy.  
**Code:** Exists (commands, code-tasks, software-factory, decisions).  
**Live:**

- Command intake **works**
- Code tasks **FLAKY** (high deadletter rate: max turns, TPM, credits, missing **git** in worker, `code-tasks/step` 500)
- Sandbox host **down** (connection refused)
- Previews often **`127.0.0.1`** — operator cannot open
- GitHub Mission Control **0 connections/bindings** (token path ≠ MC wiring)
- Worker image **skew** (`norebuild` vs api/web `view2`)

**Why critical:** This is the primary dogfood narrative.

### 2. Orchestration glue after plan — **CRITICAL-MISSING (glue)**

**Finding (code):** `plan.dispatched` → default subscription `agent.orchestrator.next_move`, but that job kind is **not** in worker `capabilities()`.  
`ceo_command_to_pr` SpecPack is a **definition**, not a sealed live runner.  
**Result:** CEO command creates task/plan/job pieces, but **automatic multi-phase graph execution is fragmented**; operators must hit explicit routes (code-task, software-factory).

### 3. ProviderOps / Elastic Resource Plane — **CRITICAL-BROKEN + PACKAGING-MISMATCH**

**PRD:** Resource Plane is core; Community should include **basic ProviderOps read-only inventory**.  
**OpenAPI:** Full ProviderOps present.  
**Runtime:** `/v1/providers`, `/v1/resources`, `/v1/changesets`, … **enterprise-gated** → community **404**.  
**UI:** System Configuration + **every area SSR** still fans out → permanent `apiReachable=false`.

**Cannot demo:** inventory, ChangeSet → approve → actuate → ResourceReceipt, janitor, autoscaler.

### 4. Self-improvement change management — **CRITICAL-BROKEN + PACKAGING-MISMATCH**

**PRD moat:** measured improvements only via proposal + eval + canary + rollback (never auto-apply).  
**Code/OpenAPI:** `/v1/self-improvement/*` exists and is **enterprise-gated**.  
**Also:** `/v1/improvement-programs/*` is a **separate** community-class family (trial ledger → promote into SIM).  
**Live:** SIM list/create **404**; improvement-programs **empty**; Ops UI still full SIM forms.

### 5. Sandboxes + operator previews — **CRITICAL-BROKEN**

- Sandbox agent host: **connection refused**, empty list  
- Previews: many URLs **localhost-only**

Breaks execution-plane inspection and “View the app” feedback.

### 6. GitHub project-governed merge plane — **CRITICAL-MISSING on dogfood (wiring)**

**Code:** Rich GHX APIs (connections, bindings, leases, sync, conflicts, merge auth).  
**Live:** 0 connections, 0 bindings → Mission Control GitHub is a **shell**.  
Governed merge/sync cannot be demonstrated without wiring.

### 7. Deploy / monitor closed loop — **CRITICAL-MISSING on dogfood**

- 0 deployment targets; health `not_deployed`  
- Observability stack not deployed  
- UI still full CI/CD + Monitoring machinery  
- Software-factory **production** deploy remains external-blocked by design  

### 8. Mission Control honesty / Community product trust — **PACKAGING-MISMATCH**

PRD: Community must be useful, not a crippled demo; open core must feel complete.  
Live: Enterprise, ProviderOps, SIM panels **visible but dead**; edition correctly omits backends.  
**Same failure class as delivery/edition tests warn about.**

### 9. Earned autonomy + value-based scheduling — **CRITICAL-MISSING (PRD principle)**

From `BOSS_GAP_ANALYSIS.md` re-audit: value-per-dollar / trust_score risk-tier gates / SLA-risk claim order still open as MVP principle (G1–G3).  
Not fixed by “more panels.”

### 10. Comms outbox as company backbone — **PARTIAL / SHELL live**

**Code:** Outbox/inbox APIs exist.  
**Live:** empty; real Slack/email fail-closed (correct).  
**Product gap:** looks complete without a real channel loop; Constitution §5 not dogfood-visible.

### 11. Assurance / ground-truth as populated product — **SHELL live**

APIs exist; live metrics/cases/surfaces **empty**.  
Cannot demo “proof gates” with real settled conclusions.

### 12. Agent Studio as commissioning plane — **SHELL live**

Empty agents/panels/workflows; A2A degraded; UI “not configured.”  
PRD Studio/groups cannot be demoed.

---

## B. What *is* critical and working (do not rebuild)

| Capability | Live / code |
|---|---|
| Auth + project scope | Live PASS |
| CEO command propose → task/job/plan | Live PASS |
| Budget quote/reserve/release + fail-closed consume | Live PASS (note community `NoopBudgetPort` composition vs Postgres enterprise — verify spend enforcement on dogfood) |
| Metatron status Q&A | Live PASS |
| Agent chat | Live PASS |
| Decisions inbox API | Live PASS (UX noisy) |
| Leases / worker poll / DLQ visibility | Live PASS |
| Receipts / events / audit lists | Live PASS |
| Packs, roles, skills, MCP inventories | Live PASS |
| Kernel hard rules in tests (dedupe, denial visibility, etc.) | Docs + tests claim strong |

---

## C. External-blocked (real, not fake work)

| ID | Feature | Class |
|---|---|---|
| GAP-002 | Hosted Account Connect / billing / marketplace / team | External / Cloud |
| GAP-003 | Live Enterprise IdP + SCIM/SAML runtime | External |
| GAP-004 | Live ProviderOps writes + live cluster proof | External + approval |
| — | Real Slack/email/Mattermost send | External + policy flags |
| — | GA process (branch protection 403, Actions disabled) | Process, not feature |

Do not claim GA or Cloud until these are real.

---

## D. Vertical slice step grades (summary)

| Step | Grade | Why |
|---|---|---|
| CEO command | GREEN | Live + code |
| Plan + budget | GREEN | Live budget + plan create; auto-reserve not universal |
| Task graph auto-execution | YELLOW | Dispatch exists; orchestration job kind not worker-capable |
| Leased workers | GREEN | Primitive solid; substrate flaky |
| Artifacts/tests/review | YELLOW | Implemented; dogfood flaky |
| Decisions | GREEN / UX YELLOW | API solid; inbox noisy |
| Receipts/audit/DLQ | GREEN | Visible; backlog dirty |
| ChangeSet/deploy | RED live community | Edition + empty targets |
| Learn/propose | RED live community | SIM enterprise-gated |

---

## E. OpenAPI / contract debt (affects agents & customers)

High-impact paths **runtime-ahead-of-OpenAPI** (or partial):

- Full `/v1/code-tasks` lifecycle (OpenAPI only feedback/preview fragments)  
- Entire `/v1/delivery/*` and much of `/v1/deployment-targets*`  
- `/v1/sandboxes*`

Kernel command/task/budget/job/decision/receipt surfaces are **strong** in OpenAPI.

---

## F. PRD Community vs code packaging (must resolve)

| PRD Community expectation | Live/code reality |
|---|---|
| Basic ProviderOps **read-only** | Providers/resources **paid-gated** |
| Useful free tier, not crippled demo | Dead panels + 404 noise on MC |
| Self-improvement as governed path | SIM paid; UI still shows it |
| Simple Mission Control in OSS/Community (GTM) | GTM gap: console often framed paid-side |

**Product decision required:** either promote read-only ProviderOps + SIM to community, or **edition-aware UI** that hides paid surfaces cleanly.

---

## G. Recommended work stack (product-critical only)

### Tier 0 — Dogfood honesty (days)

1. Edition-aware Mission Control (stop ProviderOps/enterprise/SIM 404 fan-out).  
2. Wire SIM UI to real routes **or** hide; wire improvement-programs if that’s the community path.  
3. Sandbox host connectivity + preview URLs operators can open.  
4. Worker image parity + `git` + DLQ triage for code.task.  
5. Wire or hide GitHub MC / CI-CD empty states.

### Tier 1 — Close the vertical slice (weeks)

6. Fix orchestration glue (`agent.orchestrator.next_move` or replace with sealed `ceo_command_to_pr` runner).  
7. One automated E2E: command → budgeted code-task → green evidence → PR decision → receipts.  
8. React hydration #418 on core Run areas.  
9. Decision inbox “pending only” default UX.

### Tier 2 — OS differentiation

10. Community read-only ProviderOps **or** honest paid upsell.  
11. Earned-autonomy / value scheduling (G1–G3).  
12. Populated Assurance demo path.  
13. Agent Studio seed/setup so empty ≠ broken.  
14. OpenAPI parity for code-tasks / delivery / sandboxes.

### Tier 3 — External (when owner ready)

15. GAP-002/003/004, real comms, GA process gates.

---

## H. Parallel agent sources (this session)

| Agent | Focus |
|---|---|
| PRD extract | Critical feature inventory from PRD-v2 + workflow |
| Tech/impl status | Docs claim shipped vs blocked |
| Live QA map | UI areas vs live grades |
| OpenAPI coverage | Contract vs PRD steps + edition gates |
| Vertical slice | Step scorecard + orchestration gap |
| Gap docs mine | BOSS_GAP_ANALYSIS, GTM, GAP_BACKLOG, chatbot gap |

---

## Bottom line for product leadership

**Missing critical features are not “we forgot to build APIs.”**  
They are:

1. **Closed loops that don’t hold on dogfood** (factory, sandboxes, previews, GitHub MC, deploy).  
2. **Edition packaging that contradicts PRD Community** (ProviderOps, SIM).  
3. **Orchestration glue** between command and multi-step execution.  
4. **UI honesty** (shells that look finished).  
5. **A few true product principles still open** (earned autonomy / value scheduling).  
6. **External Cloud/Enterprise** still correctly fail-closed.

Ship the dogfood closed loop and edition honesty before adding more Mission Control panels.
