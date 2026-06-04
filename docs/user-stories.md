# BOSS User Stories and Acceptance Criteria

Last updated: June 3, 2026

These stories are derived from `docs/product/PRD-v2.0.md`, `docs/product/TECHNICAL_SPEC-v2.0.md`, `reference/boss_build_spec_pack/docs/CODEX_RUNBOOK.md`, `packages/specpack/workflows/ceo_command_to_pr.yaml`, `packages/specpack/workflows/providerops_readonly_sync.yaml`, `packages/specpack/workflows/changeset_apply_with_receipts.yaml`, `packages/specpack/workflows/deployment_run.yaml`, `packages/specpack/workflows/model_entitlement_routing.yaml`, `packages/specpack/policies/default_safety.yaml`, `packages/specpack/policies/resource_plane_policy.yaml`, `packages/specpack/policies/budget_governor_policy.yaml`, and `packages/specpack/policies/model_entitlement_policy.yaml`.

## Founder / CEO

### Story CEO-001: Issue Outcome Commands

As a founder/CEO, I want to submit a natural-language outcome, so that BOSS turns it into structured, auditable work.

Acceptance criteria:

- Given an authenticated API client and active project actor, when the CEO submits a command, then BOSS creates a task, planning job, command artifact, and event.
- Given an unknown or unauthorized actor, when the command is submitted, then BOSS rejects it and emits a visible `policy.denied` event when the project exists.
- Given the API or database is unavailable, when the console loads, then it shows a visible blocked state instead of pretending work was accepted.

### Story SEC-001: Authenticate API Clients

As an operator, I want BOSS API access protected by API keys or OIDC bearer JWTs, so that customer and console clients can use the API without bypassing project-member permissions.

Acceptance criteria:

- Given API-key auth is required, when a `/v1/*` request lacks a valid configured key, then BOSS rejects it while leaving `/health` public.
- Given API-key principal binding is configured, when a valid key tries to act as another member, then BOSS rejects the write before business logic runs.
- Given OIDC auth is enabled and configured with issuer, audience, and JWKS URL, when a signed bearer JWT resolves to a project member through claim or subject mapping, then BOSS accepts transport authentication and applies the same actor permission checks as API keys.
- Given OIDC auth is enabled without issuer, audience, or JWKS config, when a `/v1/*` request presents a bearer token, then BOSS fails closed with a visible configuration error.
- Given an OIDC token cannot resolve to a project member, when a write action claims an actor, then BOSS rejects the request before the action executes.
- Given an org-level delegate has explicit permissions, when they act in any project under the same org, then BOSS authorizes them through `org_members` without duplicating a project member row.
- Given an org-level delegate lacks the required permission or the target project belongs to another org, when they attempt a write action, then BOSS rejects the request before the action executes.

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
- Given a module declares required marketplace entitlements, when the project lacks those entitlements, then BOSS denies installation, emits `agent_module.install_denied`, and records the entitlement result.
- Given the project has the required entitlement, when the module is installed, then BOSS records an install governance record with entitlement result and rollback snapshot.
- Given a module install is updated, when the previous install is replaced, then BOSS preserves a rollback snapshot with version, config, and spec.
- Given a module has a rollback snapshot, when rollback is requested by an authorized actor, then BOSS restores the prior spec/config, records `agent_module.rolled_back`, and keeps role lineage auditable.
- Given an installed module is no longer allowed or desired, when uninstall is requested by an authorized actor, then BOSS deactivates the module, restores the base RoleSpec where available, records `agent_module.uninstalled`, and keeps a snapshot for rollback.
- Given a governed job kind, when runtime behavior is resolved, then BOSS uses installed module configuration instead of hidden service defaults.
- Given a default dogfood project is seeded, when BOSS installs initial agent modules, then Metatron, COO, CAO, CTO, PM, QA, Release Manager, Program Manager, VP Agents, ProviderOps Manager, FinOps Sentinel, Resource Janitor, Infra Steward, Deployment Manager, Secrets Steward, Model Spend Analyst, and Software Engineer modules are active.
- Given a worker leases an `agent.*` job, when the job kind maps to an installed module, then the payload includes the resolved module, role, prompt checklist, BOSS invariants, skill defaults, and runtime configuration before execution.
- Given an `agent.*` job completes, when the worker returns a brief, then BOSS stores an `agent_brief` artifact and emits `agent.brief_created`.
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

