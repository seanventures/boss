# Governed QA benchmark applications

These benchmarks evaluate BOSS as a software-factory system. They are intentionally
small, but each requires the complete path: an operator launches a pinned contract in
Mission Control, BOSS creates a budgeted governed code task, the isolated plan/QA/build/
review stages execute, and the resulting evidence is rendered back in the Code Tasks UI.

They are not sample applications or a score inferred from model prose. A run passes only
when its durable plan and authoritative `npm run verify` result are present and successful.
Missing evidence is reported as `not_recorded`.

## Catalog

| Benchmark | Primary proof | Required hard cases |
| --- | --- | --- |
| BugBox | Ordinary full-stack delivery | CRUD, migrations, persistence after refresh, search/filtering, derived dashboard stats, deletion |
| HookWatch | Backend/integration depth | Dynamic capture endpoint, JSON/plain-text parsing, persisted event history, polling, allowlisted replay and response record |
| ApproveIt | Business-rule correctness | Requester/manager roles, server-side authorization, terminal-state immutability, transition audit log, negative paths |

## Completion contract

Every benchmark must deliver:

1. A written implementation plan in the governed run.
2. A working frontend and backend; local schema, migrations, and seed data.
3. Client and server validation, API tests, and browser-level end-to-end coverage.
4. One-command local startup, a real health-check endpoint, lint, type-check, and a concise README.
5. A final verification command, `npm run verify`, which invokes and reports each required command:
   `npm run build`, `npm run typecheck`, `npm run lint`, `npm run test:api`,
   `npm run test:e2e`, and `npm run healthcheck`.
6. No unresolved TODO in required functionality.

The agent must report command results, not assert them. BOSS persists the authoritative
`npm run verify` exit result in the code-task evidence and exposes
`GET /v1/qa/benchmark-apps/runs/{run_id}/report` to make incomplete evidence obvious.

## Scorecard and workflow tuning

Each report includes a versioned, weighted scorecard tied to the original benchmark
contract. It evaluates functionality (22%), capability coverage (12%), interface quality
(12%), app quality (14%), speed (8%), efficiency (8%), cost discipline (6%), token
consumption (6%), implementation effort (4%), independently assessed innovation (4%),
and reliability (4%). The fixed v1 baselines are 15 minutes, $5, 200k tokens, 80 turns,
and 30 changed files.

Scores are evidence-aware: build/verification, phase timing, cost, token telemetry,
turns/files, repair attempts, and independent quality gates are calculated from durable
run records. Capability-to-test mapping, browser-state evidence, and innovation review
remain **not scored** until cited evidence exists. Overall scores are provisional until
the full 100% weight has evidence. This lets operators tune workflow profile, model route,
repair budget, and test strategy from measured regressions rather than vague impressions.

## Developer runner and evidence export

Developer models and operators can launch the exact same governed workload from a terminal:

```bash
make qa-benchmark BENCHMARK=bugbox PROJECT_ID=<boss-project-id> \
  QA_BENCHMARK_OUTPUT=tmp/bugbox-report.json
```

The command calls only the BOSS control-plane API, queues the normal budgeted code-task
run, waits for its terminal durable report, prints it, and optionally writes a JSON evidence
export. `BOSS_API_URL` and `BOSS_API_KEY` select an authenticated control plane; credentials
are never placed in the output. `--no-wait` is available on `scripts/run_qa_benchmark.py`
when an external worker will finish the run later.

The exported report is the input to a tuning comparison. It preserves the benchmark ID,
scorecard version, observed-weight coverage, command evidence, cost/token telemetry, and
unscored gaps, so a developer can compare like-for-like runs instead of optimizing from
an anecdote.

## Governed baseline-improvement loop

`qa_benchmark_baseline_tuning` connects scorecard evidence to the established
`self_improvement` proposal/canary/rollout workflow. A terminal report can identify measured
gaps and prepare a candidate packet, but BOSS will create a proposal only when a developer
supplies a traceable `change_ref` for an already-authored PR or configuration diff:

```text
benchmark report → measured-gap recommendation → candidate PR/config diff
  → isolated control/candidate comparison → approval → QA-only canary
  → staged rollout or rollback
```

The API endpoint is
`POST /v1/qa/benchmark-apps/runs/{run_id}/tuning-proposal`. It creates a normal
`ImprovementProposal` with a review decision, an isolation test plan, canary scope, rollback
plan, and success metrics derived from the report. It does **not** choose a model, modify a
workflow, alter a baseline, open a PR, or apply a change. A regression may not normalize the
baseline: baseline version changes require the separate evidence-backed approval workflow.

## Operator workflow

1. In **Code Tasks**, select one benchmark from **End-to-end QA benchmarks**.
2. BOSS validates `task_create` authority, reserves budget through the standard code-task path,
   records a code-task run, and dispatches its leased `code.task` job.
3. Follow the existing phase ribbon and evidence bundle. The strict profile supplies independent
   test, review, security, and consistency lanes.
4. Open the benchmark report after completion. A benchmark cannot be called passed unless its
   report says `passed: true`; failed or unrecorded evidence remains visible for repair.

## Agent catalog smoke test

Agent Studio also offers **Smoke test all agents**. It invokes the deterministic test path
for every active catalog version (all 13 seeded personas by default), records one durable
test result per agent, and reports blocked/invalid entries rather than skipping them. It
uses zero model calls, zero tool calls, and zero external effects; a live-model evaluation
remains a separately budgeted, leased workflow.

## Safety boundaries

- The suite never grants raw credentials or direct provider access.
- HookWatch replay is limited by the generated app contract to an auditable built-in mock
  destination. It must not become an arbitrary production egress mechanism.
- Launching a benchmark uses the normal receipt, budget reservation, lease, retry/deadletter,
  review, and merge-decision controls. The benchmark layer does not certify or merge a change.
- Tuning candidates require a comparable control with the same pinned benchmark and scorecard
  revision. Correlation is recorded as inconclusive; only a single-variable, evidence-backed
  comparison can advance to a canary decision.
