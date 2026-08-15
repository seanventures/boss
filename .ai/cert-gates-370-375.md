# Cert — BOSS-370 (feature sweep) / BOSS-375 (factory e2e gate)

**Probed:** 2026-08-15T13:54Z–14:00Z  
**API:** `https://boss-api.local.sean.network`  
**Project:** `boss-dogfood`  
**Auth:** `X-Boss-Api-Key` from goldeneye host env, copied to a local `0600` file (never printed).  
**Health:** `GET /health` → 200, `status=ok`, `database_connected=true`, `github_configured=true`, `openai_configured=true`.  
**Repo HEAD / live shared tag:** `3af9c64` (`feat(ops): five remaining operator slices`). api + worker + web all on that tag.  
**Plane:** workspace `goldeneye`, project `cc97d5cc-e8c6-4893-ac14-7f646c37f3d7`. Both issues **In Progress** (`3b0d09f0-…`) at probe start. Evidence comments posted; state **not** moved: 370 `86c9f226-8164-4275-a756-eb29b0893a32`, 375 `2612a2fb-fcfd-4cbd-aa41-de7f2df4a70e`.  
**FEATURES / GA:** ◐. **Not GA.** No official POST. No factory create. No spend.

Ops `GET $OPS_BASE_URL/secret?key=…` is not reachable from this workstation (`ops.local.sean.network` → Traefik 404). Plane key came from the existing local key file. `BOSS_API_KEY` came from the live host `.env` via SSH (value never printed, never written into reports).

| Flag | This session |
|---|---|
| `BOSS_FACTORY_ALLOW_CREATE` | **unset** (no create) |
| `BOSS_QA_ALLOW_CANARIES` | **unset** (no live PR) |
| `BOSS_QA_ALLOW_OFFICIAL` | **unset** (no official /reports POST) |

---

## Plane