## ProviderOps / Elastic Resource Plane

### Story RPL-001: Connect Providers Safely

As a ProviderOps Manager, I want provider accounts connected through scoped adapters and SecretRefs, so that BOSS can inspect provider resources without exposing raw secrets to agents.

Acceptance criteria:

- Given provider credentials are registered, when BOSS tests the connection, then it stores only a `SecretRef`, detects capabilities and missing permissions, and emits auditable provider-access events.
- Given a provider is marked read-only or destructive-actions-disabled, when a write or destructive action is requested, then policy blocks the action before any provider call.
- Given an agent needs provider data, when it requests access, then it receives typed Resource Plane outputs rather than raw credentials.

### Story RPL-002: Inventory External Resources

As a founder or operator, I want BOSS to discover and normalize external resources across providers, so that unmanaged, orphaned, costly, or production-critical assets are visible.

Acceptance criteria:

- Given connected DigitalOcean, Cloudflare, Supabase, OpenAI, or Anthropic providers, when read-only sync runs, then BOSS records normalized `ExternalResource`, `BillingAccount`, `CostEvent`, `ProviderHealth`, and `ReconciliationRun` objects.
- Given a discovered resource, when the Resource Explorer renders, then it shows ownership classification, project/task/deployment links, health, last seen timestamp, estimated or actual cost, provider, environment, and owner role where known.
- Given a resource is not linked to BOSS desired state, when reconciliation completes, then it is classified as discovered unmanaged, orphaned, external/manual, or managed by BOSS.

### Story RPL-003: Govern Provider ChangeSets

As a CEO or delegated approver, I want provider changes represented as ChangeSets with quotes, approvals, and rollback plans, so that cloud, DNS, database, and model-provider changes cannot bypass governance.

Acceptance criteria:

- Given an agent proposes a provider write, when BOSS models the request, then it creates a `ChangeSet` containing resource diffs, risk tier, cost quote, budget reservation requirement, approvals needed, rollback plan, and expected receipts.
- Given the ChangeSet is destructive or production-affecting, when approval is missing, then BOSS holds the action and does not call the provider.
- Given a ChangeSet is approved and budget-reserved, when a worker applies it, then each provider call creates a `ResourceReceipt` with a dedupe key and reconciliation verifies the resulting provider state.
- Given an approved Cloudflare DNS record upsert ChangeSet with valid credentials, when BOSS applies it, then the Resource Plane Kernel calls the official Cloudflare API adapter, marks the Receipt and ResourceReceipt succeeded, and links the managed DNS resource to the receipt.
- Given an approved Cloudflare R2 bucket creation ChangeSet with valid credentials and an active BudgetReservation, when BOSS applies it, then the Resource Plane Kernel calls the official Cloudflare R2 API, writes Receipt and ResourceReceipt records, creates an observed CostEvent, and links the managed bucket resource to the receipt.
- Given an approved DigitalOcean droplet create or destroy ChangeSet with valid credentials and explicit real-provisioning policy, when BOSS applies it, then the Resource Plane Kernel uses the official DigitalOcean adapter, writes a ResourceReceipt, and updates the managed resource inventory.
- Given an approved Supabase project creation ChangeSet with valid Management API credentials, an active BudgetReservation, and an env-backed database password SecretRef, when BOSS applies it, then the Resource Plane Kernel calls the official Supabase project-create endpoint, writes Receipt and ResourceReceipt records, creates an observed CostEvent, links the managed project resource, and never persists the raw database password.
- Given an approved destructive Supabase project pause ChangeSet with valid credentials and rollback instructions, when BOSS applies it, then the Resource Plane Kernel calls the official Supabase pause endpoint, writes Receipt and ResourceReceipt records, and updates the managed project resource to a paused state.
- Given Cloudflare DNS credentials are missing, when an approved DNS ChangeSet is applied, then BOSS records failed Receipt and ResourceReceipt records, marks provider health degraded, creates a deadletter, and does not call the provider adapter.

### Story RPL-004: Track Provider Costs

As a FinOps Sentinel, I want provider costs reconciled by confidence level, so that estimates are not presented as exact spend and budget governance can act on real provider pressure.

Acceptance criteria:

- Given provider billing APIs return exact or reported spend, when cost sync runs, then BOSS records cost events with `exact_call_metered`, `provider_reported`, or `invoice_reconciled` confidence.
- Given exact billing is unavailable, when BOSS estimates spend, then the cost event is labeled `estimated_from_price_catalog` or `unknown` and is never displayed as exact.
- Given billing-readable DigitalOcean, OpenAI, or Anthropic provider accounts have valid env-backed credentials, when read-only ProviderOps sync runs, then BOSS ingests official billing/cost report data into duplicate-safe `CostEvent` records linked to `BillingAccount` records and confidence labels.
- Given a provider account is not billing-readable, when read-only ProviderOps sync runs, then BOSS skips billing endpoints and records only supported inventory/reconciliation evidence.
- Given a billing-readable provider has no implemented official billing endpoint, when read-only ProviderOps sync runs, then BOSS marks the reconciliation failed, records provider health degradation, and creates a visible deadletter rather than inventing a cost estimate.
- Given a spendy ProviderOps ChangeSet applies successfully before billing reconciliation, when the Resource Plane records the result, then BOSS creates an observed `CostEvent` linked to the Receipt and ResourceReceipt with `estimated_from_price_catalog` confidence.
- Given observed spend exceeds policy thresholds, when the ledger updates, then BOSS emits an anomaly event and can apply budget kill switches such as read-only provider mode, local-only mode, or freeze provisioning.
- Given observed provider spend crosses the project freeze threshold, when the CostEvent is recorded, then BOSS creates a `CostAnomaly`, switches the provider account to read-only mode, creates a decision card, and blocks subsequent provider write ChangeSets until reviewed.

### Story RPL-005: Manage Deployments and Drift

As a Deployment Manager, I want deployments and drift reports connected to provider resources, DNS, health checks, receipts, and rollback points, so that release operations are auditable and recoverable.

Acceptance criteria:

- Given an app has deployment targets, when a deployment run is planned, then BOSS links repo, environments, resources, domains, SecretRefs, health checks, release receipts, and rollback points.
- Given a deployment target requires a provider write, when a deployment run is planned, then BOSS creates a release-packet artifact and an approval-gated ChangeSet rather than calling the provider directly.
- Given an approved Cloudflare Worker deployment ChangeSet with valid credentials and a deployable Worker script descriptor, when BOSS applies it, then the Resource Plane Kernel calls the official Cloudflare Workers API, writes Receipt and ResourceReceipt records, creates an observed CostEvent when a reservation exists, links the managed Worker resource, and marks the DeploymentRun deployed.
- Given an approved Git-connected Cloudflare Pages deployment ChangeSet with valid credentials and Pages project metadata, when BOSS applies it, then the Resource Plane Kernel calls the official Cloudflare Pages deployment API, writes Receipt and ResourceReceipt records, creates an observed CostEvent when a reservation exists, links the managed Pages deployment resource, and marks the DeploymentRun deployed.
- Given an approved DigitalOcean App Platform deployment ChangeSet with valid credentials and an active BudgetReservation, when BOSS applies it, then the Resource Plane Kernel calls the official DigitalOcean adapter, writes Receipt and ResourceReceipt records, creates an observed CostEvent, links the managed app deployment resource, and marks the DeploymentRun deployed.
- Given an approved Supabase migration deployment ChangeSet with valid Management API credentials and migration SQL, when BOSS applies it, then the Resource Plane Kernel calls the official Supabase migrations endpoint, writes Receipt and ResourceReceipt records, creates an observed CostEvent when a reservation exists, links the managed migration resource, and marks the DeploymentRun deployed.
- Given a deployment run has health checks, when health validation runs, then BOSS performs real HTTP health checks, writes a health-report artifact, and marks the run healthy or rollback-pending.
- Given a health check fails, when BOSS records the result, then it creates rollback evidence and a decision card instead of silently treating the deploy as successful.
- Given provider inventory diverges from BOSS desired state, when drift detection runs, then BOSS classifies drift as benign, risky, cost-impacting, security-impacting, or production-impacting and proposes remediation.
- Given production resources would be deleted or production DNS would change, when a remediation is proposed, then BOSS requires explicit approval and rollback plan before actuation.

### Story RPL-006: Propose Resource Janitor Cleanups

