# Panel 09 — Assurance / Earned Autonomy Expert Panel

**Date:** 2026-08-12  
**Panel role:** Assurance / Earned Autonomy expert for BOSS GA readiness  
**Repo:** `/Volumes/DataSSD1/test/boss`  
**Sources:** `docs/product/ASSURANCE_CONTRACTS.md`, `docs/product/BOSS_AUTONOMY_FRONTIER_PRD.md`, `docs/product/BOSS_GAP_ANALYSIS.md` (G1–G3), `docs/architecture/AUTONOMOUS_IMPROVEMENT_LOOP.md`, `docs/product/AGENT_STUDIO_A2A.md`, `docs/operations/PRD_CRITICAL_GAPS_2026-08-12.md`, `docs/operations/LIVE_UI_FUNCTIONAL_AUDIT_2026-08-11.md`, `docs/operations/E2E_COMPLETION_PLAN.md`, runtime under `engine/boss_engine/services/{assurance,autonomy_engine,agent_studio,improvement_programs,role_governance,jobs,work_orchestration}.py`, feature ledger AST-001 / ROL-003 / SIM-001, CHANGELOG v0.8–v0.11

---

## 1. Executive verdict

| Claim class | GA honesty | Grade |
|---|---|---|
| **Directed, budget-gated delivery autonomy** (CEO outcome → plan/task/job → decision → receipt) | **Honest for GA** if dogfood loop holds | Core moat |
| **Evidence-based autonomy ladder** (levels 0–4, review → upgrade decision / auto-downgrade) | **Honest as partial** — implemented; trust_score does **not** gate risk tiers | G2 open |
| **Value-based scheduling under FinOps pressure** | **Not honest as complete** — priority_score + ORANGE/RED deferrals exist; full value-per-dollar + SLA-risk claim order incomplete | G1 partial |
| **Full §20.3 weighted trust signals** | **Future** | G3 open |
| **Assurance Center as “proof OS”** | **API/runtime real; product shell on dogfood** | Empty-shell risk **HIGH** |
| **Agent Studio as commissioning plane** | **Code + SpecPack catalog real; dogfood empty** | Seed path exists, not applied live |
| **Autonomy Frontier** (R1–R7: discover → validate demand → portfolio P&L → always-on) | **Future PRD; draft not scheduled** | Do not claim GA |
| **Self-improvement / improvement programs** | **Programs/trials real; Ops SIM UI DEAD on community** | Honest only as proposal→canary→rollback, never auto-mutate |

**Bottom line:** GA can claim a **trusted directed-delivery OS with bounded autonomy, receipts, and fail-closed gates**. It must **not** claim a fully earned-autonomy economy, a populated Assurance proof plane, a live Agent Studio workforce, or FeltSense-style autonomous market outcomes.

---

## 2. Honest autonomy claims — GA vs future

### 2.1 Honest for GA (if messaging is precise)

1. **Autonomy is level-bounded, not unbounded.**  
   `packages/specpack/policies/autonomy_ladder.yaml` defines L0 observe → L4 high_trust with explicit allowed capabilities. Role checks emit `role.autonomy_allowed` / `role.autonomy_denied`.

2. **Autonomy changes are governed.**  
   `AutonomyEngine` gathers event-log evidence since last review and drives `RoleGovernanceService.record_review`: clean windows **propose upgrade Decisions**; violations **auto-downgrade**. Upgrade still requires explicit decision (ladder: `explicit_decision_required: true`).

3. **Side effects are receipted and budget-gated.**  
   Spendy work needs reservations; external effects need dedupe keys / receipts. This is the durable kernel claim and remains the strongest GA differentiator.

4. **Workers lease work; claim order is not pure FIFO.**  
   `JobLeaseService.claim_job` orders by reclaim priority, then `priority_score DESC NULLS LAST`, then static `priority`, then age. Work orchestration can attach an `effective_score` as `priority_score`. Under **ORANGE/RED** budget states, non-critical / non-P0 work is deferred (`_budget_state_policy_for_job`).

5. **Self-improvement is proposal-only.**  
   Improvement programs + trials (`improvement_programs.py`) and the governed loop (`AUTONOMOUS_IMPROVEMENT_LOOP.md`) freeze baselines, require single-variable candidates, and promote only into `SelfImprovementProposal` → canary → staged rollout → auto-rollback. **BOSS never silently rewrites production.**