| Ticket | UUID | Title | State at start |
|---|---|---|---|
| [BOSS-370](https://plane.local.sean.network/goldeneye/projects/cc97d5cc-e8c6-4893-ac14-7f646c37f3d7/issues/a634c36e-323c-488f-a3d9-0aab041bc13a) | `a634c36e-323c-488f-a3d9-0aab041bc13a` | QA harness feature sweep: Community operator settings+features catalog | In Progress |
| [BOSS-375](https://plane.local.sean.network/goldeneye/projects/cc97d5cc-e8c6-4893-ac14-7f646c37f3d7/issues/91871479-be95-46ea-9b54-e9b4f45e8101) | `91871479-be95-46ea-9b54-e9b4f45e8101` | Expand factory E2E gate: quality loop + scorecard v1.1 UI/UX (no spend) | In Progress |

Prior close-matrix (same day) held both for **live-tag-lag** (`b6d8ccc`). That lag is gone: live is now `3af9c64`.

---

## 1. Unit tests

Command (as specified):

```bash
PYTHONPATH=.:engine .venv/bin/python -m pytest \
  scripts/tests/test_feature_sweep.py \
  scripts/tests/test_factory_e2e_gate.py -q --noconftest
```

| Suite | Result |
|---|---|
| `scripts/tests/test_feature_sweep.py` | included |
| `scripts/tests/test_factory_e2e_gate.py` | included |
| **Total** | **64 passed, 0 failed** in 1.66s |

No network. No invented pass.

---

## 2. Factory dry-run (BOSS-375)

### 2a. Specified command — **FAIL** (required)

```bash
python3 scripts/qa/factory_e2e_gate.py --dry-run
```

System `python3` is Xcode 3.9. `boss_engine` is not on `sys.path`.

| Check | Required | Result |
|---|---|---|
| `structure.preview_operator_urls` | yes | **FAIL** `loopback still operator-facing: {'imported': False, 'safe': False}` |
| All other required structure checks | yes | PASS |
| **Required** | | **23/24** — overall **FAIL**, exit 1 |

Report: `artifacts/qa/factory-e2e-gate-20260815T135426Z.json`

This is fail-closed on ImportError, **not** a measured loopback leak. The check refuses to invent `safe=True` when `publicize_preview` cannot be imported.

### 2b. Same script with the test/runtime path — **PASS**

```bash
PYTHONPATH=.:engine .venv/bin/python scripts/qa/factory_e2e_gate.py --dry-run
```

| | |
|---|---|
| Mode | `dry-run` |
| Required | **24/24** |
| Optional | process_compiler + process_manager present (not skipped) |
| `structure.preview_operator_urls` | PASS — `publicize_preview` never hands loopback as View |
| `structure.official_reports_honesty` | PASS |
| `structure.scorecard_v11_ui_ux` | PASS version=1.1, ui+ux present |
| `structure.no_budgeted_code_task` | PASS (create stays opt-in) |
| Exit | **0** |

Report: `artifacts/qa/factory-e2e-gate-20260815T135518Z.json`

`make qa-factory-e2e DRY_RUN=1` uses `PYTHON_BOOTSTRAP ?= python3` with no `PYTHONPATH=.:engine`, so it would reproduce 2a, not 2b.

---

## 3. Live feature sweep (BOSS-370)

Command (GET / safe-POST only; canary-mode=catalog; no launch):

```bash
PYTHONPATH=.:engine .venv/bin/python scripts/qa/harness.py \
  --phases features --canary-mode catalog \
  --output artifacts/qa/feature-sweep-370-live.json
```

| | |
|---|---|
| Gate | `boss_qa_harness` v1.0.0 |
| Window | 2026-08-15T14:00:19Z–14:00:45Z |
| Total checks | 72 |
| **Required** | **41/41 PASS** (`failed=[]`) |
| Optional pass | 31 |
| Optional fail | **0** |
| Honest skip | 1 (`community.skills-export` GET `/v1/skills/deployment.health.check/export` → **404 skip**, not an invented pass) |
| Exit | **0** |
| Key in report | **false** |

Required Community paid-prefix honesty (404 = PASS, 500 would FAIL):

| Probe | HTTP | Verdict |
|---|---|---|
| `paid.account-connect` | 404 | PASS (edition honesty) |
| `paid.marketplace` | 404 | PASS |
| `paid.self-improvement` | 404 | PASS |
| `paid.enterprise-clusters` | 404 | PASS |

Live workforce add-ons (required=false; 404 would be skip): all **200**, including `process-manager`.

`factory.previews` → 200, 50 items; sweep did **not** fail the BOSS-341 loopback rule (no operator `url`/`open_url` was 127.0.0.1/localhost).

Safe POSTs only: `POST /v1/deadletters/classify` 200; `POST /v1/diag/sandbox-echo` 200. No code-task create, no fixture POST, no easy-connect POST, no official run, no Metatron drive.

Report: `artifacts/qa/feature-sweep-370-live.json`

---

## 4. Live factory gate readonly (BOSS-375, extra evidence)

No `--create-fixture`, no `--create-code-task`, no `--live-pr`.

```bash
PYTHONPATH=.:engine .venv/bin/python scripts/qa/factory_e2e_gate.py \
  --output artifacts/qa/factory-e2e-375-live.json
```

| | |
|---|---|
| Mode | `live+readonly` |
| **Required** | **18/18 PASS** |
| Optional failed | **0** |
| Honest skips | `code_tasks.create_fixture` (flag off), `factory.proposals_queue` GET **405**, `factory.live_pr` (flag off / `BOSS_QA_ALLOW_CANARIES` unset) |
| Duration | 2748 ms |
| Exit | **0** |
| Key in report | **false** |

Required live HTTP:

| Check | HTTP | Notes |
|---|---|---|
| `health` | 200 | `status=ok` |
| `factory.wip` | 200 | read-only |
| `code_tasks.list` | 200 | count=20 |

Optional honesty (not a sealed weekly proof): `factory.sealed_slice_honesty` reports existing receipts `live_pr=39/40` and last simulated `crun_d9c77df4b3c540f7adc5`. That is **not** a BOSS-351 sealed receipt+hold.

Report: `artifacts/qa/factory-e2e-375-live.json`

---

## Required failures

| Where | Check | Required? | Honest meaning |
|---|---|---|---|
| Specified `python3 … --dry-run` | `structure.preview_operator_urls` | yes | ImportError (`imported=False`). Fail-closed. Not a live loopback measurement. |
| Engine-aware dry-run | — | — | none |
| Live feature sweep | — | — | none |
| Live factory readonly | — | — | none |

Optional live: no failures. One feature-sweep skip (skills export 404). Three factory skips (create fixture / GET proposals 405 / live PR flag off).

---

## CLOSE recommendation

Rule used: close 370/375 only if dry-run is green **and** live required probes pass or honest-skip. Never invent a pass. Do not stamp GA.

| Ticket | CLOSE? | Why |
|---|---|---|
| **BOSS-370** | **YES — close** | Unit tests green. Live required probes **41/41 PASS** on current shared tag `3af9c64`. One optional 404 is an honest skip. Prior hold (live tag `b6d8ccc`) is gone. |
| **BOSS-375** | **YES — close** | Unit tests green. Engine-aware dry-run **24/24**. Live required probes **18/18**. No spend, no create. Quality-loop / scorecard v1.1 / preview-url / official-reports honesty all measured PASS. |

**Do not treat the specified bare `python3 --dry-run` as a product red.** It is an invocation residual: system Python 3.9 cannot import `boss_engine`, and the check fail-closes instead of inventing `safe`. The same check PASSes under `PYTHONPATH=.:engine` (how the unit tests and live gate run) and PASSes on live. Residual for ops/docs: `make qa-factory-e2e DRY_RUN=1` should set `PYTHONPATH=.:engine` (or use `$(PYTHON)`). That is not a reason to keep 375 In Progress.

**Still not closed / not claimed by this cert**

- BOSS-351 sealed weekly receipt+hold — not this gate; factory create flag still off.
- BOSS-381 official `/reports` POST — `BOSS_QA_ALLOW_OFFICIAL` unset.
- Public GA / FEATURES freeze — not stamped.

---

## What this is not

- Not a 14-day `qa-live` streak day.
- Not a sealed factory E2E.
- Not an official BugBox/HookWatch/ApproveIt score.
- Not Community public GA.
