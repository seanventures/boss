# BOSS PRD v0.1

## Product name

BOSS — Business Operating System Sentinel

## Product thesis

BOSS is a financially controlled autonomous company operating system. It gives a CEO a command interface that converts outcomes into tasks, plans, agent work, decisions, budget-governed execution, verified software changes, receipts, and audit trails.

The first product domain is BOSS itself: BOSS must dogfood its own workflows to improve BOSS through PRs, tests, approvals, budget limits, and rollback plans.

## Target users

Primary initial user:

- Founder/CEO/product person building software with AI agents and limited coding capacity.

Future users:

- Small software teams that want agentic execution with budget/accountability controls.
- Agencies and startups that want installable company packs.
- Enterprise teams that need auditable AI work, approvals, and spend control.

## Core value proposition

Most agent tools run tasks. BOSS runs an accountable company loop:

CEO command → plan → budget → execute → verify → decide → receipt → audit → improve.

## Goals

1. Convert fuzzy CEO commands into structured work.
2. Provide an executive console with what changed, decisions, risks, costs, and next actions.
3. Provide a decision inbox with recommendations, options, costs, deadlines, defaults, and approval receipts.
4. Run durable background jobs with leases, retries, and deadletters.
5. Enforce financial control with budget policies, reservations, cost receipts, and degraded mode.
6. Manage agent roles/personas as RoleSpecs with boundaries, permissions, autonomy, and evaluation rubrics.
7. Support a software factory workflow: spec → plan → chunk → code/tests → validate → PR → merge decision.
8. Support self-improvement only through artifacted proposals, tests, canaries, approvals, and rollbacks.

## Non-goals for v0

- Fully autonomous production deploys.
- Full enterprise RBAC/compliance.
- Real cloud autoscaling without a mock boundary.
- Dozens of agent roles.
- True general company replacement.
- Browser automation or creative/video tools in the first slice.

## MVP user flows

### 1. Onboard demo workspace

User creates a demo organization, workspace, and project called `boss-dogfood`.

Acceptance:

- Seeded roles: Metatron, COO, CTO, PM, QA, Release, VP Agents.
- Seeded policy: default safety + budget policy.
- Seeded model/skill registry with mock adapters.

### 2. CEO command

User types: “Add a decision inbox page that lets me approve/reject pending decisions.”

System creates:

- Command event
- Task
- Initial plan artifact
- Budget quote
- Job for planning or implementation

Acceptance:

- User sees task and plan in UI.
- Budget estimate is visible before execution.
- Job is leased and completed by worker.

### 3. Budget-gated execution

Before a model/tool/worker action executes, BOSS quotes and reserves budget.

Acceptance:

- If budget is available, reservation is created and later consumed/released.
- If budget is unavailable, no job starts and a decision card is created.

### 4. Decision inbox

A decision card appears when approval is required.

Acceptance:

- Card shows recommendation, options, risks, cost, deadline, and default action.
- Approve/reject updates decision state and emits event.
- All decision actions create receipts or audit records.

### 5. Exactly-once effect receipt

If a side-effect is attempted twice with the same `dedupe_key`, BOSS returns the first receipt instead of repeating the effect.

Acceptance:

- Automated test proves duplicate execution does not duplicate receipt/effect.

### 6. Worker lease and deadletter

Worker pulls a job, heartbeats, completes it, or fails it into retry/deadletter.

Acceptance:

- Stale lease is reclaimable.
- Retry count is bounded.
- Deadletter is visible in UI/API.

## Success metrics

### Reliability

- Duplicate effect rate: 0 in tests.
- Silent drop rate: 0; all failed messages/jobs visible.
- Stuck lease recovery: automatic within TTL window.

### Cost control

- No spendy action can execute without reservation.
- UI shows daily cap, actual, reserved, burn rate, and budget state.
- Budget hard-cap tests pass.

### Delivery

- One CEO command can create a plan and a decision card end-to-end.
- Software factory mock can generate a PR proposal artifact end-to-end.

## v0 milestones

### M0 — Repo scaffold

- Monorepo skeleton.
- Local Postgres.
- Basic UI/API/worker.
- Test and lint commands.

### M1 — Canonical objects

- Org/workspace/project.
- Task, Decision, Job, Receipt, Artifact, Event.

### M2 — Budget governor

- BudgetPolicy, BudgetReservation, CostReceipt.
- Quote/reserve/consume/release.
- Budget status endpoint and UI widget.

### M3 — Decision inbox and event feed

- Pending decision cards.
- Approve/reject flow.
- Event/audit feed.

### M4 — Software factory mock

- Spec-to-PR workflow in mock mode.
- Review packet artifact.
- Approval-gated merge placeholder.

## Future packs

- Software Factory Pack: OpenHands/Codex coding worker integration, PR review, CI gates.
- Integrations Pack: Slack, GitHub, email, MCP/Activepieces.
- FinOps Pack: cost anomaly detection, budget forecasting, commitment janitor.
- Creative Pack: design/image/video tool skills.
- Enterprise Pack: compliance, audit export, data boundary controls.
