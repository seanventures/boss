# Agent Studio and A2A workstream

- Date: 2026-07-26
- Owner: Codex
- Branch: `ai/codex/boss-256-agent-studio-a2a`
- Plane: BOSS-256–266
- Parallel work checked: Paramg PR #80 remains untouched.

## Delivered

- Immutable AgentDefinition/AgentVersion configuration, validation, test,
  publish, scope compilation, and leased execution.
- Immutable PanelDefinition/PanelVersion composition with attributed findings,
  evidence gates, preserved dissent, and deterministic consolidation.
- Twelve specialist agents and three reusable Panels seeded from SpecPack.
- Canonical Postgres A2A envelope/delivery/outbox ledger with Redis Streams
  transport, Receipt-backed XADD/XACK, visibility/reclaim/deadletter,
  replay/cancel, health, and audit.
- Responsive Agent Studio operator workbench.

## Validation

- 121/121 web tests.
- 938/938 executable Python tests; four intentional external/live skips.
- 39 focused Agent Studio/A2A/worker/assurance tests.
- Ruff, TypeScript, OpenAPI YAML, diff checks, and optimized Next build.
- Real Redis publish/claim/ACK/replay/cancel and XAUTOCLAIM/deadletter proofs.

## Handoff

Keep Plane issues In Progress until the PR merges. Do not enable live-model
execution by bypassing BudgetReservation, model adapter, entitlement, or cost
Receipt controls.
