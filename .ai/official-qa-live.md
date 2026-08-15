# Official QA /reports — live evidence (BOSS-381)

**Probed:** 2026-08-15T12:53Z–12:55Z  
**API:** `https://boss-api.local.sean.network`  
**Project:** `boss-dogfood`  
**Auth:** `X-Boss-Api-Key` from local key file (never printed).  
**Health:** `GET /health` → 200, `status=ok`, `database_connected=true`.  
**Plane:** [BOSS-381](https://plane.local.sean.network/goldeneye/projects/cc97d5cc-e8c6-4893-ac14-7f646c37f3d7/issues/79f85c21-40e7-45ff-8751-c9cedf41c2a1) remains **In Progress**. Comment `fa14b433-627d-44b6-8811-48c872917fe1`.  
**FEATURES / GA:** ◐. Not GA. No invented scores. No official POST.

## Spend gate

| Flag | This session |
|---|---|
| `BOSS_QA_ALLOW_OFFICIAL` | **unset** |

That is the **named blocker** for new official scores. `scripts/qa/official_reports.py` will not POST spendy official runs unless the flag is already `1`. This probe did **not** POST `/v1/qa/benchmark-apps/{id}/runs`.

## How `/report` is supposed to stay honest

Repo (`engine/boss_engine/services/benchmark_apps.py`):

- `completion_report` renders only observed evidence. An absent verify command is `not_recorded`, never a pass.
- `benchmark_scorecard` writes `status=not_scored` + `score=None` + reason when UI/UX/capability (and other) evidence is missing.
- `report_honesty` treats `status=scored` + `score=None` as an **invented** score (`honest=false`). `not_scored` is missing evidence, not a score.

Live `/report` today returns scorecard v1.1 dimensions but **does not emit the `honesty` blob** (`completion_report` keys on live: `benchmark_id`, `benchmark_name`, `completion_contract`, `message`, `passed`, `required_verification_commands`, `results`, `run_id`, `run_status`, `scorecard`, `simulated`). Honesty below is computed from dimensions the same way `scripts/qa/official_reports.py` `_honesty_from_report` does when the blob is absent.

## Catalog vs advertised built

`GET /v1/qa/benchmark-apps` without `project_id` → **200**, three contracts (`bugbox`, `hookwatch`, `approveit`). No `built` field.

`GET /v1/qa/benchmark-apps?project_id=boss-dogfood` → **200**. Built is only attached when the match is `benchmark_id` or `launched_as_benchmark`.

| App | Catalog `built.run_id` | Catalog match | Reportable? | `GET .../runs/{id}/report` | ui | ux | Invented null scores |
|---|---|---|---|---|---|---|---|
| BugBox | **missing** | `related` only: `crun_4c762352803e4487a3b4` (`matched_on=name`) | no | **missing** (404 `code task is not a QA benchmark run`) | n/a — no catalog report | n/a | none (no report) |
| HookWatch | `crun_911888eefbd14166bce6` | `launched_as_benchmark` | catalog says **true** | **missing** (404 `code task is not a QA benchmark run`) | n/a — advertised report missing | n/a | none (no report) |
| ApproveIt | **missing** | `related` only: `crun_1adb66027cc548d5a047` (`matched_on=name`) | no | **missing** (404 `code task is not a QA benchmark run`) | n/a — no catalog report | n/a | none (no report) |

`scripts/qa/official_reports.py --mode inspect` (no `project_id` query) exited **0**:

- `official.catalog` → 200
- `official.{bugbox,hookwatch,approveit}.no_report` → `no built.run_id — skip, not an invented score`

That inspect path never sees the HookWatch stale `built` pointer. The pointer is still not a score: the `/report` is **missing**.

## Existing QA-benchmark `/report`s (not catalog-built)

Found by listing `/v1/code-tasks` + `/v1/qa/benchmark-apps/evaluations` and GETting each `runs/{id}/report`. Dummy `does-not-exist` → 404 `code task not found` (route exists).

Every live report that exists: **`passed=false`**, **`simulated=false`**, **ui = `not_scored`**, **ux = `not_scored`**, **capability = `not_scored`**, **invented_scores = []**.

| App | run_id | run_status | `/report` | ui | ux | capability | Other scored (numeric, not invented) | overall | invented null scores |
|---|---|---|---|---|---|---|---|---|---|
| BugBox | `crun_9acc1e5f44e0464099be` | failed | **exists** 200 | **not_scored** | **not_scored** | not_scored | quality=100, speed=24, cost=25, tokens=1 | 50.0 / weight 30 | **none** |
| BugBox | `crun_27b5a74441fc4c0d8ace` | blocked | **exists** 200 | **not_scored** | **not_scored** | not_scored | quality=60, speed=24, cost=29, tokens=1 | 34.8 / weight 30 | **none** |
| BugBox | `crun_7c11008dc2b74d1ba30e` | failed | **exists** 200 | **not_scored** | **not_scored** | not_scored | none | null / weight 0 | **none** |
| BugBox | `crun_2f9b97e4b9bb41f9bd0c` | cancelled | **exists** 200 | **not_scored** | **not_scored** | not_scored | none | null / weight 0 | **none** |
| HookWatch | `crun_437689e8fdcc45adb395` | failed | **exists** 200 | **not_scored** | **not_scored** | not_scored | none | null / weight 0 | **none** |
| HookWatch | `crun_08edd63875b64d1694b9` | cancelled | **exists** 200 | **not_scored** | **not_scored** | not_scored | none | null / weight 0 | **none** |
| HookWatch | `crun_4cd5b9929c42456dbe8e` | cancelled | **exists** 200 | **not_scored** | **not_scored** | not_scored | none | null / weight 0 | **none** |
| HookWatch | `crun_cfb94b67ba5d40ce99f3` | cancelled | **exists** 200 | **not_scored** | **not_scored** | not_scored | none | null / weight 0 | **none** |
| ApproveIt | `crun_907e7ca9e6f54ba6945f` | completed | **exists** 200 | **not_scored** | **not_scored** | not_scored | quality=100, speed=27, cost=33, tokens=1 | 52.2 / weight 30 | **none** |
| ApproveIt | `crun_48797c9b36ce435499de` | failed | **exists** 200 | **not_scored** | **not_scored** | not_scored | none | null / weight 0 | **none** |

UI reason (all existing reports): `UI craft evidence (a11y_violations, states_covered, contrast_ok) was not attached.`  
UX reason (all existing reports): `UX evidence (JTBD walkthroughs and/or rubric 1–5) was not attached.`

`not_scored` stays `not_scored`. Null next to `not_scored` is **not** an invented score. No dimension was `status=scored` with `score=null`.

No existing report is a certified pass. ApproveIt’s only completed QA-benchmark run is still `passed=false` (verify / plan evidence incomplete).

## Evaluations

`GET /v1/qa/benchmark-apps/evaluations?project_id=boss-dogfood` → 200, 12 snapshots (scorecard v1.1). They point at the same run ids above. Latest rows still list `ui` and `ux` in `missing_evidence` when a recommendation is present. Older snapshots on the same run can differ in `observed_weight` (30 vs 34); live `/report` is the authority and currently has ui/ux `not_scored`.

## What this is not

- Not new official BugBox / HookWatch / ApproveIt scores.
- Not a certified 5/5 or GA stamp.
- Not a claim that catalog `built` implies a `/report`. HookWatch proves the opposite.

## Residual

1. **`BOSS_QA_ALLOW_OFFICIAL` unset** — cannot POST official runs.
2. HookWatch catalog `built` (`crun_911888eefbd14166bce6`, PRD title, `prev_6d788e954beb445f9b94`) is **not** a QA-benchmark run; `/report` missing.
3. Live `/report` omits `honesty` even though repo `completion_report` attaches it.
4. Inspect script does not pass `project_id`, so it cannot see catalog `built` / stale pointers.
5. No live official run has UI/UX evidence attached.
