# BOSS User Stories and Acceptance Criteria

Last updated: June 3, 2026

These stories are derived from `docs/product/PRD-v1.0.md`, `reference/boss_build_spec_pack/docs/TECHNICAL_SPEC.md`, `packages/specpack/workflows/ceo_command_to_pr.yaml`, and `packages/specpack/policies/default_safety.yaml`.

## Founder / CEO

### Story CEO-001: Issue Outcome Commands

As a founder/CEO, I want to submit a natural-language outcome, so that BOSS turns it into structured, auditable work.

Acceptance criteria:

- Given an authenticated API client and active project actor, when the CEO submits a command, then BOSS creates a task, planning job, command artifact, and event.
- Given an unknown or unauthorized actor, when the command is submitted, then BOSS rejects it and emits a visible `policy.denied` event when the project exists.
- Given the API or database is unavailable, when the console loads, then it shows a visible blocked state instead of pretending work was accepted.

### Story CEO-002: Review Decisions

As a founder/CEO, I want clear decision cards with costs, risks, deadlines, defaults, and options, so that I can safely approve or reject material actions.

Acceptance criteria:

- Given a pending decision, when the inbox renders it, then recommendation, options, cost summary, risk tier, deadline, and default action are visible.
- Given a Tier-2 or Tier-3 decision, when an actor without the configured approver role attempts approval, then BOSS rejects the approval and emits `policy.denied`.
- Given an overdue pending decision, when decisions are refreshed, then BOSS expires it to the safe default and emits `decision.expired`.

### Story CEO-004: Receive Decision Digests

As a founder/CEO, I want pending decisions grouped into digest artifacts, so that urgent approvals stand out while low-risk items can be reviewed in batches.

Acceptance criteria:

- Given pending decisions with mixed risk tiers, when a digest is created, then BOSS stores a durable `decision_digest` artifact with urgent, standard, and low-risk batch groups.
- Given an optional digest channel, when the digest is created, then BOSS creates an outbox draft and does not send it until a separate budgeted queue action occurs.
- Given the digest is created, when events are inspected, then `decision.digest_created` links to the artifact and includes item counts.

### Story CEO-003: Understand What Changed

As a founder/CEO, I want a current event and health view, so that I can see risks, blockers, costs, and next actions.

Acceptance criteria:

- Given live project state, when the console loads, then it shows budget state, pending decisions, open tasks, active jobs, deadletters, and top risks.
- Given a Metatron query, when BOSS answers, then the answer includes freshness and source references.
- Given a denied action, failed job, or blocked effect, when I inspect activity, then it appears in events, deadletters, or decisions.

## Product / Engineering

### Story ENG-001: Produce Reviewable Software Work

As an engineering lead, I want CEO commands to become PR proposals with validation evidence, so that software changes are reviewable and safe.

Acceptance criteria:

- Given an approved plan decision and budget capacity, when follow-on execution starts, then BOSS reserves PR budget and queues a leased software-factory job.
- Given a real sandbox PR proposal, when GitHub validation completes, then BOSS stores test report and review packet artifacts.
- Given local sandbox validation is enabled, when a PR proposal is created, then BOSS runs real allowlisted commands in the configured sandbox clone and receipt-backs the validation run.
- Given failing or missing validation, when merge is considered, then BOSS blocks merge and creates visible evidence instead of silently proceeding.

### Story ENG-002: Package Agent Logic

As a VP Agents or marketplace operator, I want worker agents packaged as modules, so that roles, prompts, skills, and runtime defaults can be versioned and distributed.

Acceptance criteria:

- Given an agent module in the specpack catalog, when it is installed, then BOSS records module installation and role lineage.
- Given a governed job kind, when runtime behavior is resolved, then BOSS uses installed module configuration instead of hidden service defaults.
- Given an unauthorized actor, when module installation is attempted, then BOSS rejects it and emits a policy denial.

## Finance / Governance

### Story FIN-001: Govern Spend