As a Resource Janitor, I want orphaned or expired non-production resources converted into cleanup ChangeSets, so that waste can be reduced without silent deletion or provider bypass.

Acceptance criteria:

- Given orphaned or TTL-expired non-production resources exist in the Resource Inventory Graph, when the janitor runs, then BOSS creates a `resource_janitor_report` artifact with candidates, skipped resources, savings estimates, cost confidence, and linked cleanup ChangeSets.
- Given a cleanup action would be destructive, when the janitor proposes it, then the resulting ChangeSet requires explicit approval, rollback steps, and a ResourceReceipt before any provider actuation can occur.
- Given a candidate resource is production, unsupported, read-only, destructive-disabled, or already covered by an open cleanup ChangeSet, when the janitor runs, then BOSS skips it with a visible reason instead of deleting or duplicating proposals.

### Story RPL-007: Install ProviderOps Operating Roles

As a founder/operator, I want the core company pack to install ProviderOps Manager, FinOps Sentinel, Infra Steward, Deployment Manager, Resource Janitor, Secrets Steward, and Model Spend Analyst roles, so that provider inventory, cost control, deployment safety, secret boundaries, and cleanup proposals have accountable owners from workspace bootstrap.

Acceptance criteria:

- Given the `core_v1` company pack is installed, when BOSS seeds or installs the pack into a project, then ProviderOps Manager, FinOps Sentinel, Infra Steward, Deployment Manager, Resource Janitor, Secrets Steward, and Model Spend Analyst roles are created with complete RoleSpec contracts.
- Given the default agent-module installer runs, when those ProviderOps roles have matching modules, then each module exposes contained prompts, job kinds, skill defaults, and worker runtime configuration.
- Given a ProviderOps role performs work, when it needs provider evidence, then it must use typed Resource Plane skills and references instead of raw provider secrets or direct provider API calls.
- Given provider writes, destructive cleanup, budget increases, or production DNS/deploy changes are proposed, when a ProviderOps role reviews them, then the role must escalate through ChangeSets, approvals, BudgetReservations, and ResourceReceipts instead of executing directly.

### Story RPL-008: Plan Budget-Aware Capacity Changes

As a ProviderOps Manager, I want BOSS to scale local worker capacity and propose cloud spillover only when budget, provider health, and approval policy allow it, so that queue pressure is handled without bypassing the Budget Governor.

Acceptance criteria:

- Given queued jobs exceed current worker-pool capacity and local capacity is available, when the autoscaler runs, then BOSS records a `ScalePlan`, updates local desired capacity, and emits worker-pool and autoscaler events.
- Given the project is in red budget state, when the autoscaler runs, then BOSS enters degraded-mode recommendations, drains desired capacity to the safe minimum, and creates a decision instead of scaling up.
- Given cloud spillover is enabled, queue pressure exists, and budget headroom is available, when the autoscaler runs against a cloud worker pool, then BOSS creates a BudgetReservation-backed, approval-required ChangeSet and does not call the provider directly.

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
- Given a public subscription source uses OpenAI Codex, Claude Code, or Grok Build, when no official OAuth/CLI session is connected, then BOSS denies routing and records the connection reason.
- Given an entitled operator records a verified official session reference, when the source is connected through the API, then BOSS stores only the connection reference, scopes, non-secret metadata, observed capacity, and `entitlement.source_connected` event.
- Given a connected official session reference is revoked, when the source is disconnected through the API, then BOSS marks credentials absent, resets capacity to unknown, emits `entitlement.source_disconnected`, and subsequent routes fail closed.
- Given an official OAuth/CLI session is connected for a subscription source, when owner-sandbox work is routed, then BOSS may select it and records the auth method and connection reference in the routing decision.
- Given a source is exhausted, rate-limited, blocked, disabled, suspended, unavailable, or has zero remaining counters, when routing is evaluated, then BOSS rejects it with a capacity-specific reason and reset/observation metadata where available.
- Given a personal subscription source and multi-tenant workload, when routing is attempted, then BOSS denies it unless provider policy explicitly allows it.

### Story ADM-005: Audit Entitlement Pools and Capacity Receipts

As an agent platform admin, I want model adapters, entitlement pools, capacity probes, and route decisions exposed through stable APIs, so that UI surfaces and customers can inspect AI capacity without relying on stitched-together logs.

