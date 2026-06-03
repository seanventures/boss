# Codex Runbook for Building BOSS

## Codex operating mode

Use Codex as an implementation teammate, not as an unbounded autonomous operator.

For complex tasks, ask Codex to plan first, then implement the approved slice. Keep each slice small enough to review.

## Initial setup

1. Install Codex CLI or use Codex in your IDE/app.
2. Create a clean repo.
3. Copy this spec pack into the repo root.
4. From the repo root, ask Codex:

```text
Summarize the BOSS AGENTS.md instructions, then inspect docs/PRD.md and docs/TECHNICAL_SPEC.md. Do not write code yet. Return a build plan for milestone M0 only.
```

5. After reviewing the plan, paste `codex/prompts/00-bootstrap-monorepo.md`.

## Recommended prompt shape

Every Codex task should include:

- Goal
- Context files
- Constraints
- Done when
- Tests to run

Template:

```text
Goal: <what to build>
Context: Read <files> before coding.
Constraints: Follow AGENTS.md. Keep the PR small. Do not add external side effects.
Done when: <specific behavior + tests passing>.
Run: <commands>.
```

## Implementation sequence

1. `00-bootstrap-monorepo.md`
2. `01-database-and-migrations.md`
3. `02-control-plane-api.md`
4. `03-worker-leases.md`
5. `04-budget-governor.md`
6. `05-decision-inbox-ui.md`
7. `06-software-factory-mock.md`
8. `07-e2e-smoke-and-ci.md`

## Review loop

After each implementation prompt:

1. Ask Codex to run tests.
2. Ask Codex to review its own diff against `AGENTS.md`.
3. Ask Codex to list any missing acceptance criteria.
4. Commit only after you understand the diff.

## Do not ask Codex to do these in v0

- Create real DigitalOcean droplets.
- Send real Slack/email messages.
- Merge PRs automatically.
- Spend money on external model/tool APIs without a mock or local adapter.
- Build all agents from the catalog.

## First dogfood command after v0 works

```text
CEO command: Add a Decision Inbox page showing pending decisions with approve/reject actions. It must emit events, enforce tenant scope, and show receipts/audit links where applicable.
```