As a finance operator, I want every spendy action quoted and reserved before execution, so that BOSS cannot run away with model, tool, or infrastructure cost.

Acceptance criteria:

- Given an over-cap reservation request, when it is submitted, then BOSS denies execution and creates a decision card.
- Given an active reservation, when spend completes, then BOSS records actual cost and releases or consumes budget.
- Given a spend-gated job without an active reservation, when a worker polls, then BOSS refuses to lease it and creates a visible decision or event.

### Story FIN-002: Inspect Budget Forecasts

As a finance operator, I want actual spend, reservations, burn rate, forecast, time-to-cap, and cost drivers in one API-driven dashboard, so that I can understand spend pressure before caps are hit.

Acceptance criteria:

- Given recorded cost receipts, when budget status is requested, then BOSS returns actual spend, top cost drivers, observed burn rate, forecasted 24-hour spend, and time-to-cap.
- Given active reservations, when the dashboard renders, then reserved spend and forecasted spend include those reservations.
- Given the budget API is unavailable, when the console renders, then it shows an explicit unavailable state rather than synthetic budget data.

### Story GOV-001: Prove Effects and Dedupe

As an operator or auditor, I want every external or material side effect receipt-backed, so that duplicate effects cannot silently repeat.

Acceptance criteria:

- Given an effect dedupe key that already succeeded, when the effect is attempted again, then BOSS returns the prior receipt and does not repeat the effect.
- Given a side-effect adapter failure, when retries are exhausted, then BOSS creates a deadletter or decision rather than dropping the failure.
- Given an audit export, when scoped to a task, decision, or job, then the chain includes related tasks, jobs, decisions, artifacts, receipts, reservations, routing logs, deadletters, and events.

### Story GOV-002: Export Compliance Evidence

As an auditor or compliance admin, I want a durable compliance evidence bundle, so that access, budget, policy, decision, failure, and side-effect controls can be reviewed without reconstructing them manually.

Acceptance criteria:

- Given an authorized audit actor, when a compliance evidence bundle is requested, then BOSS creates a durable `compliance_evidence_bundle` artifact.
- Given the artifact is inspected, then it includes project members, role governance, policy records, break-glass grants, decisions, receipts, budget reservations, cost receipts, deadletters, and compliance-relevant events.
- Given an unauthorized actor requests the bundle, when the API enforces access, then BOSS rejects the request and records a visible policy denial when the project can be resolved.

### Story GOV-003: Classify Data Boundaries

As a security or compliance operator, I want work objects to carry data-boundary classifications, so that restricted or local-only work can be routed and audited safely.

Acceptance criteria:

- Given a command or task with a `data_boundary`, when BOSS creates related jobs and artifacts, then the classification is persisted and inherited instead of defaulting to a weaker boundary.
- Given a receipt-backed effect with a `data_boundary`, when receipts or audit exports are inspected, then the classification is visible alongside the dedupe and effect metadata.
- Given restricted or local-only data, when the entitlement broker evaluates model capacity, then unapproved external sources are rejected and the routing log records the boundary decision.
- Given restricted or local-only data, when software-factory work would create a GitHub PR, merge, or deployment, then BOSS blocks before opening a receipt or calling GitHub and records a visible decision/event.
- Given restricted or local-only data, when an outbox message would be delivered through Slack or email, then BOSS blocks before opening a receipt or calling the external provider and records a visible decision/event.
- Given an unsupported boundary value, when the API receives it, then BOSS rejects the request with a visible validation error and does not create hidden work.

### Story GOV-004: Inspect Change Journal

As an auditor or release reviewer, I want code, deployment, infrastructure, self-improvement, and role-governance change artifacts summarized in one journal, so that I can inspect what changed before approving risky work.

Acceptance criteria:

- Given a durable change artifact such as `diff_manifest`, `review_packet`, `deployment_rollout`, or `role_scope_violation`, when the change journal is requested, then BOSS returns a normalized entry with change kind, summary, diff summary, files changed, rollback plan, data boundary, and artifact id.
- Given a reviewer opens a specific journal item, when the detail endpoint is requested with the correct project scope, then BOSS returns the full artifact content.
- Given a journal item is requested from the wrong project, when the detail endpoint is called, then BOSS returns `404` instead of leaking artifact content.

### Story GOV-005: Track Task Lineage

As a founder or operator, I want tasks to expose hierarchy, dependencies, owners, and due dates, so that initiative-to-task relationships and blockers are visible through API and UI.

Acceptance criteria:

- Given a task is created with a parent task, dependency task ids, owner role, and due date, when the API returns the task, then those fields are present in create, list, get, and audit export responses.
- Given a parent or dependency references an unknown task in the same project, when task creation is attempted, then BOSS rejects the request and does not silently create broken lineage.
- Given an owner role references an unknown role in the same project, when task creation is attempted, then BOSS rejects the request and records no hidden assignment.

### Story GOV-006: Prevent Role Drift

As VP Agents, I want RoleSpec scope checks to block out-of-scope skills and lower trust automatically, so that roles cannot drift into actions outside their charter.

Acceptance criteria:

- Given a role checks an action listed in its RoleSpec `allowed_skills`, when the scope check endpoint is called, then BOSS allows the action and records a `role.scope_allowed` event without changing trust.
- Given a role checks a skill matching `forbidden_skills`, when the scope check endpoint is called, then BOSS denies the action, creates a `role_scope_violation` artifact, emits visible events, increments scope/policy violation counters, lowers trust, and downgrades autonomy when possible.
- Given a role checks a skill absent from `allowed_skills`, when the scope check endpoint is called, then BOSS treats it as out of scope rather than silently allowing it.

### Story OPS-004: Deliver Governed Email

As an operator, I want outbound email to run through the same receipt-backed outbox as other comms, so that founder updates and approvals stay deterministic and auditable.

Acceptance criteria:

- Given an outbox message with an `email:` channel and working SMTP configuration, when delivery runs with a reservation, then BOSS creates a receipt and acknowledges the outbox message.
- Given email delivery is disabled or missing SMTP configuration, when delivery is attempted, then BOSS blocks visibly, releases the reservation, and records a decision instead of pretending email was sent.
- Given restricted or local-only data, when an email outbox message would be delivered, then BOSS blocks before opening a receipt or contacting SMTP.

### Story OPS-006: Process Decision Replies and Comms Cadence

As an operator, I want inbound decision replies and outbound delivery cadence to be deterministic, so that approvals and notifications do not disappear or spam recipients.

Acceptance criteria:

- Given an inbound message that clearly approves, rejects, revises, delegates, or executes a decision ID, when it is ingested, then BOSS applies the decision transition through the same permissioned service as the UI and marks the inbox message `actioned`.
- Given an inbound decision reply missing an ID/action or referencing an invalid transition, when it is ingested, then BOSS marks the inbox message `deadlettered` and records a visible `inbox_message` deadletter.
- Given quiet hours or per-channel throttling, when a non-urgent outbox message is queued, then BOSS defers the delivery job through `run_after` and emits `outbox.deferred`.
- Given an urgent outbox message, when it is queued during quiet hours or after throttle limits, then BOSS bypasses scheduling deferral while still requiring reservation, lease, and receipt before delivery.

### Story OPS-005: Govern Sandbox Infrastructure

As a release or platform operator, I want sandbox infrastructure provisioning to stay budgeted, approval-gated, and TTL-managed, so that BOSS can use real droplets without creating silent cost or control drift.

Acceptance criteria:

- Given a valid reservation and provisioning request, when infrastructure review is requested, then BOSS creates a durable request artifact and a pending approval decision before any provider call is attempted.
- Given an approved provisioning review, when real infrastructure execution is enabled, then BOSS queues or executes a receipt-backed `infra.droplet.create` action and records the resulting resource through the API.
- Given real infrastructure execution is disabled or unconfigured, when the review is approved, then BOSS creates a visible gate artifact or blocking decision instead of pretending the resource exists.
- Given restricted or local-only data, when droplet provisioning would target an external provider, then BOSS blocks before opening a receipt or contacting the provider and releases the reservation.
- Given an expired active resource TTL, when the janitor runs, then BOSS queues a visible termination job rather than silently deleting the resource.

## Agent Platform / Admin

### Story ADM-001: Route AI Capacity Safely

As an agent platform admin, I want BOSS to route tasks only through approved official APIs, CLIs, agents, or local models, so that provider limits and rules are respected.

Acceptance criteria:

- Given a source disallowed for a workload class, when routing is previewed, then BOSS denies that source and logs the reason.
- Given a source missing credentials, when routing is evaluated for runtime work, then BOSS fails closed.
- Given a personal subscription source and multi-tenant workload, when routing is attempted, then BOSS denies it unless provider policy explicitly allows it.

### Story ADM-002: Control Project Actors

As an admin, I want project-scoped member actors and permissions, so that API writes are not accepted from arbitrary caller-supplied names.

Acceptance criteria:

- Given a seeded project, when members are listed, then CEO, system, Metatron, VP Agents, release manager, and executive-console actors exist.
- Given a write request, when the actor lacks the required permission, then BOSS returns `403` and records `policy.denied`.
- Given a member-management request, when the actor has `member_manage`, then BOSS creates or updates the member and emits an event.

### Story ADM-003: Use Break-Glass Access Safely

As an admin, I want emergency access to require identity, reason, scope, TTL, event logs, and postmortem follow-up, so that exceptional access is controlled and reviewable.

Acceptance criteria:

- Given an authorized requester, when break-glass access is granted, then BOSS records target member, requester, reason, scope, granted permissions, TTL, events, and a postmortem task.
- Given a target member with an active unexpired grant, when they perform a granted action, then BOSS allows the action and emits `break_glass.used`.
- Given a revoked or expired grant, when the target member attempts the privileged action, then BOSS denies the action and keeps the grant visible through the API.

## Release / Operations

### Story OPS-001: Recover Worker Failures

As an operator, I want leased jobs to heartbeat, retry, and deadletter, so that no work disappears.

Acceptance criteria:

- Given an expired lease, when a worker polls, then the job can be reclaimed within the TTL and retry policy.
- Given a retryable failure with attempts remaining, when the worker reports failure, then the job becomes retryable and is scheduled after delay.
- Given exhausted attempts or terminal failure, when the worker reports failure, then BOSS creates a deadletter visible through API and console.

### Story OPS-002: Protect Self-Improvement

As a release manager, I want BOSS self-improvement to require proposal, tests, canary, approval, rollout, and rollback evidence, so that BOSS cannot silently modify itself.

Acceptance criteria:

- Given a self-improvement proposal missing impact, tests, canary scope, rollback plan, or metrics, when submitted, then BOSS rejects it.
- Given canary or rollout evidence, when recorded by an authorized actor, then BOSS persists the artifact and updates proposal state.
- Given a rolled-out proposal, when rollback is required, then BOSS records reason, metrics, evidence, and rollback artifact.

### Story OPS-003: Track Sandbox Deployment Rollouts

As a release manager, I want every sandbox deployment to produce staged rollout evidence, so that release readiness and rollback decisions are visible and auditable.

Acceptance criteria:

- Given a sandbox deployment is recorded, when the deployment succeeds and health verification passes or is explicitly skipped by configuration, then BOSS creates a completed deployment rollout record and artifact.
- Given post-deploy health verification fails, when BOSS records the deployment, then the rollout is blocked at the health stage and links to the blocking health/rollback decision.
- Given an operator queries rollout history, when `GET /v1/software-factory/rollouts` is called, then BOSS returns rollout status, current stage, deployment evidence, health evidence, and rollback decision references.