6. **Discovery / software factory (directed)**  
   Outcome discovery and governed coding pipeline are “build what you approved,” not “find and monetize markets unattended.” That is still valuable and shippable as **directed** autonomy.

### 2.2 Partial — say carefully or fix before hard GA copy

| Gap ID | Spec intent | Code reality | GA copy rule |
|---|---|---|---|
| **G1** Value-per-dollar + SLA-risk scheduling | Under YELLOW/ORANGE/RED, claim by **value/$ and SLA risk** | Multi-factor **work item score** (strategic value, impact, urgency, CoD, risk reduction, dependency pressure) → `priority_score`; budget **defers** non-critical under ORANGE/RED. **No true value÷cost** ranking; not all jobs carry scores | “Priority-aware queue with budget pressure deferrals” — **not** “value-per-dollar scheduler” |
| **G2** Trust score gates **allowed risk tiers** | Trust drives autonomy **and** risk tiers at execution | `trust_score` stored, adjusted on violations/reviews, returned from checks — **not** used as a hard gate on `risk_tier` in `check_autonomy` | “Trust score tracked and used in reviews” — **not** “trust unlocks risk tiers” |
| **G3** Full §20.3 weighted signals | cost-estimate error, deadletter rate, spam/throttle, PR-green-no-rollback, etc. | Evidence mostly allowed/denied/scope counts + simple trust deltas | “Evidence-based review from authorization events” — **not** “full multi-signal trust engine” |

Plane: **BOSS-348** (earned autonomy + value scheduling). E2E plan W4.6.

### 2.3 Future — must not appear in GA one-pagers as shipped

From `BOSS_AUTONOMY_FRONTIER_PRD.md` (**draft, not scheduled**):

| ID | Capability | Why not GA |
|---|---|---|
| **R1** Signal → Initiatives | Always-on “what should we build?” | Beyond directed CEO commands |
| **R2** Demand Validation Gate | Market canary before deep build | Highest-impact future slice; not productized as default lifecycle gate |
| **R3** Portfolio allocation | Multi-initiative fleet under one envelope | Parallelism / portfolio view incomplete |
| **R4** Tier-4 live monetized deploy | Stripe + synthetic purchase canaries | Stops at staging-class tiers + governed flags |
| **R5** Outcome P&L / Value Ledger | Cost vs revenue per initiative | FinOps = execution cost, not business value ledger |
| **R6** Always-on outcome loop | Nightly sense→validate→build→promote | Request-driven + autonomy ceilings only |
| **R7** Human work-orders | Paid scoped human tasks | Optional later |

**Protect the moat wording:** “trusted autonomous *delivery* + chief-of-staff OS,” not “agentic founder that runs the company unattended.”

Also future / non-GA:

- Routing optimizer from outcome data (gap G4).  
- Full SSO/IdP live runtime, Account Connect marketplace, live provider production writes (correctly fail-closed).  
- Langfuse/DSPy/Temporal as required runtime (optional adapters only).

---

## 3. Assurance Center — empty-shell risk

### 3.1 What is real (code / tests)

- **AST-001** marks Ground-truth and Assurance runtime **Implemented**.  
- Durable service: `engine/boss_engine/services/assurance.py` — metrics, observations, baselines, settled conclusions, publication guard, claims/evidence evaluation, surface ownership, verification runs, assurance cases, overview.  
- OpenAPI surfaces under `/v1/assurance/*` and claim/case evaluate routes.  
- SpecPack objects + workflows (BOSS-175–208) and contract tests (`test_assurance_specpack_contracts.py`, `test_assurance_workflow_contracts.py`, `test_governance_runtime.py`).  
- Mission Control UI: `apps/web/app/assurance-ui.tsx` + `lib/assurance.ts`.  
- Contracts correctly state models may **propose** evidence; only measured records/policy make claims authoritative.

### 3.2 Live dogfood truth (2026-08-11/12)

| Observation | Source |
|---|---|
| Assurance graded **SHELL** | LIVE_UI_FUNCTIONAL_AUDIT |
| APIs 200; metrics / cases / surfaces **empty** | PRD_CRITICAL_GAPS §11 |
| UI copy is strong (“Ground truth is measured, fenced, and independently verified”) while counts can be **0** | assurance-ui empty states: “No cases recorded yet”, etc. |
| Bootstrap seeds Agent Studio, workflows, work defaults — **not** assurance demo metrics/cases | `bootstrap.py` |
| E2E plan W4.5: seed one metric + case + verification run | E2E_COMPLETION_PLAN |

