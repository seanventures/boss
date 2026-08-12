# BOSS end-to-end completion plan

**Goal:** Finish critical product loops so goldeneye dogfood (community) can demonstrate a real company OS — not a console shell over half-wired surfaces.  
**System of record:** Plane workspace `goldeneye`, project BOSS.  
**Umbrella:** [BOSS-337](https://plane.local.sean.network)  
**Evidence baseline:** LIVE_QA, LIVE_UI_FUNCTIONAL_AUDIT, PRD_CRITICAL_GAPS (this folder).  
**In flight:** PR for community API keys (BOSS-350/338) — deploy after merge.

---

## Success criteria (definition of “done”)

A founder can, on `boss.local.sean.network` / `boss-api.local.sean.network` **community**:

1. Attach **third-party model keys** and **infra API keys** in System Configuration (not only `.env`).
2. Issue a **CEO command** → durable task + plan + budget path.
3. Drive **code work** in sandbox → tests/evidence → PR proposal with receipts (reliable, not 50% DLQ).
4. Open **previews** from Mission Control (reachable URLs, not `127.0.0.1`).
5. See **honest UI** for anything unpaid or unconfigured (no permanent 404 banners).
6. Use **GitHub Mission Control** with a real connection+binding **or** clear setup empty-state.
7. Either run a **minimal deploy path** to a sandbox target **or** honest not-configured CI/CD.
8. Have **one automated live gate** that fails the release if the vertical slice regresses.

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

## W0 — Land community keys (now)

| Step | Action | Plane | Exit |
|---|---|---|---|
| W0.1 | Merge PR `community third-party and infra API keys` | BOSS-350 | Merged to main |
| W0.2 | Build/push `boss-api` (and web if needed) amd64; redeploy `boss-app` **without** clobbering `.env` via template | BOSS-350 | `deploy ls` healthy |
| W0.3 | Live smoke: providers catalog + accounts 200; register one test provider account (or list empty OK); System Configuration not hard-dead on catalog | BOSS-350, 338 | Comment evidence on Plane |
| W0.4 | Close or split BOSS-350 when keys path proven; keep BOSS-338 open if residual paid partialFailures need UI polish | — | State updated |

**Risk:** redeploy with `--env-template` clobbers secrets — use goldeneye redeploy without template.

---

## W1 — UI honesty & edition polish

| ID | Work | Plane | Depends | Exit |
|---|---|---|---|---|
| W1.1 | Soft-partial paid ProviderOps (ChangeSets/pools/scale): never `apiReachable=false` when catalog+accounts work | BOSS-338 | W0 | Live MC no hard ProviderOps banner |
| W1.2 | Self-Improvement: hide on community **or** rewire to improvement-programs with working list/create | BOSS-339 | — | No 404 CTAs in Ops |
| W1.3 | Enterprise/Account Connect: edition-aware nav (hide or upsell) on community | BOSS-338 | — | Enterprise area not a 404 farm |
| W1.4 | CI/CD + Monitoring empty states when 0 targets / stack not deployed | BOSS-344 | — | Honest copy + setup CTA |
| W1.5 | React hydration #418 on work, code_tasks, shepherd, reports, pipeline, org | BOSS-346 | — | Playwright pageErrors empty |
| W1.6 | Decision inbox default to pending/actionable (not expired noise) | (new or 337) | — | Approvals usable as CEO cockpit |

**Parallelism:** W1.2–W1.5 can run as parallel PRs after W0.

---

## W2 — Execution substrate (dogfood reliability)

| ID | Work | Plane | Depends | Exit |
|---|---|---|---|---|
| W2.1 | Align worker image tag/SHA with api; rebuild with `git` + allowlisted tools | BOSS-342 | — | No missing-git DLQ; images match |
| W2.2 | Fix sandbox agent host (`BOSS_SANDBOX_AGENT_URL`) connectivity from api/worker | BOSS-340 | — | `host.connected: true` or explicit offline |
| W2.3 | Preview URLs operator-reachable (tailnet/public), never loopback for MC View | BOSS-341 | — | View opens real preview |
| W2.4 | Triage/reap 39 open deadletters; fix recurring classes (step 500, NoneType, allowlist) | BOSS-342 | W2.1–2 | DLQ trend down; root causes fixed |
| W2.5 | Model path hygiene: TPM/credits/fallback documented; fail to Decision not infinite DLQ | BOSS-342 | — | Budget/model failures → visible decision |

**Parallelism:** W2.1 image build, W2.2 host fix, W2.3 preview (code may already be on main post-view2) can parallel.

---

## W3 — Vertical slice sealed (core product)

| ID | Work | Plane | Depends | Exit |
|---|---|---|---|---|
| W3.1 | Fix orchestration glue: implement worker capability for `agent.orchestrator.next_move` **or** replace with sealed `ceo_command_to_pr` WorkflowRun executor | BOSS-345 | W2.1 | Plan dispatch produces claimable jobs |
| W3.2 | One automated E2E: command → budgeted code evidence → PR decision → receipts | BOSS-351 | W2, W3.1, W3.3 | Script/gate green on dogfood |
| W3.3 | Wire GitHub project connection+binding for dogfood **or** factory uses only sandbox token path with clear UI | BOSS-343 | — | Real PR evidence or honest non-GH path |
| W3.4 | OpenAPI parity for code-tasks lifecycle, delivery, sandboxes (contract matches runtime) | BOSS-349 | W3.2 | openapi.yaml + CI check |

**Critical path:** W2 → W3.1 → W3.2. Without substrate + glue, E2E is theater.

---

## W4 — Remaining product loops

| ID | Work | Plane | Depends | Exit |
|---|---|---|---|---|
| W4.1 | Minimal deploy: one sandbox/staging deployment target + health (if flags allow) **or** permanently clear empty-state (W1.4) | BOSS-344 | W2 | Demo path or honest hide |
| W4.2 | Observability stack: deploy minimal Prometheus/Grafana for dogfood **or** hide Monitoring until configured | BOSS-344 | W4.1 | No fake Grafana promise |
| W4.3 | Agent Studio: seed demo agents/panels **or** first-run wizard | BOSS-347 | — | Not blank “not configured” |
| W4.4 | Improvement programs community path end-to-end (trial → promote → proposal if paid SIM) | BOSS-339 | W1.2 | One governed improvement demo |
| W4.5 | Assurance: seed one metric + case + verification run for dogfood proof | (337 child) | — | Assurance not empty shell |
| W4.6 | Earned autonomy / value scheduling (G1–G3) | BOSS-348 | W3 | Claim order under budget tested |
| W4.7 | Comms: one dry-run channel policy + outbox path without real external send **or** hide | (337) | — | Not empty pretending full |

**Paid/external (do not block community DoD):** GAP-002 Account Connect, GAP-003 IdP/SCIM, GAP-004 live provider **production** writes, real Slack/email, GA GitHub protection — track separately; fail-closed is correct until owner enables.

---

## W5 — QA gates & Plane hygiene

| ID | Work | Exit |
|---|---|---|
| W5.1 | `scripts/qa/live/` (cheers-style): health, auth, providers catalog, budget, deadletter count, area crawl, console errors | `make qa:live` against goldeneye |
| W5.2 | Deploy gate: after boss-app image bump, run live suite; fail deploy on P0 checks | Documented in GOLDENEYE_DEPLOYMENT |
| W5.3 | Plane: every PR links BOSS-IDs; move issues Todo→In Progress→Done only with live evidence comments | No “Done” without dogfood proof |
| W5.4 | Weekly signal-poll: DLQ + reliability incidents + api 5xx → worklist before invented backlog | Process |

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

## Suggested execution calendar (aggressive dogfood)

| Week | Focus | Plane exits |
|---|---|---|
| **0** | Merge + deploy community keys; live smoke | 350→Done (keys path) |
| **1** | W1 honesty (338, 339, 344, 346) + W2.1 worker image | 338 partial, 342 in progress |
| **1–2** | W2.2 sandbox host, W2.3 previews, W2.4 DLQ | 340, 341 Done |
| **2–3** | W3.1 glue + W3.3 GitHub wire + W3.2 E2E | 345, 343, 351 Done |
| **3–4** | W4 Studio/Assurance/SIM polish + W5 live gate | 347, 349; 337→Done when P0/P1 closed |

Adjust if sandbox host or model credits block factory reliability.

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
