# BOSS Implementation Plan

## Foundation decisions

- Build the BOSS control plane custom.
- Use Postgres as source of truth for coordination.
- Use local object artifacts in v0.
- Use deterministic policy/budget enforcement.
- Keep all AI/model/tool integrations behind adapters.
- Dogfood through PRs only.

## Milestone M0 — scaffold

Deliver:

- Local dev setup.
- API health endpoint.
- Web shell with dashboard placeholder.
- Worker process that can start and idle.
- DB connection and migrations.
- Seed demo org/workspace/project.

Acceptance:

- `make dev` starts services.
- `make test` passes.
- Dashboard loads demo project.

## Milestone M1 — canonical objects

Deliver:

- CRUD/read APIs for task, decision, artifact, receipt, event.
- Command endpoint creates task and event.
- Plan artifact can be attached to task.

Acceptance:

- API tests cover create/read/list.
- Event feed shows command/task creation.

## Milestone M2 — worker leases

Deliver:

- Job queue table.
- Lease claim endpoint or internal function.
- Worker loop.
- Heartbeat/complete/fail.
- Deadletter handling.

Acceptance:

- Two workers cannot claim the same job.
- Expired lease can be reclaimed.
- Failed job retries then deadletters.

## Milestone M3 — budget governor

Deliver:

- BudgetPolicy.
- BudgetReservation.
- CostReceipt.
- Quote/reserve/consume/release service.
- Budget widget API.

Acceptance:

- Over-cap reservation denied.
- Under-cap reservation allowed and consumed.
- Budget status shows cap, actual, reserved.

## Milestone M4 — decision inbox

Deliver:

- Decision cards UI.
- Approve/reject actions.
- Deadline/default fields.
- Events for all actions.

Acceptance:

- Pending decisions visible.
- Approve/reject updates state.
- State changes visible in event feed.

## Milestone M5 — software factory mock

Deliver:

- Spec-to-PR workflow in mock mode.
- Review packet artifact.
- Mock GitHub adapter creates a fake PR receipt.
- Merge decision card.

Acceptance:

- CEO command can produce fake PR proposal and merge decision.
- Duplicate fake PR effect uses same receipt.
