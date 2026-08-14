# BOSS end-to-end completion plan

**Goal:** Finish critical product loops so goldeneye dogfood (community) can demonstrate a real company OS — not a console shell over half-wired surfaces.  
**System of record:** Plane workspace `goldeneye`, project BOSS.  
**Umbrella:** [BOSS-337](https://plane.local.sean.network)  
**Living tracker (update first after every ship):** [`GA_LIVING_PLAN.md`](GA_LIVING_PLAN.md)  
**GA scorecard:** [`ga-readiness-2026-08-12/GA_READINESS_SCORECARD.md`](ga-readiness-2026-08-12/GA_READINESS_SCORECARD.md)  
**Evidence baseline:** LIVE_QA, LIVE_UI_FUNCTIONAL_AUDIT, PRD_CRITICAL_GAPS, GA panels 01–15.  
**Last plan refresh:** 2026-08-12 — GA assessment; DoD **~3.5/8**; **NOT GA-READY**.

---

## Success criteria (definition of “done”)

A founder can, on `boss.local.sean.network` / `boss-api.local.sean.network` **community**:

| # | Criterion | Status (2026-08-12) |
|---|---|---|
| 1 | Attach **third-party model keys** and **infra API keys** in System Configuration (not only `.env`). | **Partial** — ProviderOps kernel + env SecretRefs live; paste UX incomplete |
| 2 | Issue a **CEO command** → durable task + plan + budget path. | **Mostly** — `qa-live` command+budget PASS |
| 3 | Drive **code work** in sandbox → tests/evidence → PR proposal with receipts (reliable, not 50% DLQ). | **Mostly** — BugBox completed + verify green + PR #83; new-build on current tag unproven |
| 4 | Open **previews** from Mission Control (reachable URLs, not `127.0.0.1`). | **Live replay** — BugBox View 200 `prev_af60b33719b04082b027` |
| 5 | See **honest UI** for anything unpaid or unconfigured (no permanent 404 banners). | **Partial** — improved; Enterprise still visible |
| 6 | Use **GitHub Mission Control** with a real connection+binding **or** clear setup empty-state. | **Done** — 2 conns / 2 binds |
| 7 | Either run a **minimal deploy path** to a sandbox target **or** honest not-configured CI/CD. | **Honest empty** |
| 8 | Have **one automated live gate** that fails the release if the vertical slice regresses. | **Done** — `make qa-live` 10/10 |

Optional / later SKUs (not community DoD): Account Connect, live IdP/SCIM, managed marketplace, full ChangeSet production fleet automation, GA GitHub branch protection.

---

## Workstreams (execute in order within each stream; streams with no dependency can parallelize)

```text
                    ┌─────────────────────────┐
                    │  W0 Deploy community    │
                    │  keys PR + smoke        │
                    └───────────┬─────────────┘
                                │
          ┌─────────────────────┼─────────────────────┐
          ▼                     ▼                     ▼
   ┌────────────┐       ┌────────────┐       ┌────────────────┐
   │ W1 Honesty │       │ W2 Runtime │       │ W3 Factory E2E │
   │ UI/edition │       │ substrate  │       │ vertical slice │
   └─────┬──────┘       └─────┬──────┘       └───────┬────────┘
         │                    │                      │
         └────────────────────┼──────────────────────┘
                              ▼
                    ┌─────────────────────────┐
                    │  W4 Product loops       │
                    │  GH / deploy / SIM /    │
                    │  Studio / Assurance     │
                    └───────────┬─────────────┘
                                ▼
                    ┌─────────────────────────┐
                    │  W5 QA gates + Plane    │
                    │  Done only with live    │
                    │  evidence               │
                    └─────────────────────────┘
```

---

## W0 — Land community keys → **DONE (kernel)** · residual redeploy

| Step | Action | Plane | Exit | Status |
|---|---|---|---|---|
| W0.1 | Merge community keys / full BYOK kernel | BOSS-350 | Merged (#157) | **Done** |
| W0.2 | Build/push + redeploy without `--env-template` | BOSS-350 | healthy | **Partial** — live `968bc47` behind main `4727de7` |
| W0.3 | Live smoke providers + accounts + changesets 200 | BOSS-350, 338 | Plane evidence | **Done** (gate + probes) |
| W0.4 | Close BOSS-350; polish residuals | — | State updated | **Done** (decision + kernel) |

**Next:** Phase A shared-tag redeploy (see `GA_LIVING_PLAN.md`).  
**Risk:** redeploy with `--env-template` clobbers secrets — never use template on redeploy.

---

## W1 — UI honesty & edition polish → **MOSTLY DONE**

| ID | Work | Plane | Depends | Exit | Status |
|---|---|---|---|---|---|
| W1.1 | Soft-partial / full kernel ProviderOps community | BOSS-338 | W0 | No hard banner when kernel works | **Done** (#157) |
| W1.2 | Self-Improvement hide or rewire community | BOSS-339 | — | No 404 CTAs | **Done** |
| W1.3 | Enterprise/Account Connect edition-aware nav | BOSS-338 | — | Not a 404 farm | **Done** — Community nav is SSO; lab hide_nav; paid CTAs gated |
| W1.4 | CI/CD + Monitoring empty states | BOSS-344 | — | Honest copy | **Done** |
| W1.5 | React hydration #418 residual | BOSS-346 | — | pageErrors empty | **Open** residual |
| W1.6 | Decision inbox actionable default | BOSS-337 | — | CEO cockpit usable | **Open** |

---

## W2 — Execution substrate → **PARTIAL** (GA P0)

| ID | Work | Plane | Depends | Exit | Status |
|---|---|---|---|---|---|
| W2.1 | Worker image + `git` + shared tag with api | BOSS-342 | — | No missing-git; tags match | **Code Done**; live **skew** worker `91faf6d` vs api `968bc47` |
| W2.2 | Sandbox host connectivity | BOSS-340 | — | `host.connected: true` | **Done** host; **rootfs still missing** |
| W2.3 | Preview URLs operator-reachable | BOSS-341 | — | View opens real preview | **Code Done**; live View proof **open** |
| W2.4 | DLQ triage (~41 open) | BOSS-342 | W2.1–2 | Trend down | **Open** |
| W2.5 | Model TPM/credits → Decision not endless DLQ | BOSS-342 | — | Visible decision | **Partial** |

---

## W3 — Vertical slice sealed → **PARTIAL**

| ID | Work | Plane | Depends | Exit | Status |
|---|---|---|---|---|---|
| W3.1 | Orchestration `next_move` worker-capable | BOSS-345 | W2.1 | Claimable jobs | **Done** (code; stale “not capable” notes retired) |
| W3.2 | Sealed E2E command → code evidence → PR decision | BOSS-351 | W2, W3.1, W3.3 | Gate + factory green | **Gate Done**; factory reliability **Open** |
| W3.3 | GitHub MC connection+binding dogfood | BOSS-343 | — | Real link or honest empty | **Done** live (+ easy-connect #158) |
| W3.4 | OpenAPI parity lifecycle surfaces | BOSS-349 | — | 0.11.0 + parity gate | **Done** (#161); deploy lag to live |

**Critical path remaining:** W2 rootfs/shared-tag + W3.2 factory reliability (not just control-plane gate).

---

## W4 — Remaining product loops → **MOSTLY OPEN**

| ID | Work | Plane | Depends | Exit | Status |
|---|---|---|---|---|---|
| W4.1 | Minimal deploy target **or** honest empty | BOSS-344 | W2 | Demo or hide | **Honest empty Done** |
| W4.2 | Observability deploy **or** hide | BOSS-344 | W4.1 | No fake Grafana | **Honest empty Done** |
| W4.3 | Agent Studio seed | BOSS-347 | A1 redeploy | Non-empty catalog | **Open** — seed 404 on live tag |
| W4.4 | Improvement programs community path | BOSS-339 | W1.2 | One demo | **Partial** packaging |
| W4.5 | Assurance seed one metric/case | BOSS-337 | — | Not empty shell | **Done (code)** — honest empty + `POST /v1/assurance/seed-demo` (`seeded: true`, fail-closed, idempotent) |
| W4.6 | Earned autonomy G1–G3 | BOSS-348 | W3 | Budget claim order | **Open** — not GA claim |
| W4.7 | Comms dry-run or hide | BOSS-337 | — | Honest | **Open** |

**Paid/external (do not block community DoD):** GAP-002 Account Connect, GAP-003 IdP/SCIM, GAP-004 live provider **production** writes, real Slack/email, GA GitHub protection — track separately; fail-closed is correct until owner enables.

---

## W5 — QA gates & Plane hygiene → **ACTIVE**

| ID | Work | Exit | Status |
|---|---|---|---|
| W5.1 | `scripts/qa/live_dogfood_gate.py` required checks | `make qa-live` 10/10 | **Done** (PASS 2026-08-12) |
| W5.2 | Deploy gate: post-bump always run live suite | Documented + practiced | **Open** — make mandatory SOP |
| W5.3 | Plane: Done only with live evidence | No fake Done | **Active** |
| W5.4 | Weekly signal-poll DLQ/incidents | Process | **Active** (GA panels) |
| W5.5 | Update `GA_LIVING_PLAN.md` after every ship | Changelog + tables | **Required** |

---

## PR / branch strategy

| Stream | Branch pattern | Notes |
|---|---|---|
| W0 | `ai/grok/community-provider-keys` (this PR) | Small, ship first |
| W1 | `ai/grok/edition-mc-honesty`, `ai/grok/sim-community`, `ai/grok/hydration-418` | Parallel small PRs |
| W2 | `ai/grok/worker-image-git`, `ai/grok/sandbox-host`, `ai/grok/preview-urls-deploy` | Deploy-heavy |
| W3 | `ai/grok/orchestration-glue`, `ai/grok/dogfood-e2e-gate` | Sequential after W2 |
| W4 | per feature `ai/grok/<plane-id>-…` | After W3 for factory-dependent items |

Prefer **small reviewable PRs** (AGENTS.md). One requirement ID (or few) per PR. No big-bang “fix everything” branch.

---

## Suggested execution calendar (aggressive dogfood) — refreshed 2026-08-12

| Week | Focus | Plane exits | Reality check |
|---|---|---|---|
| **0** | Community keys + BYOK kernel | 350 Done | **Done** (#157) |
| **1** | Honesty + worker git | 338/339/344 Done; 342 code | **Mostly done** |
| **1–2** | Sandbox host, previews, DLQ | 340 host Done; 341 code | **Rootfs + DLQ remain** |
| **2** | GitHub wire + next_move + OpenAPI + gate | 343/345/349/351 Done | **Done** (factory still flaky) |
| **Now (Phase A)** | Shared-tag redeploy; Studio seed; rootfs; dual-plane honesty | 347; 337 | **In progress** |
| **+1–2 wk (Phase B)** | Sealed factory E2E; View proof; Enterprise hide; hydration | 351 residual; 346 | Open |
| **Phase C** | 14-day green gate + security/legal | 337→Done only then | Open |

Adjust if sandbox host or model credits block factory reliability. **Always update `GA_LIVING_PLAN.md` when a row moves.**

---

## Explicit non-goals (until P0/P1 closed)

- New Mission Control areas / more panels  
- Full multi-cloud adapter suite (AWS/Azure/GCP)  
- Hosted BOSS Cloud / seat billing  
- Making Community identical to Enterprise  
- Declaring GA without branch protection / live E2E gate  

---

## Tracking map (Plane)

| Plane | Workstream | Priority |
|---|---|---|
| 337 | Umbrella live QA | — |
| 350 | Community keys (W0) | P0 |
| 338 | Edition MC honesty (W1) | P0 |
| 339 | Self-improvement community (W1/W4) | P0 |
| 340 | Sandbox host (W2) | P0 |
| 341 | Preview URLs (W2) | P0 |
| 345 | Orchestration glue (W3) | P0 |
| 351 | Sealed E2E (W3) | P0 |
| 342 | Worker/DLQ (W2) | P1 |
| 343 | GitHub MC wire (W3) | P1 |
| 344 | CI/CD empty honesty (W1/W4) | P1 |
| 346 | Hydration (W1) | P1 |
| 348 | Earned autonomy (W4) | P1 |
| 347 | Agent Studio (W4) | P2 |
| 349 | OpenAPI parity (W3) | P2 |

---

## First commands after this plan

```bash
# After PR merge
# build+push amd64 boss-api:<sha> ; deploy boss-app without --env-template
# then:
curl -sS -H "Authorization: Bearer $BOSS_API_KEY" \
  "https://boss-api.local.sean.network/v1/provider-definitions" | head
curl -sS -H "Authorization: Bearer $BOSS_API_KEY" \
  "https://boss-api.local.sean.network/v1/providers?project_id=boss-dogfood" | head
```

Then pick **W2.1 worker image** and **W1.2 SIM hide/rewire** as next parallel PRs while W0 deploy settles.
