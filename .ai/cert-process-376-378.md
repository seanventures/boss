# Cert — BOSS-376 / 377 / 378 (agent process, NL authoring, PM steward)

**Probed:** 2026-08-15T14:01:46Z–14:02:24Z  
**API:** `https://boss-api.local.sean.network`  
**Project:** `boss-dogfood`  
**Actor:** `executive_console` (principal `ceo` via `X-Boss-Api-Key`)  
**Auth:** `X-Boss-Api-Key` from `~/.config/boss/boss_api_key`, length-matched to first `BOSS_API_KEYS` entry on `root@goldeneye:/srv/apps/boss-app/.env`. Key never printed.  
**Live tag:** shared `b6d8ccc` (`boss-app-api-1` / `boss-app-web-1` / `boss-app-worker-1`). Local repo HEAD `3af9c64` (`feat(ops): five remaining operator slices`). Live is older than local; routes below were already mounted on `b6d8ccc`.  
**Edition:** Community (`GET /v1/edition?project_id=boss-dogfood` → 200).  
**Spend:** none. No `POST /v1/code-tasks`. Simulated start used `simulated=true` and `auto_build=false`. PM tick applied nothing this cycle.  
**FEATURES / GA:** ◐. **Not GA.** Do not stamp public GA.

## CLOSE recommendation

**YES** — close BOSS-376, BOSS-377, and BOSS-378.

The live simulated chain works on the current shared tag:

1. `GET /v1/agent-processes/templates` → **200**, default `prd_to_accepted_work` with the 11-step handoff catalog.
2. `POST /v1/agent-processes/runs` (`simulated=true`, `auto_build=false`) → **201**, run `aprun_04fbab9cced742c1a40c` **completed** intake → architect/CTO/VP review → improve → planned build → QA → UX → verify → panel `certified_planned` → captain accept.
3. `GET /v1/process-manager` → **200**, steward already-on (`pmgr_bd572e7f7a1a42a9a716`, `status=active`).
4. `POST /v1/process-manager/tick` (already-on only) → **200**, `status=verified`, `verify.ok=true`, applied `[]`, observe-only, receipt `rcpt_04708442662f4aca95cc`. `avg_score` stays `null`.

This is **not** a sealed factory proof and **not** live software certification. Simulated panel verdict is `certified_planned`. Captain build detail: `"No budgeted code.task."`

## Plane

