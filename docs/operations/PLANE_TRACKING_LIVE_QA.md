# Plane tracking — live QA + GA path

**Workspace:** `goldeneye`  
**Project:** BOSS (`cc97d5cc-e8c6-4893-ac14-7f646c37f3d7`)  
**UI:** https://plane.local.sean.network  
**Contract:** `AGENTS.md` — Plane is the system of record; dedupe before create; never commit API keys.  
**Living plan (update first):** [`GA_LIVING_PLAN.md`](GA_LIVING_PLAN.md)  
**Last refresh:** 2026-08-12 (open-issue honesty pass + post GA 15-panel assessment)

## Umbrella

| Issue | Title | State |
|---|---|---|
| **BOSS-337** | Deep agentic production QA + GA readiness | **In Progress** |

Evidence:

- `docs/operations/ga-readiness-2026-08-12/GA_READINESS_SCORECARD.md`
- `docs/operations/GA_LIVING_PLAN.md`
- `docs/operations/LIVE_QA_2026-08-11.md` (historical)
- `docs/operations/LIVE_UI_FUNCTIONAL_AUDIT_2026-08-11.md`
- `docs/operations/PRD_CRITICAL_GAPS_2026-08-12.md`
- `.ai/live-qa-memory.md`

## Closed with live / merge proof (Done)

| Issue | Proof summary |
|---|---|
| **BOSS-350** | Community BYOK + full ProviderOps kernel (#157); live providers/changesets 200 |
| **BOSS-338** | Edition-aware MC / kernel UI (Community SSO kept; Enterprise/design-system primary nav residual closed) |
| **BOSS-339** | SIM / improvement-programs honesty on community |
| **BOSS-340** | Sandbox host.connected=true (rootfs still a follow-on) |
| **BOSS-341** | Preview publicize + live list contract (absolute `open_url`); View HTML still Phase B |
| **BOSS-342** | Worker Dockerfile git/tools + contract tests (live shared-tag still Phase A) |
| **BOSS-343** | GitHub MC wired: connections + bindings + easy-connect (#158) |
| **BOSS-344** | CI/CD + Monitoring empty-state honesty |
| **BOSS-345** | next_move orchestration glue |
| **BOSS-349** | OpenAPI 0.11.0 + 79-route parity (#161) |
| **BOSS-351** | `live_dogfood_gate` 10/10 PASS (factory reliability still open) |

## Still open (must track)

| Issue | Status | Next proof |
|---|---|---|
| **BOSS-337** | In Progress | Phase A–C; close only when GA living plan Phase C exits |
| **BOSS-346** | Open | Residual hydration #418 crawl clean |
| **BOSS-347** | Open / In Progress | Redeploy seed route; catalog 13/3; one deterministic run |
| **BOSS-348** | Open | G1–G3 — not required for narrow Community GA claims |
| **BOSS-340 residual** | Open work | Guest rootfs / provision success |
| **BOSS-342 residual / BOSS-372** | In Progress | DLQ remediations list + fail-closed retry; remaining live classes TPM + missing DO creds |
| **BOSS-343 residual** | Open work | Factory uses primary binding or UI dual-plane honesty |
| **BOSS-351 residual** | Open work | Sealed weekly command→PR reliability |

## GA Phase A mapping (Plane)

| Phase A | Plane | State |
|---|---|---|
| Shared-tag redeploy | 337, 342 residual | Todo |
| `qa-live` on new tag | 351 residual | Todo |
| Studio seed | 347 | Todo |
| Rootfs / claim freeze | 340 residual | Todo |
| DLQ triage | 342 residual | Todo |
| Dual-plane honesty | 343 residual | Todo |
| Claims freeze | 337 | Drafted in scorecard |

## Related existing (not re-created)

| Issue | Note |
|---|---|
| BOSS-246–255 | GHX feature stack — dogfood connect now live |
| BOSS-320 | SCIM deprovision org-member fallback — Done (#131); C2 added focused 403 test 2026-08-13 |
| BOSS-324 | boss-sandbox main protection (external/plan) |
| BOSS-326 | PyJWT 2.13.0 / cryptography 50.0.0 — Done (#139); C2 re-scan 2026-08-13, no bump, no CI fail-gate |
| GAP-002/003/004 | Account Connect / IdP / prod provider writes — fail-closed |

## Agent rules for this workstream

1. **Update `GA_LIVING_PLAN.md` first** after any ship/deploy/proof.  
2. Before starting: search Plane; use BOSS-33x ids; no duplicate issues.  
3. Move **Todo → In Progress → Done** only with live dogfood evidence comments.  
4. Link PRs/commits; re-run `qa-live` after redeploys.  
5. Never print or commit `PLANE_API_KEY` / ops tokens.  
6. Stale docs (0 GitHub connections, ProviderOps 404 community) are **superseded** — trust living plan + latest live probes.

## Plane sync — 2026-08-12 multi-cloud + Goldeneye session

Automated + agent team Plane update (workspace `goldeneye`, BOSS project).

### Created & Done
- **BOSS-362** Goldeneye modular bare-metal PaaS kit (extractable OSS)
- **BOSS-363** Multi-cloud models+infra (AWS Bedrock, Azure, DO, OVH)
- **BOSS-364** Community one-line installer
- **BOSS-365** Cloud product distinction + Community Easy Connect
- **BOSS-366** Elastic fabric + provider usage ledger

### Closed with code evidence
- **BOSS-238** DO adapter · **BOSS-239** OVH · **BOSS-242** elastic PRV-07
- **BOSS-70** Azure adapters (GCP not shipped — refile if needed)
- **BOSS-359** multi work-tracker providers
- **BOSS-360/361** worker fabric · **BOSS-352/354** autonomy
- **BOSS-121** Plane webhooks

### Cancelled obsolete noise
- BOSS-1/2/3/4/12/16/17, BOSS-152/153

### Left open honestly (partial/roadmap)
GA/QA umbrellas (337, 314) remain **In Progress** until Phase A (shared-tag redeploy + `qa-live`) lands. Residual children: 346, 347, 324, rootfs/DLQ/factory honesty. Knowledge/Autonomy parents and PRV FinOps children may still be open.

### 2026-08-12 open-issue honesty pass (docs + Plane)

| Issue | Action | Evidence |
|---|---|---|
| BOSS-288–290 | **Done** | `docs/marketing/youtube/EP{1,2,3}_SCRIPT.md` — script package; recording outside repo |
| BOSS-291 | **Done** | `docs/product/ENGINE_EXTRACT_CHECKLIST.md` (actionable extract checklist) |
| BOSS-292/293 | Progress comments | Reference extract checklist; public flip blocked on BOSS-300 |
| BOSS-295 + 304 | **Done** | `docs/product/GTM_PRODUCT_GAP_RECONCILIATION.md` (+ AI SRE `/v1/oncall-sre`) |
| BOSS-300 | **Todo** | BLOCKED — human legal decision; do not Done |
| BOSS-302/303 | **Cancelled** | Phase-03 Cloud roadmap; not active this cycle |
| BOSS-97/98/101/102 | **Backlog** (kept) | Python worker fabric + concurrency governance + elastic cover Community; Go S1–S3 optional scale |
| BOSS-314 + 337 | **In Progress** | Progress comments only; Phase A live proof still required |

Reports: `/tmp/plane_session_sync_report.json`, `/tmp/plane_agent_*.json`  
Script: `scripts/plane_sync_session_work.py` · ops helper: `/tmp/plane_ops.py`
