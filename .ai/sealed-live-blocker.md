# BOSS-351 sealed factory E2E — live blocker

**Date:** 2026-08-15T12:53Z  
**Plane:** BOSS-351 `c1f3b9c6-923e-4b6e-b211-939934f908c5` (In Progress)  
**API:** `https://boss-api.local.sean.network`  
**Project:** `boss-dogfood`  
**Live tag:** `b6d8ccc` (shared api + web + worker)  
**Edition:** community (`GET /v1/edition?project_id=boss-dogfood` → 200)  
**Auth:** `X-Boss-Api-Key` as member `ceo` (`GET /v1/auth/session` → 200)  
**No spend this probe.** Flags were not enabled. No `POST /v1/code-tasks`. No sealed receipt invented.

## Verdict

**Blocked.** Factory allow-create is **OFF**. A sealed `simulated=false` + `open_pr=true` governed `code.task` was **not** started.

A reservation path exists (budget green, GitHub binding present, WIP admits), but using it would require turning spend/create flags on. Instructions forbid that.

**Sealed receipt + hold decision: does not exist** for this weekly dogfood run.

## Exact blocker

1. **`BOSS_FACTORY_ALLOW_CREATE` is unset (OFF)**
   - Probe process env: unset
   - Live host `/srv/apps/boss-app/.env`: key **absent**
   - Gate (`scripts/qa/factory_e2e_gate.py`) refuses a budgeted create unless this is `1`
2. **`BOSS_QA_ALLOW_CANARIES` is unset (OFF)**
   - Probe process env: unset
   - Live host `.env`: key **absent**
   - Gate fail-closes live PR (`open_pr`) without this flag
3. **Starting the sealed path would spend**
   - `simulated=false` + `open_pr=true` reserves budget, calls a model, and creates `github.pull_request.create`
   - User rule: do not spend unless live flags are already on; do not enable flags

Not the primary blocker (present and healthy):

- GitHub plane is bound (`github_plane=mission_control_binding`, `seanheiney/boss`)
- Budget state is green
- WIP `admit=true` (0 in flight / 5 slots)

## Live HTTP matrix (this probe)

| Method | Endpoint | HTTP | Notes |
|---|---|---|---|
| GET | `/health` | **200** | `status=ok`; DB/GitHub/OpenAI configured |
| GET | `/v1/auth/session` | **200** | `auth_required=true`; `auth_method=api_key`; principal `ceo` |
| GET | `/v1/edition?project_id=boss-dogfood` | **200** | `edition_id=community` |
| GET | `/v1/edition` | **422** | `project_id` required |
| GET | `/v1/factory/wip?project_id=boss-dogfood&wip_limit=5` | **200** | see WIP below |
| GET | `/v1/factory/wip?…` (no auth) | **401** | fail-closed |
| GET | `/v1/settings` | **404** | no such route |
| GET | `/v1/settings?project_id=boss-dogfood` | **404** | no such route |
| GET | `/v1/budget/status?scope_type=project&scope_id=boss-dogfood` | **200** | `budget_state=green` |
| GET | `/v1/budget/allocations?project_id=boss-dogfood` | **200** | `items=[]` (0 allocations) |
| GET | `/v1/budget/allocations?scope_type=project&scope_id=boss-dogfood` | **422** | needs `project_id` |
| GET | `/v1/code-tasks?project_id=boss-dogfood&limit=5` | **200** | 5 recent runs |
| GET | `/v1/github/connections?project_id=boss-dogfood` | **200** | 2 active |
| GET | `/v1/github/repository-bindings?project_id=boss-dogfood` | **200** | 2 active |
| GET | `/v1/secret-refs?project_id=boss-dogfood` | **200** | 7 refs (metadata only) |
| GET | `/v1/providers?project_id=boss-dogfood` | **200** | 4 providers |
| GET | `/v1/decisions/inbox?project_id=boss-dogfood&limit=10` | **200** | 92 inbox items |
| GET | `/v1/receipts?project_id=boss-dogfood&limit=10` | **200** | latest are ticks, not PR creates |
| GET | `/v1/jobs?project_id=boss-dogfood&limit=5` | **200** | |
| GET | `/v1/deadletters?project_id=boss-dogfood&limit=5` | **200** | |
| GET | `/v1/diag/host-capabilities` | **200** | |
| GET | `/openapi.json` | **200** | live `info.version=0.2.1` (stale vs main) |
| GET | `/v1/meta` | **404** | |
| GET | `/v1/version` | **404** | |

"Settings" on this API is not `/v1/settings`. The settings surface used here is edition + budget + GitHub + secret-refs + providers.

