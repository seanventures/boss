# The harness benchmark

BOSSAPP-959, phase 0 of the harness kernel epic (BOSSAPP-958). Every later change to how BOSS
drives a coding agent — prompts, context, routing, repair, the kernel itself — is accepted or
rejected by this benchmark. It replaces "I think it is better" with a number and a verdict.

## What it is

- **A suite** (`bench/harness/suite.yaml`, versioned): eight tasks.
  - Three small fixes and three features on existing apps: HelpDesk (`fixtures/helpdesk-lite`,
    HelpDesk's ticket domain in dependency-free Node) and a Python library (`fixtures/textkit`).
  - Two new apps from an empty repository: a Node slug service and a Python converter.
  - Each fixture is pinned by the sha256 of its files, written in the suite file. Change a
    fixture without re-pinning and the suite refuses to load.
- **A runner** (`scripts/harness-bench`, the module `scripts/harness_bench.py`). For every task,
  and every pass of `--repeat`, it:
  1. seeds the fixture into a local GitHub (the e2e gate's: git smart-HTTP and the REST API on
     loopback, `tests/e2e/gate/remote.py`); its CI answers at once with one passing check;
  2. starts the build exactly as the API does (`code_task_service.start_code_task`) and runs it
     exactly as the worker does (`run_code_task`). That is BOSS's real governed pipeline, in this
     process, on the chosen engine — the engines gate's way;
  3. follows the lineage: a repair child the build queued (a cold review-repair round, a
     checks-failing continuation) is run next, as the worker would;
  4. reads what the build cost and did from the database (`engine/boss_engine/harness_bench/metrics.py`);
  5. clones the pull request's head and runs the acceptance checks itself (below).
- **Storage**: `harness_bench_runs` and `harness_bench_results` (`db/schema.sql`), and one JSON
  report per run.
- **A comparison** (`harness_bench/compare.py`) with a named baseline: a verdict of better,
  same or worse; an exit code of 1 when a guard fails.
- **CI** (`.github/workflows/ci.yml`, job `bench-scripted`): the scripted run against the
  committed baseline `bench/harness/baselines/scripted.json`, on every pull request.
- **The harness score**: `GET /v1/harness/score` and `scripts/harness-bench report`. Each shows
  the latest run per engine and level against its baseline, with the verdict per metric.

## Running it

It runs only on a database named `boss_test*` (`--db-url`, else `BOSS_BENCH_DATABASE_URL`, else
`DATABASE_URL`). It refuses any other, so it can never touch a live install. It applies the
schema itself and works in a throwaway project per run.

### Scripted: no model, no cost, about 2 minutes

```bash
export BOSS_BENCH_DATABASE_URL=postgresql+psycopg://boss:<password>@localhost:5432/boss_test_bench
scripts/harness-bench run --engine scripted --baseline scripted      # the CI job, locally
scripts/harness-bench run --engine scripted --save-baseline scripted # after a suite change
```

The scripted conformance engine (`services/scripted_engine.py`) writes each task's solution
overlay (`bench/harness/solutions/<task>`) stage by stage. It plays the task's faults: a
reviewer BLOCKER on `helpdesk-priority`, and failing tests on `new-slug-service`. So the fix
rounds run in the baseline too.

This run proves the plumbing and the measuring:

- every stage, gate, PR and acceptance check runs;
- cost is zero;
- model calls, turns and repair rounds are fixed numbers, so a harness change that adds a call
  or a round shows up exactly.

### A real engine: costs money, needs a budget

```bash
scripts/harness-bench run --engine claude_code --repeat 3 --budget-usd 120 \
    --allow-unisolated --baseline claude_code-<date>        # compare with an earlier real run
scripts/harness-bench run --engine codex --budget-usd 40 --allow-unisolated
scripts/harness-bench run --engine native:qwen2.5-coder --budget-usd 20 --allow-unisolated
```

- **`--budget-usd`** is required. It is the most the whole run may spend, at list price.
  - Each build's own budget reservation (`code_task_budget_usd_micros`) is capped at what is left.
  - When the budget is spent, the remaining tasks are skipped. The run is then `incomplete`, and
    an incomplete run cannot be compared.
- **`--allow-unisolated`** is required on a host without `boss-sandbox-exec` (a laptop). There,
  the engine's sessions run as you, outside the sandbox.
- **Credentials** come from the environment: the `claude` or `codex` CLI's own sign-in, or
  `OPENAI_API_KEY` for `native`. The test database holds no stored keys.
- **`--dry-run`** prints the plan, the config fingerprint and the guards. It builds nothing.

**Expected cost and time** come from `scripts/harness-bench estimate --history-db-url <url>`.
That command reads past builds read-only. On the laptop install, the last 60 days of claude_code
builds (133 completed) were:

| Per build | p50 | p75 | p90 |
|---|---|---|---|
| USD at list price | $4.27 | $6.65 | $11.46 |
| Wall time | 23 min | 48 min | 111 min |

- Those builds were whole apps. The suite's tasks are smaller, so expect at or below the p50:
  roughly $2–4 and 10–25 minutes per task.
- One pass of eight tasks is about $20–35 and 2–3 hours. Three passes cost about three times that.
- 78% of those builds ran on a subscription, where the dollar figure is list-price value, not money.

### Comparing

```bash
scripts/harness-bench compare <run-id | report.json> --baseline scripted [--json]
scripts/harness-bench report                    # the harness score in this database
```

A baseline is a name: `bench/harness/baselines/<name>.json`, else the latest run saved under
that name in the database. It can also be a report path.

The comparison refuses with exit code 2 when:

- the suite changed (a different sha256);
- the engines differ (unless `--cross-engine`);
- the levels differ;
- the candidate is incomplete.

### Options

| Option | Meaning |
|---|---|
| `--engine` | `scripted`, `claude_code`, `codex`, `native:<model>`, or any registered harness `name[:model]` |
| `--level` | the scaffold level: `delegate` today; phase 3 adds `guided` and `stepwise` |
| `--tasks` | `all` or comma-separated task ids |
| `--repeat N` | N passes over the tasks. Spread needs at least 2; use 3 for a real engine |
| `--set key=value` | a harness setting for this run, e.g. `code_repair_max_retries=2`. It is recorded in the config, and unknown keys are refused |
| `--scripted-faults` | faults added to every task on the scripted engine, e.g. `failing_test` |
| `--save-baseline NAME` | write `bench/harness/baselines/NAME.json` and mark the run in the database |

## What each number means

All numbers are read from BOSS's records, never from what the agent says.

| Metric | Better | Read from |
|---|---|---|
| `success_rate` | higher | runs where every success component holds / runs |
| `wall_s` | lower | the runner's clock, from `start_code_task` to the end of the lineage |
| `tokens_in` | lower | `llm_calls`: prompt + cache read + cache write tokens |
| `tokens_out` | lower | `llm_calls.completion_tokens` |
| `cache_share` | higher | cache read / `tokens_in` |
| `cost_usd` | lower | `llm_calls.cost_usd_micros` / 1e6 (list price, also on a subscription) |
| `model_calls` | lower | `llm_calls` rows for the lineage |
| `turns` | lower | the sum of `llm_calls.turns` (the engine's own count) |
| `repair_rounds` | lower | `quality_gates.evidence.repair_attempts` (the implement loop's retries) + `code_task.inrun_convergence_round` (warm review rounds) + `code_task.review_repair_round` (cold rounds) |
| `interventions` | lower | `decisions` asked in the project while the task ran, except the planned merge question (`code_task_runs.decision_id`, "Approve code change: …"), plus a run parked in `awaiting_gate_override` or `awaiting_plan_approval` |

Each result also keeps:

- `tool_calls` and transcript rows (from `agent_turns`);
- cost and tokens per stage, the engines, models and routes;
- the gate verdicts;
- the release gate's own end-of-build checks (`scripts/e2e_gate_agent.py: final_checks`);
- the fixture commit;
- the lineage.

**Success** is every one of these, and nothing less:

| Component | Holds when |
|---|---|
| `completed` | the lineage's last run ended `completed` |
| `pr_opened` | it opened a pull request |
| `verify_passed` | BOSS's own test run passed (`quality_gates.tests_passed`, and `written_tests_pass` did not fail) |
| `review_passed` | the review gate passed (`code_review_passed`) |
| `gates_passed` | the quality gate's status is `pass` |
| `no_checks_failing_pr` | no PR opened with its checks failing |
| `acceptance_passed` | the benchmark's own checks passed (next table) |

The **acceptance checks** run on a clean clone of the pull request's head:

- the branch carries a change, and the files the task must change did change (or must exist do
  exist);
- the task's own test commands pass;
- the **held tests** pass. These are `bench/harness/acceptance/<task>`, copied into
  `bench_acceptance/` over anything the branch put there. The agent never sees them.

A held test fails on the untouched fixture and passes on the scripted solution.
`engine/tests/test_harness_bench_suite.py` checks this for every task, so a run that changed
nothing cannot pass.

## How the guards decide

The rules live in the suite file: `noise`, `metrics` (the floors) and `guards`. They can be
overridden per engine under `engine_overrides`. A run records the rules it was judged by.

1. **Aggregate per pass.** Over the tasks both runs measured:
   - `success_rate` = passing runs / runs;
   - `cache_share` = cache read / input tokens;
   - every other metric is the sum over tasks.

   A pass that lacks a task is not a sample of the suite, so it is left out.
2. **Mean and spread.** With N passes, each metric has N samples: mean, sample stdev, min, max.
3. **Noise band.** `band = max(abs_floor, rel_floor × |baseline mean|, k_stdev × max(stdev_baseline, stdev_candidate))`.
   - A difference within the band is `same`.
   - Nothing is called better or worse on noise.
   - With one pass the stdevs are 0, so only the floors apply. Run `--repeat 3` for a real engine.
4. **Guards.** A guard fails when the candidate is worse by more than its limit AND by more than
   the band. The limits are:
   - `max_drop` / `max_rise`: an absolute worsening;
   - `max_rise_pct`: a percentage of the baseline mean. A baseline of 0 allows no rise beyond the band.
5. **Verdict.**
   - `worse` when any guard fails (exit 1).
   - `better` when at least one metric is better and none is worse.
   - Otherwise `same`. An unguarded metric that got worse is reported, and keeps the verdict
     from being `better`.

The suite's guards today:

| Guard | All engines | Scripted only |
|---|---|---|
| `success_rate` | must not drop | must not drop |
| `cost_usd` | +15% at most | +15% at most |
| `wall_s` | +25% at most | none: a CI runner against a laptop would guard the hardware |
| `interventions` | must not rise | must not rise |
| `model_calls` | | must not rise |
| `turns` | | must not rise |
| `repair_rounds` | | must not rise |

The scripted guards on calls, turns and rounds are exact, because that engine is deterministic.

## The regression it caught

Measured on the laptop at 216ae9646, 2026-11-10. The same suite and baseline ran three times:

| Run | What changed | Verdict | Exit |
|---|---|---|---|
| A | `--scripted-faults failing_test`: every task's first implementation fails its tests | WORSE: model_calls 61→68, turns 61→68, repair_rounds 2→9 (guards failed); tokens_in +27% | 1 |
| B | `--set code_review_inrun_max_rounds=0` | SAME. Correct: `effective_review_rounds` reads 0 as 2, and a blocker always gets one warm round, so the setting changes nothing | 0 |
| C | nothing (a rerun) | SAME: wall time +13.5 s and tokens_in −7 are both inside their bands | 0 |

`engine/tests/test_harness_bench_e2e.py` repeats run A on one task in every P1 run.

## Adding or changing a task

1. **Fixture.** Put a fixture under `bench/harness/fixtures/<name>`. Keep it small, with no
   dependencies to install: the runner's acceptance step installs nothing.
2. **Pin it.** Write the fixture's `sha256` in the suite. Print it with:
   ```bash
   PYTHONPATH=engine python3 -c "from boss_engine.harness_bench.suite import tree_sha256; from pathlib import Path; print(tree_sha256(Path('bench/harness/fixtures/<name>')))"
   ```
3. **Held tests.** Write them under `bench/harness/acceptance/<task>`. They must fail on the
   fixture and pass on a correct change.
4. **Scripted solution.** Write the overlay under `bench/harness/solutions/<task>`: the files of a
   correct change. Tests go under `test/` or `tests/`; the scripted engine writes them at
   `write_tests` and the rest at `implement`.
5. **Version.** Bump `version`.
6. **Baseline.** Run `--save-baseline scripted`, and commit the new baseline with the change.
   `test_harness_bench_suite.py` fails while the committed baseline was made on another suite hash.

## Limits today

- **Where it runs.** It runs in the runner's process, like the engines gate. On a laptop the
  sandbox is not isolated. A container mode, the worker image next to a throwaway Postgres as in
  `scripts/e2e-gate.sh`, is a follow-up.
- **Timeouts.** The per-task time limit (`defaults.timeout_s`) cancels the build at its next stage
  boundary through `cancel_run`. A stage that never returns is not interrupted.
- **Token counts across engines.** `tokens_in` adds prompt, cache read and cache write tokens.
  That is right for Claude, whose prompt tokens exclude the cache. For an engine whose prompt
  tokens already include cached ones, it double-counts the cached part. Compare such engines on
  `cost_usd`, not on `tokens_in`.
- **The score has no screen yet.** It is in the API and the CLI only; the console row is a
  follow-up.
- **The scripted baseline's wall time** is reported but not guarded.