Acceptance criteria:

- Given the model provider registry is loaded, when model adapters are listed, then BOSS returns each official API, official CLI, official coding agent, and approved local model adapter with auth method, workload rules, runtime status, and provider-policy notes.
- Given a project has entitlement sources, when entitlement pools are listed, then BOSS returns normalized pool records with capacity state, routing modes, allowed workload classes, and budget-policy reference.
- Given an entitled actor records observed pool capacity, when a capacity probe is submitted, then BOSS updates the pool capacity, writes a `CapacityReceipt`, emits `capacity.receipt_recorded`, and stores no raw provider secret.
- Given the same capacity probe `dedupe_key` is submitted again, when BOSS processes the request, then it returns the prior `CapacityReceipt`, does not create a second receipt, and does not overwrite capacity state.
- Given route simulation is requested, when the Model Entitlement Broker evaluates sources, then BOSS writes and returns a canonical `ModelRouteDecision` with selected pool, rejected-source reasons, capacity snapshot, budget state, and policy result.

### Story ADM-003: Run Community Without Hosted Auth

As a self-hosted Community operator, I want BOSS to run with local auth and no BOSS Account Connect dependency, so that I can trust and evaluate the open-core product independently.

Acceptance criteria:

- Given a Community install, when no Account Connect org is linked, then local auth, local runner, core objects, Decision Inbox, basic Budget Governor, and basic Model Entitlement Broker still work.
- Given a Community install, when `GET /v1/edition` is called, then BOSS reports `status=active`, `account_connect_required=false`, and `local_auth_allowed=true`.
- Given a Community install, when `GET /v1/entitlements` is called, then local edition-pack capabilities are returned without requiring hosted BOSS auth.
- Given a Community install, when Cloud-only capabilities are requested, then BOSS explains the missing entitlement without breaking local operation.
- Given Community packaging is documented, when licensing language is shown, then restricted or commercial modules are not described as open source without legal review.

### Story ADM-004: Use Account Connect for Cloud Identity

As a Cloud customer admin, I want BOSS Account Connect to manage login, org identity, billing, license entitlements, team invites, and marketplace identity, so that managed Cloud operation is centralized and auditable.

Acceptance criteria:

- Given BOSS Cloud is enabled, when an org is created, then Account Connect owns cloud identity and license entitlement state.
- Given BOSS Cloud is enabled and no Account Connect org is linked, when `GET /v1/edition` is called, then BOSS reports `blocked_account_connect_required` instead of silently allowing Cloud-only behavior.
- Given an authorized ProviderOps manager links Account Connect, when `POST /v1/account-connect/link` succeeds, then BOSS persists the hosted org reference, refreshes account-connect entitlement state, and emits `account_connect.linked`.
- Given Account Connect sync is requested without `BOSS_ACCOUNT_CONNECT_URL`, when `POST /v1/account-connect/sync` runs, then BOSS fails closed, returns a visible error, and emits `account_connect.sync_denied`.
- Given Account Connect sync is configured, when the hosted service returns valid entitlements, then BOSS upserts local license entitlement rows and emits `account_connect.synced`.
- Given Account Connect is unlinked, when `POST /v1/account-connect/unlink` succeeds, then BOSS marks dependent Account Connect entitlements detached and emits `account_connect.unlinked`.
- Given a team invite is requested without an active Account Connect org, when `POST /v1/account-connect/team-invites` runs, then BOSS denies visibly and does not pretend an invite was sent.
- Given a team invite or billing portal session requires a hosted write, when the hosted adapter is configured, then BOSS opens a `Receipt`, calls the configured Account Connect endpoint, records the hosted reference, and emits an audit event.
- Given a team invite or billing portal session requires a hosted write and `BOSS_ACCOUNT_CONNECT_URL` is missing, when the request runs, then BOSS creates a blocked record with a failed receipt and emits a visible event.
- Given a Cloud user installs a marketplace pack, when the install starts, then publisher identity and entitlement checks are resolved through Account Connect.
- Given Enterprise is detached, when Account Connect is unavailable, then private license and identity flows remain explicitly supported.

### Story ADM-007: Operate Commercial State From Mission Control

As a founder, Cloud admin, or enterprise operator, I want edition, Account Connect, license entitlements, enterprise posture, and SCIM provisioning visible in Mission Control, so that commercial and trust controls are not hidden behind undocumented API calls.