| Ticket | UUID | State at cert | Name |
|---|---|---|---|
| [BOSS-376](https://plane.local.sean.network/goldeneye/projects/cc97d5cc-e8c6-4893-ac14-7f646c37f3d7/issues/3084a424-649e-414b-878c-d3985ed23457) | `3084a424-649e-414b-878c-d3985ed23457` | In Progress (`3b0d09f0-…`) | Agent process handoff workflows (PRD → review → build → QA/UX → panel → accept) |
| [BOSS-377](https://plane.local.sean.network/goldeneye/projects/cc97d5cc-e8c6-4893-ac14-7f646c37f3d7/issues/91b0edce-dcd5-4f7a-9a41-bff79cf39b9e) | `91b0edce-dcd5-4f7a-9a41-bff79cf39b9e` | In Progress | NL + manual + Metatron authoring of agent process workflows |
| [BOSS-378](https://plane.local.sean.network/goldeneye/projects/cc97d5cc-e8c6-4893-ac14-7f646c37f3d7/issues/dd002cba-b953-4d63-a055-b726c2b2d659) | `dd002cba-b953-4d63-a055-b726c2b2d659` | In Progress | Project Manager agent: operate, test, validate, and improve agent workflows |

This cert **recommends** Done. It does not itself flip Plane state or stamp FEATURES ✅.

## What was read (repo)

| Slice | Files |
|---|---|
| 376 runtime | `engine/boss_engine/services/agent_process.py` — templates/runs/steps, default `prd_to_accepted_work`, simulated never enqueues a budgeted factory job |
| 377 compiler + HTTP + Metatron/intent | `engine/boss_engine/services/process_compiler.py` (deterministic NL, no LLM); `apps/api/app/routers/agent_processes.py` (`/compile`, `/templates`, `/runs`); intent/Metatron classify-before-build tests |
| 378 steward | `engine/boss_engine/services/process_manager.py` — observe → measure → validate → apply-safe → verify; blocked `auto_build` / merge / deploy / rewrite |
| Routers | `apps/api/app/routers/agent_processes.py`, `apps/api/app/routers/process_manager.py`, mounted from `apps/api/app/routers/platform.py` |
| Worker | `apps/worker/boss_worker/main.py` ticks `/v1/agent-processes/tick-all` and `/v1/process-manager/tick` |
| UI | `apps/web/app/agent-process-ui.tsx` + `agent-process-actions.ts`; `apps/web/app/process-manager-ui.tsx` + `process-manager-actions.ts` (`data-testid="agent-processes"` / `"process-manager"`). Tick copy: never spends, never auto-builds, never rewrites a published template. |

## Unit tests (noconftest)

Command (repo `.venv`, `-p no:cacheprovider`, no `apps/api/tests/conftest.py`):

```
pytest -p no:cacheprovider -q --tb=short \
  engine/tests/test_agent_process.py \
  engine/tests/test_process_compiler.py \
  engine/tests/test_process_manager.py \
  engine/tests/test_sealed_e2e_contract.py \
  engine/tests/test_intent_process.py \
  scripts/tests/test_factory_e2e_gate.py::test_process_handoff_module_and_simulated_never_spends \
  scripts/tests/test_factory_e2e_gate.py::test_simulated_process_is_not_sealed_on_the_gate_contract \
  scripts/tests/test_factory_e2e_gate.py::test_agent_process_module_exists_and_exports \
  scripts/tests/test_factory_e2e_gate.py::test_process_compiler_and_manager_skip_honestly_when_absent
```

**Result: 50 passed in 3.33s.** No failures. No skips in this set.

Covered: default spec + deterministic reviews, panel `certified_planned` vs live `certified`, compiler fail-closed unknown role/action, PM `validate_action` reject auto_build/DLQ/rewrite, sealed contract `simulated_is_not_sealed`, process handoff catalog intake→accept, factory-gate “simulated never spends”.

API fixture tests (`apps/api/tests/test_agent_processes.py`, `test_process_manager_api.py`) were **not** run (they need conftest/DB). Process-manager engine tests that need Postgres would skip if no `DATABASE_URL`; the 50-pass set above included the manager file and completed without skip output.

## Live HTTP matrix

Auth header `X-Boss-Api-Key`. TLS verified via `curl -skS` against the uvicorn backend (`server: uvicorn`). Unauthenticated urllib without `-k` hit a Traefik-style `404 page not found` — that is a client TLS/SNI miss, not a missing route. `curl` is the authority here.

| Method | Endpoint | HTTP | Evidence |
|---|---|---|---|
| GET | `/health` | **200** | `status=ok`, `database_connected=true` |
| GET | `/v1/auth/session` | **200** | `auth_required=true`, `auth_method=api_key`, principal `ceo` |
| GET | `/v1/edition?project_id=boss-dogfood` | **200** | `edition_id=community` |
| GET | `/v1/agent-processes/templates?project_id=boss-dogfood` (no auth) | **401** | fail-closed: `Valid BOSS API key or OIDC bearer token required.` |
| GET | `/v1/agent-processes/templates?project_id=boss-dogfood` | **200** | 1 template, `prd_to_accepted_work`, 11 steps listed below |
| GET | `/v1/process-manager?project_id=boss-dogfood` | **200** | steward **already-on** (see PM) |
| POST | `/v1/process-manager/tick` | **200** | already-on; `status=verified` (see PM tick) |
| POST | `/v1/agent-processes/compile` | **200** | NL default story → 11 canonical step ids (see 377) |
| POST | `/v1/agent-processes/runs` | **201** | simulated run **completed** (see 376) |
| GET | `/v1/agent-processes/runs/aprun_04fbab9cced742c1a40c?project_id=boss-dogfood` | **200** | same 11 succeeded steps |
| GET | `/v1/agent-processes/runs?project_id=boss-dogfood` | **200** | 4 runs; newest is the cert run |
| GET | `/v1/process-manager` (after) | **200** | last tick `pmtick_d25820b73b0441708a5a` |
| GET | `/v1/process-manager/metrics` | **200** | counts only; `avg_score=null` |
| GET | `/v1/process-manager/proposals` | **200** | 1 proposed spec_diff, `apply=false` |

**Not called:** `POST /v1/code-tasks`, factory create, `auto_build=true`, live (`simulated=false`) process start, `/v1/process-manager/ensure` (already-on).

## BOSS-376 — handoff runtime (simulated)

**Simulated run id:** `aprun_04fbab9cced742c1a40c`  
**HTTP:** 201 create / 200 get  
**status:** `completed`  
**process_key:** `prd_to_accepted_work`  
**receipt:** `rcpt_3ee02626b337410eaa62`  
**input:** `simulated=true`, `auto_build=false`  
**context:** `simulated=true`, `build_status=planned`, no `code_task` / `crun_*`

| step_key | status | action | verdict | step receipt |
|---|---|---|---|---|
| intake_prd | succeeded | accept_prd | accepted | (run-level start receipt) |
| review_architect | succeeded | review_prd | approve | |
| review_cto | succeeded | review_prd | approve | |
| review_vp | succeeded | review_prd | **revise** | |
| improve_prd | succeeded | improve_prd | improved | |
| captain_build | succeeded | build | **planned** | `rcpt_b6804c3642bc417bb84c` |
| qa_check | succeeded | qa_review | checked | |
| ux_review | succeeded | ux_review | reviewed | |
| captain_verify | succeeded | verify_build | verified_planned | `rcpt_c9d3a27308ba4d62a1a2` |
| panel_certify | succeeded | certify_spec | **certified_planned** | `rcpt_5068a1f490eb4adf890d` |
| captain_accept | succeeded | accept_work | accepted | `rcpt_11b9577a6fca49898a25` |

Captain build output (verbatim): `"Build recorded as planned (simulated or auto_build=false). No budgeted code.task."`  
Panel: simulated builds are **not** live software certification. QA `tests_passed=null`, UX `has_e2e=false`, `states_covered=[]`. No invented scorecard.

Default template steps (GET templates):  
`intake_prd → review_architect / review_cto / review_vp → improve_prd → captain_build → qa_check → ux_review → captain_verify → panel_certify → captain_accept`

## BOSS-377 — NL / Metatron authoring

Live `POST /v1/agent-processes/compile` with the default story:

> PRD delivered, architect, CTO, and VP product review, improve, captain builds, QA, UI/UX, captain verifies, panel certifies, captain accepts

→ **HTTP 200**. Spec step ids match the default catalog. Compiler is deterministic (no LLM). `warnings=[]`.

`spec.key` on this live compile is `custom` (same as local `compile_process_from_text` for that prose). Step **ids/actions/entry** still match `prd_to_accepted_work`. Unit tests assert id/action/entry sets, not the key string.

Manual authoring surface exists (`agent-process-ui.tsx` compile / save template / start). Intent tests classify process authoring **before** multi-phase build (`engine/tests/test_intent_process.py`). Metatron process phrases are covered in `engine/tests/test_metatron_captain.py` (not re-run in this focused noconftest set). Live Metatron chat/drive was **not** POSTed (would be authoring-adjacent; compile + simulated start already prove the compiler/runtime path without spend).

## BOSS-378 — Project Manager steward

**Already-on before this session’s tick.**

| Field | Value |
|---|---|
| steward id | `pmgr_bd572e7f7a1a42a9a716` |
| status | `active` |
| last_tick (pre) | `pmtick_3b6030a6173f46cb9c98` @ 2026-08-15T14:01:38Z |
| last_tick (post) | `pmtick_d25820b73b0441708a5a` @ 2026-08-15T14:02:24Z |
| tick HTTP | **200** |
| tick `status` | `verified` |
| `verify` | `{ok: true, checks: [{name: observe_only, ok: true, detail: "no apply this cycle"}]}` |
| applied | `[]` |
| rejected | `[]` |
| planned | `[]` |
| receipt | `rcpt_04708442662f4aca95cc` |
| recent canary observed | `aprun_faf42df1fef644ea824f` (existing PM simulated canary) |
| metrics (tick-time) | `run_count=3`, `completed=3`, `failed=0`, `accept_rate=1.0`, `panel_certified_rate=1.0`, `revise_findings_count=9`, **`avg_score=null`** |
| proposal | `pmprop_ec9eea04d0a4450a8d35` `status=proposed` `reason=all_reviews_revise`; spec_diff `apply=false` `merge=false` `deploy=false` |

Tick was issued **before** the cert simulated start, so metrics still show 3 runs. That is honest observe-only: a recent completed canary already existed inside the 1h window, so the steward did not start another canary and did not rewrite the published template.

Existing runs after cert start (`GET /runs` 200, 4 items):

- `aprun_04fbab9cced742c1a40c` completed — **this cert**, `BOSS-376-378 simulated cert`
- `aprun_faf42df1fef644ea824f` completed — `PM simulated canary`
- `aprun_1555e62629704a31a8a5` completed — `Live dogfood after activity deploy`
- `aprun_cf7f50b44cc64aeaa59f` completed — `PM simulated canary`

## Honesty / what this is not

- Not sealed factory E2E. Sealed contract tests assert simulated ≠ sealed.
- Not live software certification. Panel said `certified_planned`.
- Not a spend/create proof. Factory allow-create was not touched. No `code.task`.
- Not a Metatron live chat dogfood.
- Not GA. Live tag `b6d8ccc` ≠ local `3af9c64`.
- Metrics `avg_score` is `null`. Panel checklist has no `score` field. UI/UX evidence stays empty/`not_scored` on the simulated path.

## CLOSE line (copy)

```
CLOSE: yes
simulated_run_id: aprun_04fbab9cced742c1a40c
pm_tick: pmtick_d25820b73b0441708a5a verified observe_only
live_tag: b6d8ccc
unit: 50 passed
GA: no
```