### 3.3 Risk assessment

**Empty-shell risk: HIGH (product / GTM), MEDIUM (technical).**

- **Technical:** Not a fake API. Runtime and gates are real when data exists.  
- **Product:** First-run dogfood shows a polished **Assurance Center with zeros**, which operators read as broken or vaporware. Marketing copy (“proof gates”, “settled conclusions”) **overclaims relative to empty tenants**.  
- **Trust moat hazard:** BOSS differentiates on evidence-backed authority. An empty Assurance Center **undermines the exact claim** GA wants to sell.  
- **No automatic population** from code-task / canary / eval systems into Assurance metrics — operators must create contracts or a seed path must exist.

### 3.4 Mitigations before / at GA

1. **Dogfood seed (W4.5):** one metric definition + observation cadence + one Assurance Case + one claim/evidence pair + one verification run (even synthetic green path) on default project.  
2. **Honest empty UX:** if all counts = 0, show “Not commissioned — seed demo proof set or create first metric” with CTA, not hero copy that asserts continuous measurement.  
3. **Optional:** wire canary/eval harness outcomes into `GroundTruthObservation` so the center reflects live kernel health.  
4. **Do not** claim continuous adversarial verification in GTM until a standing verifier has run on dogfood with receipts.

---

## 4. Agent Studio seed status

### 4.1 Intended product (PRD / AGENT_STUDIO_A2A)

- Versioned `AgentDefinition` / `AgentVersion`, panels, workflows, guided wizard, A2A fabric.  
- **Seeded specialist library** (UX, UI, FE, BE, full-stack, DBA, architect, security, QA, chief operator, CFO, DevOps + Product Manager in SpecPack).  
- Seeded agents are **editable product assets**, default **deterministic / side-effect free** until governed live model/tool path.

### 4.2 Implementation status

| Layer | Status |
|---|---|
| SpecPack agents | **13 YAML** under `packages/specpack/agents/` |
| SpecPack panels | **3** (`architecture_risk_council`, `product_delivery_council`, `release_readiness_panel`) |
| `AgentStudioService.seed_catalog` | Loads agents + panels, validates, **publishes** versions |
| Bootstrap | **Calls** `seed_catalog` on project seed |
| Explicit API | `POST /v1/agent-studio/catalog/seed` (idempotent, no external effects) |
| Catalog smoke | `POST /v1/agent-studio/catalog-smoke` — zero-model authority/contract check; CHANGELOG notes all-agent smoke |
| Worker | `agent.studio.execute` job kind supported |
| Tests | `test_agent_studio_a2a.py`, conftest seeds catalog |

### 4.3 Live dogfood status

| Observation | Grade |
|---|---|
| Catalog **200** with `agents=[]`, `panels=[]`, `workflows=[]` | **SHELL** |
| UI signal “not configured” | Degraded |
| A2A health **degraded** (transport) | Partial |
| Catalog smoke with 0 items does not prove workforce | Misleading green risk |
| Plane **BOSS-347** — seed/setup so empty ≠ broken | Open |
| E2E W4.3 | Seed demo **or** first-run wizard |

**Root cause class:** seed path exists (bootstrap + `/catalog/seed`) but **dogfood project was not seeded / reseeded** (image bootstrap path, seed API disabled in prod, project created outside bootstrap, or edition deploy predating seed). This is an **ops/setup gap**, not a missing library design.

### 4.4 GA recommendation for Studio

1. On GA install / dogfood: **must** run bootstrap seed **or** surface a one-click “Install starter catalog” that calls `/v1/agent-studio/catalog/seed`.  
2. Empty catalog UI must say **“Starter catalog not installed”** with CTA — never a full workbench that looks operational.  
3. After seed: run **catalog-smoke** and show published agent count.  
4. Do not claim multi-agent Panel councils as live org process until a panel run has durable evidence on dogfood.  
5. A2A: claim **durable envelope ledger** when present; do not claim healthy low-latency transport while health is degraded.

---

## 5. Improvement programs vs “self-improving AI company”