Acceptance criteria:

- Given the commercial API is available, when Mission Control loads, then it shows edition status, Account Connect requirement/status, local-vs-hosted auth posture, license candidate, and active license entitlements.
- Given Account Connect is not linked, when an authorized actor submits a hosted org ID, then Mission Control calls the Account Connect link API and refreshes the panel from the control plane.
- Given Account Connect is linked, when an authorized actor requests entitlement sync or unlink, then Mission Control calls the corresponding API and relies on audit events from the backend.
- Given Enterprise cluster posture exists, when Mission Control loads, then it shows deployment mode, Account Connect mode, worker strategy, HA/data residency, support tier, compliance packs, and blocking gaps.
- Given enterprise identity-provider records exist, when Mission Control loads, then it shows OIDC, SAML, and SCIM provider status, SecretRefs, and blocking gaps.
- Given an authorized actor upserts SCIM groups or users, when the form is submitted, then Mission Control calls the enterprise SCIM API and active users are represented through the same org-member RBAC directory.

### Story ADM-006: Govern Enterprise Trust And Clustering Posture

As an enterprise admin, I want BOSS to record enterprise deployment mode, identity controls, audit posture, private worker strategy, and support/SLA commitments, so that enterprise readiness gaps are visible before production rollout.

Acceptance criteria:

- Given an unauthorized actor, when they attempt to create an enterprise cluster record, then BOSS denies the write and emits a visible policy-denial event.
- Given an Enterprise Cloud cluster has SSO/OIDC or SAML, SCIM, audit exports, retention, and SLA metadata configured, when it is saved, then BOSS records it as active with no blocking gaps.
- Given complete active OIDC and SCIM identity-provider records exist, when an Enterprise Cloud cluster is saved without manual identity flags, then BOSS treats those provider records as satisfying the corresponding identity controls.
- Given an operator marks an incomplete SAML/OIDC/SCIM provider active, when BOSS saves it, then BOSS stores the record as blocked with explicit blocking gaps instead of silently treating it as configured.
- Given a detached self-hosted enterprise cluster lacks SSO/SAML, SCIM, private worker pools, or compliance packs, when it is saved, then BOSS marks it blocked and lists each missing control.
- Given enterprise cluster state changes, when the write succeeds, then BOSS emits `enterprise.cluster_upserted` with deployment mode, Account Connect mode, status, and blocking gaps.
- Given enterprise identity-provider state changes, when the write succeeds, then BOSS emits `enterprise.identity_provider_upserted` with provider type, status, and blocking gaps.
- Given an enterprise admin maps a SCIM group to BOSS permissions, when the group is saved, then BOSS emits `enterprise.scim_group_upserted`.
- Given an active SCIM user belongs to a mapped group, when the user is saved, then BOSS creates or updates an org member with the merged explicit role and permission grants.
- Given a SCIM user is deactivated, when the user is saved, then BOSS removes the user's effective org-member access without deleting the audit record.

### Story ADM-002: Control Project Actors

As an admin, I want project-scoped and org-scoped member actors and permissions, so that API writes are not accepted from arbitrary caller-supplied names.

Acceptance criteria:

- Given a seeded project, when members are listed, then CEO, system, Metatron, VP Agents, release manager, and executive-console actors exist.
- Given an authorized admin, when an org delegate is created, then the delegate can act across projects in the same org only with explicitly granted permissions.
- Given the executive console loads, when org delegates are available to the selected actor, then they are visible in the actor switcher and Org Delegation management panel.
- Given a write request, when the actor lacks the required permission, then BOSS returns `403` and records `policy.denied`.
- Given a member-management request, when the actor has `member_manage`, then BOSS creates or updates the member and emits an event.

### Story ADM-003: Use Break-Glass Access Safely

As an admin, I want emergency access to require identity, reason, scope, TTL, event logs, and postmortem follow-up, so that exceptional access is controlled and reviewable.

Acceptance criteria:

- Given an authorized requester, when break-glass access is granted, then BOSS records target member, requester, reason, scope, granted permissions, TTL, events, and a postmortem task.
- Given the target is an org delegate for the project org, when break-glass access is granted, then BOSS resolves the target through the same member directory fallback used by runtime permission checks.
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