## Factory WIP (200)

| Field | Live value |
|---|---|
| `admit` | `true` (`under_wip_limit`) |
| `in_flight` / `queued` / `available_slots` | 0 / 0 / 5 |
| `fixture_supported` | `true` (simulated fixture only; **not sealed**) |
| `coding_substrate` | `local` (configured `microvm`; `microvm_claims_frozen=true`) |
| `github_plane` | `mission_control_binding` |
| `github_full_name` | `seanheiney/boss` |
| `github_binding_id` | `ghrepo_4527fba4955d4a499cfc` |
| `last_simulated_fixture` | `crun_d9c77df4b3c540f7adc5` (2026-08-13; owner `_fixture`; **not sealed**) |
| `live_pr_success` | `39/40 from existing receipts (not a sealed weekly proof)` |
| `live_pr_success.proven` | **false** |

## Budget (200)

- `budget_state=green`
- cap `129000000` micros (~$129); available `89772838` (~$89.77); reserved `0`
- `active_reservations_count=0`
- `tier2_frozen=false`
- allocations list: **empty**
- top drivers include historical `github.pull_request.create` (not a sealed weekly proof)

Reservation APIs exist. This probe did **not** `POST /v1/budget/reserve`.

## Sealed contract vs live objects

Contract: `engine/boss_engine/services/sealed_e2e_contract.py`

Required for a sealed receipt:

- `effect_kind=github.pull_request.create`
- `status=succeeded`
- `dedupe_key`
- `result.decision_id` + `result.pull_request_url` + `result.receipt_id`
- `result.ui_evidence` / `ux_evidence` / `capability_evidence` keys present (empty `{}` is honest)
- at least one of `quality_gate_id` / `artifact_id` / `test_report_artifact_id`
- `simulated` must not be true
- `proven_live` stays false until a weekly dogfood run

Required for a sealed hold decision:

- `default_action=hold`
- `status` in `{pending, blocked}`
- `recommendation.action=review_pull_request`
- `reason=merge_decision_required`
- `recommendation.receipt_id`
- evidence keys on the recommendation
- `no_auto_merge` must not be false

Live check:

- Latest 50 receipts are `process_manager.tick` / `metatron.watch` / `metatron.overwatch` — **zero** `github.pull_request.create` in that window. List endpoint does not filter by `effect_kind`.
- Inbox has **39** `review_pull_request` decisions. Sampled holds have `reason=null` (not `merge_decision_required`), no `ui_evidence`, `no_auto_merge=null`. Statuses are `expired` / `approved` / `rejected`, not a current pending hold for this run.
- Only pending inbox item this probe: `dec_ff108b28e3a340a8868b` action `create_budget_reservation` — **not** a PR hold.
- Factory honesty already labels historical PR receipts as **not a sealed weekly proof**.

**No `receipt_id` + hold `decision_id` pair satisfies `validate_sealed_receipt` + `validate_sealed_decision` for a new live run.**

## Host flags (names only; values not dumped)

From goldeneye `/srv/apps/boss-app/.env` key names:

| Key | Present | Truthy |
|---|---|---|
| `BOSS_FACTORY_ALLOW_CREATE` | **absent** | off |
| `BOSS_QA_ALLOW_CANARIES` | **absent** | off |
| `BOSS_REQUIRE_API_AUTH` | yes | true |
| `BOSS_MICROVM_CLAIMS_FROZEN` | yes | true |
| `BOSS_INFRA_REAL_PROVISION_ALLOWED` | yes | **false** |
| `BOSS_OPEN_PR_AS_DRAFT` | yes | false |
| `BOSS_EDITION` | yes | community (len=9) |
| `BOSS_GITHUB_OWNER` / `BOSS_GITHUB_REPO` | yes | set |
| GitHub token key | yes | present (not printed) |

Images:

- `boss-api:b6d8ccc`
- `boss-web:b6d8ccc`
- `boss-worker:b6d8ccc`

## What would unblock (operator only — not done)

1. Explicitly set `BOSS_FACTORY_ALLOW_CREATE=1` **and** `BOSS_QA_ALLOW_CANARIES=1` for one dogfood canary (or an equivalent written spend approval).
2. Then start **one** narrow `POST /v1/code-tasks` with `simulated=false`, `open_pr=true`, no merge.
3. Wait for `github.pull_request.create` receipt + hold decision with evidence keys.
4. Record ids in `.ai/sealed-live-evidence.md`. Never invent scores or a receipt.

Until then BOSS-351 sealed weekly E2E stays **unproven**. Do not stamp GA.
