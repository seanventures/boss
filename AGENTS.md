# AGENTS.md — BOSS repository instructions for Codex

## Mission

Build BOSS, the Business Operating System Sentinel: a financially controlled autonomous company OS that can dogfood itself. BOSS must turn CEO commands into auditable tasks, plans, decisions, budget-gated jobs, validated PRs, receipts, and event history.

## Read first

Before architecture or feature work, read:

1. `docs/product/PRD-v2.0.md`
2. `docs/product/TECHNICAL_SPEC-v2.0.md`
3. `docs/product/PRD-v1.1.md`
4. `docs/product/TECHNICAL_SPEC-v1.1.md`
5. `reference/boss_build_spec_pack/docs/CODEX_RUNBOOK.md`
6. `packages/specpack/workflows/ceo_command_to_pr.yaml`
7. `packages/specpack/policies/default_safety.yaml`
8. `packages/specpack/policies/resource_plane_policy.yaml`

## Build order

Implement the smallest vertical slice first:

1. Canonical DB schema and migrations.
2. API endpoints for commands, tasks, decisions, jobs, receipts, budget status.
3. Worker lease loop using Postgres.
4. Budget quote/reserve/consume/release path.
5. Event feed and decision inbox UI.
6. Acceptance tests proving no duplicate effects and no silent drops.

## Hard rules

- Do not call external billing, infra, Slack, GitHub, or model APIs directly unless an explicit policy-guarded adapter boundary exists.
- Every external or future external effect must be represented by a `Receipt` with a unique `dedupe_key`.
- Every spendy action must be preceded by a `BudgetReservation`.
- Every worker job must have a lease with TTL and heartbeat.
- Every provider write must be represented by a `ChangeSet`.
- Every provider action must be represented by a `ResourceReceipt`.
- Destructive provider actions and production DNS/deploy changes require explicit approval and rollback plan.
- Provider adapters are plugins; the Resource Plane Kernel is core BOSS infrastructure.
- OpenClaw is not the BOSS durable runtime or foundation.
- Every failure must be visible: retry, deadletter, or decision escalation. Never silently ignore errors.
- Do not add autonomous self-modifying behavior. BOSS may propose changes as PRs/config diffs only.
- Prefer small, reviewable PRs. Do not implement all packs/agents at once.

## Technical conventions

- Favor boring, explicit code over clever abstraction.
- Use typed schemas at boundaries.
- Keep policy enforcement deterministic.
- Keep LLM/agent behavior behind interfaces so it can be mocked in tests.
- Add tests for every state machine transition you implement.
- Add or update docs when changing behavior.

## Done means

For each implementation task:

- Tests pass.
- Lint/type checks pass if present.
- DB migrations or schema changes are included.
- State transitions are covered by tests.
- Budget/receipt behavior is covered if the feature can spend or create side effects.
- The event/audit trail shows what happened and why.

## Suggested commands once scaffold exists

- `make dev` — start local services.
- `make test` — run backend and frontend tests.
- `make lint` — run lint/format checks.
- `make seed` — seed a demo org/workspace/project.
- `make smoke` — run end-to-end smoke canaries.

## Review checklist

Before finishing a task, review the diff for:

- direct external calls bypassing adapters
- missing receipts or dedupe keys
- missing reservations for model/tool/infra work
- state machine transitions without tests
- missing tenant scope checks
- hardcoded secrets
- UI actions without corresponding audit/event records