| Surface | Reality |
|---|---|
| Improvement programs / trials | Real control plane; metric contracts; isolated candidates; proposal-only promotion |
| Self-improvement proposals canary/rollout/rollback | Service exists historically; **Ops UI DEAD** on community dogfood (`/v1/self-improvement/*` 404 vs `/v1/improvement-programs/*`) |
| Autonomous Improvement Loop doc | Correct architecture: DSPy/Langfuse optional; BOSS ledger authoritative |
| GA claim | “Governed improvement trials with human approval and rollback” |
| Forbidden claim | “BOSS rewrites itself” / “continuous unattended self-evolution” |

Edition honesty: either wire SIM UI to improvement-programs on community or hide SIM until paid routes exist (LIVE_UI audit P0).

---

## 6. Messaging matrix (copy gate)

| Allowed GA language | Disallowed GA language |
|---|---|
| Bounded autonomy levels with decision-gated upgrades | Fully autonomous company / agentic founder |
| Evidence-backed reviews and auto-downgrade on violations | Trust score fully earns risk-tier freedom |
| Priority- and budget-pressure-aware job leasing | Value-per-dollar SLA optimizer complete |
| Assurance runtime for metrics, claims, ownership, verifiers | Live continuous proof plane on every install |
| Seedable Agent Studio specialist catalog | Pre-commissioned multi-agent workforce on empty dogfood |
| Proposal → canary → rollout → rollback improvement | Silent self-modification |
| Directed discovery + governed delivery | Market demand validation + portfolio P&L always-on (R1–R6) |

---

## 7. Recommended GA readiness actions (this panel only)

| Priority | Action | Plane / plan |
|---|---|---|
| **P0** | Dogfood: `POST /v1/agent-studio/catalog/seed` (or bootstrap reseed) + catalog-smoke | BOSS-347 / W4.3 |
| **P0** | Assurance empty-state honesty + seed one proof set | W4.5 |
| **P0** | Hide or rewire Self-Improvement UI vs improvement-programs | BOSS-339 / LIVE audit |
| **P1** | Close G2: enforce min trust_score (or scorecard band) for risk_tier ≥ N | BOSS-348 / G2 |
| **P1** | Close G1 remainder: budget-pressure claim order uses value/cost (or document partial) | BOSS-348 / G1 |
| **P2** | G3 multi-signal trust weights | BOSS-348 / G3 |
| **P2** | Autonomy Frontier Phase A (R2) as post-GA roadmap only | Frontier PRD |
| **Copy** | GTM/PR: “trusted directed delivery OS” not “autonomous outcomes OS” until R2–R6 land | Marketing gate |

---

## 8. Panel scorecard

| Dimension | Score (0–5) | Notes |
|---|---|---|
| Autonomy ladder + review engine | **4** | Real; decision-gated upgrades |
| Earned autonomy principle (G1–G3) | **2** | Partial scheduling; no trust→risk gate; thin signals |
| Assurance runtime completeness | **4** | Strong implementation |
| Assurance product / dogfood | **1** | Empty shell |
| Agent Studio implementation | **4** | Versioned registry + seed + smoke |
| Agent Studio dogfood | **1** | Empty library |
| Improvement governance posture | **5** | Correct proposal-only design |
| Improvement UX honesty | **1** | SIM DEAD / split surfaces |
| Frontier autonomy (R1–R7) | **0** | Draft future |
| **Overall GA honesty if claims stay narrow** | **3.5** | Shipable with disciplined messaging + seed fixes |
| **Overall if marketing sells full assurance + earned autonomy + studio workforce** | **1.5** | Fail GTM integrity |

---

## 9. Final panel statement

**For GA, BOSS may honestly claim:** a durable, budget-gated, receipted work kernel with **level-bounded autonomy**, **evidence-triggered downgrades and decision-gated upgrades**, **priority-aware leased workers**, and a **real Assurance + Agent Studio control plane that becomes useful once seeded**.

**BOSS must not claim for GA:** full **earned-autonomy economy** (G1–G3 closed), a **populated Assurance Center** by default, a **pre-filled Agent Studio**, unattended **market discovery / demand validation / portfolio P&L / always-on loops** (Autonomy Frontier R1–R6), or **self-modifying** improvement.

**Empty-shell risk is the primary commercial defect in this panel’s scope** — not missing Python modules. Seed Assurance proof sets and Agent Studio catalog on dogfood (or first-run CTAs), fix SIM/improvement-programs UI honesty, and keep GA language inside the directed-delivery moat.
)
