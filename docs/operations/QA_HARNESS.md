# Unified BOSS QA harness

One operator command for the Community vertical slice: control-plane
preflight, a settings/features sweep, a no-spend command→task→budget→jobs
read, and the three pinned canary apps (BugBox, HookWatch, ApproveIt).

**Script:** `scripts/qa/harness.py`  
**Make:** `make qa-harness`  
**DoD map:** `scripts/qa/ga_dod_checklist.md`  
**Not this gate:** the 14-day `qa-live` calendar
(`docs/operations/QA_LIVE_14DAY.md`) stays a separate daily control-plane
proof. Do not treat a harness pass as a 14-day streak day.

Talks only to the BOSS control-plane HTTP API. Never prints `BOSS_API_KEY`.
Never calls models, GitHub, or sandboxes directly.

**Live dogfood (2026-08-13, api `3e6c906`):** `CANARY_MODE=catalog` **55/55 required PASS**. Optional 500s on elastic / worker-autoscaling / cloud status are the known missing-schema residual (not required). Catalog `built.run_id` for BugBox/ApproveIt is **stale** (report 404) — reuse records `built_stale` and does not fail the gate. Real `launch` of the three apps still needs `BOSS_QA_ALLOW_CANARIES=1` plus a worker, model lane, and sandbox.

---

## How to run

### Structure only (no secrets, no network)

```bash
make qa-harness DRY_RUN=1
# or
python3 scripts/qa/harness.py --dry-run
```

Dry-run prints the feature catalog (44 probes) and the three canary
contracts (BugBox, HookWatch, ApproveIt). No network, no secrets.

### Against local BOSS

```bash
export BOSS_API_URL=http://127.0.0.1:8000
export BOSS_PROJECT_ID=boss-dogfood
export BOSS_API_KEY=…          # never commit
make qa-harness                # default CANARY_MODE=catalog
```

### Against goldeneye dogfood

```bash
export BOSS_API_BASE=https://boss-api.local.sean.network
export PROJECT_ID=boss-dogfood
export BOSS_API_KEY=…          # never commit
make qa-harness
make qa-harness CANARY_MODE=reuse
make qa-harness CANARY_MODE=fixture
```

Optional report path: `BOSS_HARNESS_OUTPUT=artifacts/qa/harness-<utc>.json`.

Env aliases:

- `BOSS_API_BASE` or `BOSS_API_URL` (default `https://boss-api.local.sean.network`)
- `PROJECT_ID` or `BOSS_PROJECT_ID` (default `boss-dogfood`)
- `BOSS_API_KEY` (required for live; never echoed by Make)
- `CANARY_MODE` (`catalog` | `reuse` | `fixture` | `launch`)

---

## Phases

| Phase | What it proves | Spend? |
|-------|----------------|--------|
| `preflight` | `/health`, unauth fail-closed, edition, auth projects | no |
| `features` | GET / safe-POST sweep of operator settings and features | no |
| `control` | tasks, jobs, events, decisions inbox, deadletters, factory WIP, budget status | no |
| `canaries` | BugBox / HookWatch / ApproveIt (mode below) | only `launch` |

Select with `--phases preflight,features,control,canaries`.

---

## Canary modes

| Mode | Behaviour | Allow? |
|------|-----------|--------|
| `catalog` (default) | `GET /v1/qa/benchmark-apps`; assert the three contracts exist | none |
| `reuse` | catalog + `already_built` reports if present | none |
| `fixture` | `POST simulated=true` via `route=direct` (no LLM, no PR) | API key |
| `launch` | real governed builds through the budgeted code-task path | `BOSS_QA_ALLOW_CANARIES=1` |

`launch` without `BOSS_QA_ALLOW_CANARIES=1` exits **2** and does not POST.
Do not export that flag unless an operator has approved spend.

```bash
# Explicit spend — operator only
export BOSS_QA_ALLOW_CANARIES=1
make qa-harness CANARY_MODE=launch
```

---

## What "passed" means

**Harness pass (exit 0):** every *required* check is `ok`. Optional failures
do not fail the gate. Missing sibling modules on dry-run are skips, not reds.

**Canary pass is mechanical evidence, never model prose.**

A canary is green only when the durable report shows the pinned
`test_commands` (`npm run verify`) actually ran and passed:

- `already_built` / reuse: existing report with verify evidence
- fixture: simulated persist, `simulated=true`, zero cost, no job
- launch: leased code-task + Receipt + `npm run verify` in the evidence
  bundle

Agent/chat summary text is **not** a pass. "Looks good" in a model log is
**not** a pass. A queued or `already_built` row without verify evidence is
**not** a pass.

Do not invent live canary success. If verify did not run, the check is
fail or skip — never a silent green.

### No-false-reds

- 404/405 on a documented POST-only or not-mounted Community route is
  **SKIP**, not required FAIL.
- Unadvertised fixture support must not POST (old APIs would ignore
  `simulated=true` and enqueue a real job).
- Optional feature probes that 404 stay optional.
- Dry-run never talks to the network.
- `launch` is fail-closed without the allow flag.

---

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | all required checks passed (or dry-run structure ok) |
| 1 | one or more required checks failed |
| 2 | usage / missing `BOSS_API_KEY` / `launch` without allow |

---

## Relation to other gates

| Gate | Command | Role |
|------|---------|------|
| Workflow slice | `make qa-workflow-e2e` | CEO command → task → budget → jobs |
| Factory surface | `make qa-factory-e2e` | WIP + code-tasks list / fixture |
| Live dogfood | `make qa-live` | Broader control-plane (10 required + Metatron) |
| **Unified harness** | `make qa-harness` | Settings/features + 3 canaries on top of the above |
| 14-day calendar | cron on goldeneye | Consecutive daily `qa-live` — **separate** |

Closing Community GA still requires the 14-day `qa-live` streak in
`docs/operations/QA_LIVE_14DAY.md`. This harness does not replace that
calendar and must not be backfilled into it.
