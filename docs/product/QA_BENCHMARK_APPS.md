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

## Operator workflow

1. In **Code Tasks**, select one benchmark from **End-to-end QA benchmarks**.
2. BOSS validates `task_create` authority, reserves budget through the standard code-task path,
   records a code-task run, and dispatches its leased `code.task` job.
3. Follow the existing phase ribbon and evidence bundle. The strict profile supplies independent
   test, review, security, and consistency lanes.
4. Open the benchmark report after completion. A benchmark cannot be called passed unless its
   report says `passed: true`; failed or unrecorded evidence remains visible for repair.

## Safety boundaries

- The suite never grants raw credentials or direct provider access.
- HookWatch replay is limited by the generated app contract to an auditable built-in mock
  destination. It must not become an arbitrary production egress mechanism.
- Launching a benchmark uses the normal receipt, budget reservation, lease, retry/deadletter,
  review, and merge-decision controls. The benchmark layer does not certify or merge a change.
