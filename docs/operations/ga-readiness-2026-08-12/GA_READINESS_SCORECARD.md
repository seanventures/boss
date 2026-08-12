# BOSS GA Readiness Scorecard

**Date:** 2026-08-12 (UTC)  
**Campaign:** Full GA readiness with 15 parallel expert panels + live dogfood gate  
**Repo main:** `4727de7` (includes #158 GitHub easy-connect, #159 Studio seed, #160–#161 OpenAPI)  
**Live dogfood images:** api/web `968bc47`, worker `91faf6d` (**behind main** — deploy skew)  
**Edition live:** community  
**Plane umbrella:** BOSS-337  

> **Living plan (update along the way):** [`../GA_LIVING_PLAN.md`](../GA_LIVING_PLAN.md)  
> This scorecard is the **assessment snapshot**. Progress, phase exits, and DoD scores move in the living plan first.

---

## Executive verdict

### **NOT GA-READY** for public “autonomous company OS” claims.

### **CONDITIONAL GO** for **Community self-host dogfood / private preview**, if marketing claims are narrowed to:

> “Governed control plane: commands, budget, decisions, receipts, BYOK ProviderOps kernel, project GitHub connect, honest empty states for unconfigured surfaces.”

| Scope | Verdict |
|---|---|
| Public multi-tenant SaaS GA | **NO** |
| Community self-host package GA | **CONDITIONAL** (P0 list below) |
| Enterprise SKU GA | **NO** (external config + fail-closed by design) |
| “Earned autonomy / workforce Studio” GA | **NO** (shell live) |

**Live control-plane gate:** `live_dogfood_gate` **10/10 required PASS** (+ Metatron optional PASS) on 2026-08-12.

---

## Method

| Layer | What ran |
|---|---|
| Parallel expert panels | **15** specialized agents (PRD, control plane, ProviderOps, GitHub/factory, sandbox/delivery, MC UX, security, worker, assurance/autonomy, contracts, live dogfood, hard rules, edition packaging, ops, Agent Studio) |
| Live functional | `scripts/qa/live_dogfood_gate.py` → **PASS** |
| Live probes | providers/secret-refs/changesets/worker-pools/github/sandboxes/previews/DLQ; account-connect/self-improvement **404** (expected community) |
| Evidence tree | `docs/operations/ga-readiness-2026-08-12/panels/01`–`15`, gate JSON/log |

Panels that used **stale** 08-11 empty-GitHub assumptions are **overridden** by panel 11 live proof (GitHub wired).

---

## Scorecard by domain

| # | Domain | Panel grade | Live truth (2026-08-12) | GA |
|---|---|---|---|---|
| 01 | PRD completeness | **NO full GA** | Core loops Partial; shells remain | NO |
| 02 | Control plane kernel | WARN → CONDITIONAL | Gate PASS; budget green | COND |
| 03 | ProviderOps BYOK | CONDITIONAL YES | providers 4, changesets 200 | COND |
| 04 | GitHub + factory | YELLOW | **2 conns / 2 binds** wired; factory dual-plane | COND |
| 05 | Sandbox / preview / delivery | YELLOW | Host connected; rootfs missing; DLQ | NO exec |
| 06 | Mission Control UX | YELLOW | Chrome strong; many shells | COND |
| 07 | Security / auth / RBAC | CONDITIONAL | Auth fail-closed live | COND |
| 08 | Worker reliability | CONDITIONAL | 41 open deadletters | NO auto-code |
| 09 | Assurance / autonomy | Partial / shell | Empty assurance/studio live | NO claim |
| 10 | OpenAPI / tests / docs | YELLOW | main 0.11.0; live still 968bc47 OpenAPI lag | COND |
| 11 | Live dogfood | YELLOW | Gate 10/10; deploy skew; DLQ | COND |
| 12 | Hard-rules compliance | Partial | Kernel strong; Noop budget community | COND |
| 13 | Edition packaging | Community ship OK w/ honesty | Enterprise 404 correct | COND |
| 14 | Ops / observability | CONDITIONAL | Monitoring honest empty; image skew | COND |
| 15 | Agent Studio / A2A | LIVE-SHELL | agents=0; seed 404 on live image | NO |

---

## What is actually green (ship-worthy if scoped)

1. **Health + auth fail-closed** (unauthenticated 401).  
2. **CEO command propose → task** (gate).  
3. **Budget quote green** (~$88 available on dogfood).  
4. **Decisions inbox** readable (60+).  
5. **Community ProviderOps kernel** (providers, secret-refs, changesets, worker-pools) **200**.  
6. **Enterprise surfaces correctly 404** on community (account-connect, self-improvement).  
7. **GitHub project connect + bindings + repo list + easy-connect API** (live wired).  
8. **Metatron status Q&A** optional gate PASS.  
9. **OpenAPI 0.11.0 + 79-route parity gate** on main (needs deploy to live).  
10. **Fail-closed provision flags** (`INFRA_REAL_PROVISION_ALLOWED` default false).

---

## P0 blockers before any “GA” tag

| ID | Blocker | Evidence | Exit criteria |
|---|---|---|---|
| P0-1 | **Deploy skew** api/web `968bc47` vs main `4727de7`; worker `91faf6d` | Panel 11/14 | One shared tag api+worker+web on main; `qa-live` green |
| P0-2 | **code.task / factory reliability** — 41 open deadletters | Panel 08/11 | DLQ burn-down plan; missing-git gone; success rate gate |
| P0-3 | **Sandbox rootfs / provision** — host up but guest rootfs missing | Panel 05/11 | One microVM task `done` with receipt |
| P0-4 | **Preview View not dogfood-proven** after publicize | Panel 05/11 | Browser opens non-loopback View URL |
| P0-5 | **Agent Studio uncommissioned** — empty catalog; seed 404 on live | Panel 15 | Redeploy seed route; seed 13/3; one deterministic run |
| P0-6 | **GitHub dual-plane** — MC connection ≠ factory adapter | Panel 04 | Factory uses primary binding **or** documented dual path in UI |
| P0-7 | **Honest GTM copy** — no “autonomous company” / populated Assurance/Studio | Panels 01/06/09 | Claims freeze list signed |

### P1 (private preview OK, public GA not)

| ID | Item |
|---|---|
| P1-1 | Residual hydration #418 (BOSS-346) |
| P1-2 | Hide/badge Enterprise nav on community (still always shown) |
| P1-3 | Spend-kind reservation matrix for all agent jobs (panel 02) |
| P1-4 | Concurrent receipt `ON CONFLICT` reuse (panel 02) |
| P1-5 | Assurance empty-state honesty + optional seed (BOSS-348 partial) |
| P1-6 | CI dep fail-on-high; restore full openapi-validate CI (panel 10) |
| P1-7 | Rate-limit multi-replica store (panel 07) |

### P2 / external blocked (correct)

- Account Connect, live IdP/SCIM, marketplace billing  
- GitHub Actions enabled + private main protection (sandbox plan limits)  
- Azure/GCP/OVH live adapters  
- Autonomy Frontier R1–R7, G2/G3 earned autonomy economy  

---

## Community GA DoD (from E2E plan) — re-score

| # | Criterion | Status 2026-08-12 |
|---|---|---|
| 1 | Attach third-party model + infra keys | **Partial** — env SecretRefs + providers work; paste-UX incomplete |
| 2 | CEO command → task + plan + budget | **Mostly** — gate prove command+budget; full plan/job seal uneven |
| 3 | Code work → evidence → PR reliable | **NO** — DLQ/substrate |
| 4 | Previews reachable | **NO / unproven** |
| 5 | Honest UI unpaid/unconfigured | **Partial** — improved; Enterprise still visible |
| 6 | GitHub MC real connection | **YES** (live) |
| 7 | Minimal deploy path or honest empty CI/CD | **Honest empty** (0 targets) |
| 8 | Automated live gate | **YES** (`qa-live` 10/10) |

**Score: ~3.5 / 8 fully closed** (improved from 0/8 earlier this week on GitHub + BYOK kernel + gate).

---

## Recommended GA path (get to ready)

### Phase A — 48h (must)

1. **Redeploy** boss-app api+worker+web from main short SHA (shared tag).  
2. Re-run `make qa-live`.  
3. **Seed Agent Studio** on dogfood; prove catalog non-empty.  
4. Bake/fix **sandbox guest rootfs** or disable microVM claims until green.  
5. DLQ triage: top failure classes → tickets; kill missing-git permanently.  

### Phase B — 1–2 weeks (private preview)

6. One sealed **command → code evidence → PR decision** on dogfood weekly gate.  
7. Unify factory GitHub adapter with primary binding (or UI honesty).  
8. Preview View browser proof; freeze marketing.  
9. Edition nav: hide Enterprise on community.  
10. Close BOSS-346 residual hydration.  

### Phase C — Public Community GA

11. All P0 green for 14 consecutive days of `qa-live`.  
12. Security must-fix (principal maps, dep CI, rate limits).  
13. Legal/OSS readiness (`engine/COMMUNITY_READINESS.md`) if public.  
14. Release tag + notes with **allowed claims list**.  

---

## Allowed vs forbidden claims (freeze)

### Allowed (after Phase A)

- Self-hosted governed control plane (tasks, budgets, decisions, receipts, events).  
- Community BYOK ProviderOps kernel (keys as env SecretRefs, inventory, ChangeSets with dual-control).  
- Project-scoped GitHub connect + repo link + connection test receipts.  
- Metatron status queries.  
- Honest empty states for CI/CD, monitoring when undeployed.  

### Forbidden until proven

- “Autonomous company that ships reliably unattended.”  
- “Agent Studio workforce ready out of the box” (empty live).  
- “Earned autonomy / trust economy” (G1–G3 incomplete).  
- “Sandboxes/previews always work.”  
- “Enterprise SSO/SCIM/marketplace included in community.”  
- Zero deadletters / production GitHub Actions green on private sandbox.  

---

## Panel index

| File | Focus |
|---|---|
| [panels/01-prd-completeness.md](panels/01-prd-completeness.md) | PRD matrix |
| [panels/02-control-plane-kernel.md](panels/02-control-plane-kernel.md) | Jobs/budget/receipts |
| [panels/03-providerops-byok.md](panels/03-providerops-byok.md) | Community ProviderOps |
| [panels/04-github-factory.md](panels/04-github-factory.md) | GitHub + factory |
| [panels/05-sandbox-delivery.md](panels/05-sandbox-delivery.md) | Sandbox/preview/CI |
| [panels/06-mission-control-ux.md](panels/06-mission-control-ux.md) | UI/UX scorecard |
| [panels/07-security-auth.md](panels/07-security-auth.md) | Security |
| [panels/08-worker-reliability.md](panels/08-worker-reliability.md) | Worker/DLQ |
| [panels/09-assurance-autonomy.md](panels/09-assurance-autonomy.md) | Autonomy honesty |
| [panels/10-contracts-tests-docs.md](panels/10-contracts-tests-docs.md) | OpenAPI/tests/docs |
| [panels/11-live-dogfood.md](panels/11-live-dogfood.md) | Live probes |
| [panels/12-hard-rules-compliance.md](panels/12-hard-rules-compliance.md) | AGENTS hard rules |
| [panels/13-edition-packaging.md](panels/13-edition-packaging.md) | Community vs Enterprise |
| [panels/14-ops-observability.md](panels/14-ops-observability.md) | Deploy/ops |
| [panels/15-agent-studio-a2a.md](panels/15-agent-studio-a2a.md) | Studio/A2A/Metatron |
| [live_dogfood_gate.json](live_dogfood_gate.json) | Automated gate |

---

## Sign-off

| Role | Disposition |
|---|---|
| Product / PRD panel | **NO** full GA |
| Live QA panel | **CONDITIONAL** control-plane dogfood |
| Security panel | **CONDITIONAL** with must-fix |
| Ops panel | **CONDITIONAL** shared-tag redeploy required |
| **Overall** | **NOT GA-READY** — track Phase A P0s under BOSS-337 |

*Assessment produced by 15 parallel expert agents + live gates. Close only with functional proof.*
