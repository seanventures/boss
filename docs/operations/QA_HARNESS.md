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

**Live dogfood (2026-08-13, api `3e6c906`):** `CANARY_MODE=catalog` **55/55 required PASS** on the original catalog. Feature-sweep Community add-ons after that pass are **optional** (`required=false`) so a 404 on an older live tag is not a false red. Optional 500s on elastic / worker-autoscaling / cloud status are the known missing-schema residual (not required). Catalog `built.run_id` for BugBox/ApproveIt is **stale** (report 404) — reuse records `built_stale` and does not fail the gate. Real `launch` of the three apps still needs `BOSS_QA_ALLOW_CANARIES=1` plus a worker, model lane, and sandbox.

---

## How to run

### Structure only (no secrets, no network)

```bash
make qa-harness DRY_RUN=1
# or
python3 scripts/qa/harness.py --dry-run
```

Dry-run prints the feature catalog (63 probes: original required
settings/features plus optional Community add-ons) and the three canary
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

### Feature catalog (phase `features`)

`scripts/qa/feature_sweep.py` is a curated operator surface, not every
mounted route. `GET /v1/edition` always sends `project_id`.

| Class | Count (dry-run) | Gate |
|-------|-----------------|------|
| Required settings/features | 41 (includes `resource-receipts`) | 2xx (paid 2xx/404) |
| Optional schema extras | 4 (token-efficiency, elastic, worker-autoscaling, cloud status) | 2xx or 503 WARN |
| Optional Community add-ons | 18 | 2xx, or 404/405 on older tags |
| **Catalog total** | **63** | optional fails do not fail the gate |

Community add-ons (all `required=false`):

| Probe | Method / path | Honesty |
|-------|---------------|---------|
| deadletter remediations | `GET /v1/deadletters/remediations` | 404 old image ok |
| sandbox-echo | `POST /v1/diag/sandbox-echo` | 422 frozen / 503 no host / 404 old image ok |
| skills list + export | `GET /v1/skills`, `GET /v1/skills/{id}/export` | 404 ok |
| skills import | `GET /v1/skills/import` | 405 POST-only mounted |
| meetings standup / simulated-run | `GET` those POST-only paths | 405/404 ok; never creates a meeting |
| assurance seed-demo | `GET /v1/assurance/seed-demo` | 405/404 ok; never seeds |
| packs list + export | `GET /v1/packs`, `GET /v1/packs/community_edition/export` | 404 ok |
| packs import | `GET /v1/packs/import` | 405/404 ok |
| preview fixture | `GET /v1/previews/fixture` | 405/404 ok; never POSTs the fixture |
| comms outbox + dry-run | `GET /v1/comms/outbox`, `GET …/dry-run` | 405/404 ok; never delivers |
| easy-connect | `GET` providers / github / cloud easy-connect | 405/404 ok; never POSTs a secret |
| local openai-compatible | `GET …/sources/approved_local_model_host/openai-compatible` | 405/404 ok; never writes a source |

The original live **55/55** figure is harness *required* checks
(preflight + required features + control + 3 catalog canaries), not the
optional Community add-ons. Do not treat an optional 404 as a red.

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
- Community add-ons after the original 55-check catalog are
  `required=false`. That includes remediations, skills/packs
  import/export, standup / simulated-run, assurance seed-demo, preview
  fixture, comms dry-run, easy-connect, and local openai-compatible
  source. Presence is proven with GET (405 = POST-only mounted).
- `POST /v1/diag/sandbox-echo` is freeze-honest: 422 frozen or 503 no
  host is **not** a required fail. The sweep never POSTs easy-connect,
  skill/pack import, standup create, preview fixture, or comms deliver.

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
| **System stack** | `make qa-system` | All of the above, no spend, one JSON report |
| 14-day calendar | cron on goldeneye | Consecutive daily `qa-live` — **separate** |

Closing Community GA still requires the 14-day `qa-live` streak in
`docs/operations/QA_LIVE_14DAY.md`. This harness does not replace that
calendar and must not be backfilled into it.

---

## System gate (`make qa-system`)

One command for the whole Community **no-spend** stack. Orchestrates the
existing gates as subprocesses, redacts secrets, and writes a combined
JSON report.

**Script:** `scripts/qa/system_gate.py`

| Step | When | Spend? |
|------|------|--------|
| `harness.py --dry-run` | always | no |
| `harness.py` catalog | `BOSS_API_KEY` set | no |
| `live_dogfood_gate.py` | `BOSS_API_KEY` set | no |
| `workflow_e2e_gate.py` | dry-run without key, live with key | no |
| `factory_e2e_gate.py --dry-run` | always | no |
| factory live `--create-fixture` | key set **and** `fixture_supported` | no |
| preview `POST /v1/previews/fixture` + `GET .../web/` | `QA_PREVIEW_FIXTURE=1` + key | no |

```bash
make qa-system DRY_RUN=1
# or
python3 scripts/qa/system_gate.py --dry-run

export BOSS_API_KEY=…          # never commit
make qa-system
QA_PREVIEW_FIXTURE=1 make qa-system
```

Optional report path: `BOSS_SYSTEM_GATE_OUTPUT=artifacts/qa/system-<utc>.json`.
Without a key the orchestrator falls back to child `--dry-run` (exit 0 if
structure is coherent). It never passes `--create-code-task` or
`canary-mode=launch`. `CREATE_FIXTURE=1` is added only after
`GET /v1/factory/wip` advertises `fixture_supported`.
