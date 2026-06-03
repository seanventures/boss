# BOSS Implementation Status

Last updated: June 3, 2026
Release target: `v0.2.1`

This document summarizes the currently implemented BOSS runtime surfaces in this repository so the codebase, README, and product intent stay aligned.

## What is implemented now

- Canonical tenant bootstrap:
  - `orgs`
  - `workspaces`
  - `projects`
  - dogfood seed for `boss-dogfood`
- Core control-plane objects:
  - tasks
  - task hierarchy, dependencies, owners, and due dates
  - departments
  - policy records
  - plans
  - meetings
  - jobs and leases
  - decisions
  - artifacts
  - receipts
  - budget reservations and cost receipts
  - events
  - deadletters
  - health snapshots
- API-first control plane:
  - project discovery for runtime-scoped clients
  - CEO command intake
  - canonical department, policy, plan, and meeting CRUD/list surfaces
  - task and job inspection
  - change journal list/detail for diff, review, deployment, self-improvement, infrastructure, and role-governance artifacts
  - budget quote, reserve, release, and status
  - hierarchical budget allocations
  - budget dashboard forecast, burn-rate, time-to-cap, and top-cost-driver fields
  - infrastructure provisioning review, direct execution, queued execution, resource inspection, termination, and TTL janitor control
  - decision inbox and transitions
  - decision digest artifact creation with optional outbox draft handoff
  - artifact, receipt, event, and audit export access
  - replay bundle creation
  - Metatron status query
  - comms inbox and outbox
  - self-improvement proposal workflow
  - role and pack discovery/install
  - RoleSpec scope checks with drift events, trust changes, and automatic autonomy downgrade
  - agent-module catalog and install flow
  - entitlement source and routing-log visibility
  - project member directory and actor-scoped write authorization
- Worker runtime:
  - pull-based Postgres leasing
  - heartbeat and TTL enforcement
  - completion, failure, retry, reclaim, and deadletter handling
  - leased infrastructure create/delete job kinds
  - budget-state claim-time controls
- Budget governor behavior:
  - spend requires reservation
  - allocation-backed reservations cannot exceed active allocation availability
  - child allocations cannot exceed parent available budget
  - duplicate or blocked effects release budget
  - `orange` defers non-critical work
  - `red` halts non-`p0` work
  - `red` freezes higher-risk merge, deploy, and outbound comms effects
  - `red` freezes infrastructure provisioning before lease claim
- Software factory loop:
  - command -> plan artifact -> plan approval decision
  - approved plan -> reserved PR proposal job
  - installed agent module selected into queued proposal work
  - PR proposal -> receipt-backed GitHub draft PR
  - GitHub validation evidence captured into `test_report` and `review_packet`
  - optional receipt-backed local sandbox validation commands captured into `test_report` and `review_packet`
  - PR review approval -> merge gate or merge job, depending on policy
  - merge -> deployment review decision
  - deployment review approval -> deployment job, depending on policy
  - optional post-deploy health verification with blocking follow-on decision on failure
  - durable deployment rollout records with staged evidence and rollback/health-decision linkage
- Communications reliability:
  - outbox drafts, queueing, delivery, ACK, blocked, retrying, and deadlettered states
  - blocked delivery emits visible events and decision cards
  - outbox queue/delivery rejects cross-project message references
  - quiet-hours and per-channel throttling defers non-urgent outbox delivery jobs through `run_after`
  - urgent outbox delivery queue requests bypass quiet-hours/throttle deferral while preserving budget, lease, and receipt gates
  - deterministic inbound decision replies transition decisions through the decision service or deadletter visibly
  - Slack delivery remains disabled unless explicit policy and webhook config are present
  - SMTP email delivery remains disabled unless explicit policy and SMTP config are present
- Governance surfaces:
  - optional API-key authentication for `/v1/*` routes with fail-closed missing-key behavior
  - optional API-key principal binding so authenticated keys cannot impersonate another project actor
  - auth-session introspection through `GET /v1/auth/session`
  - break-glass access grants with requester identity, target member, reason, scope, TTL, temporary permissions, event logs, revocation, expiry, and mandatory postmortem task creation
  - Core Pack install for dogfood
  - packaged agent-module catalog, install flow, and role lineage
  - deterministic RoleSpec validation for pack roles and module-provided roles
  - deterministic SkillSpec validation for typed IO, cost, retry, permissions, data boundary, policy, and real-only mode
  - model entitlement broker with routing logs
  - data-boundary-aware routing that blocks restricted/local-only work from unapproved external model sources and records the boundary in routing decisions
  - data-boundary-aware GitHub effect gates that block restricted/local-only PR, merge, and deployment effects before receipts or external calls are opened
  - data-boundary-aware comms records and Slack/email delivery gates that block restricted/local-only messages before receipts or external calls are opened
  - data-boundary-aware infrastructure gates that block restricted/local-only provisioning before receipts or external calls are opened
  - runtime role governance with trust scoring, review artifacts, and autonomy-change records
  - autonomy ladder preflight checks that emit allowed/denied events before role-governed actions
  - project members seeded for CEO, system, Metatron, VP Agents, release manager, and executive-console actors
  - permission checks on command, task, decision, comms, health, audit, pack, module, entitlement, role-governance, and self-improvement writes
  - denied write attempts emit `policy.denied` events when the project can be resolved
  - self-improvement proposal, canary, rollout, and rollback records
  - fail-closed self-improvement evidence gates for tests, canary scope, rollback steps, metrics, and evidence refs
  - audit export and replay-bundle artifact generation
  - compliance evidence bundle generation for members, roles, policies, break-glass grants, decisions, receipts, reservations, cost receipts, deadletters, and compliance-relevant events
  - data-boundary classification persisted on tasks, jobs, artifacts, and receipts, with task-to-job/artifact inheritance and audit export visibility
- Executive console grounding:
  - runtime project selection instead of fixed single-project UI scope
  - active actor selection from the project member directory, passed through all console write actions
  - keyboard skip navigation and explicit accessible names for dynamic action controls
  - Metatron activity queries grounded in live event data
  - self-improvement canary, rollout, and rollback submission with required evidence payloads
  - API-driven agent marketplace visibility and module installation
  - entitlement readiness labels that distinguish active, credential-gated, adapter-ready, and registry-only sources

## Current end-to-end founder flow

The current founder-dogfood control loop is:

1. Submit a CEO command.
2. BOSS creates a task, initial job, and audit events.
3. A worker leases and completes planning work.
4. BOSS creates a plan artifact and blocking decision.
5. Approval of that decision reserves budget and queues a PR proposal job.
6. The queued proposal job resolves through the installed software-engineer module so prompt/runtime defaults come from the packaged module.
7. The proposal job creates a real sandbox PR against `seanheiney/boss-sandbox`.
8. BOSS captures GitHub check-run evidence into the test report and review packet.
9. Approval of the PR review decision only queues merge work if validation passed and merge policy is enabled.
10. Merge can create a deploy review decision.
11. Deployment can be recorded through the GitHub Deployments API when deploy policy is enabled, with optional post-deploy health verification when configured.

## Operational knobs already in use

- `BOSS_ENABLE_SANDBOX_MERGE`
- `BOSS_ENABLE_SANDBOX_DEPLOY`
- `BOSS_SANDBOX_MERGE_METHOD`
- `BOSS_SANDBOX_DEPLOY_ENVIRONMENT`
- `BOSS_SANDBOX_DEPLOY_HEALTHCHECK_URL`
- `BOSS_SANDBOX_DEPLOY_HEALTHCHECK_TIMEOUT_SECONDS`
- `BOSS_SANDBOX_CI_TIMEOUT_SECONDS`
- `BOSS_SANDBOX_CI_POLL_INTERVAL_SECONDS`
- `BOSS_SANDBOX_LOCAL_VALIDATION_ENABLED`
- `BOSS_SANDBOX_VALIDATION_REPO_PATH`
- `BOSS_SANDBOX_VALIDATION_COMMANDS`
- `BOSS_SANDBOX_VALIDATION_ALLOWED_COMMANDS`
- `BOSS_SANDBOX_VALIDATION_TIMEOUT_SECONDS`
- `BOSS_SANDBOX_VALIDATION_OUTPUT_LIMIT_CHARS`
- `BOSS_SANDBOX_VALIDATION_FETCH_REMOTE`
- `BOSS_DAILY_CAP_USD_MICROS`
- `BOSS_MONTHLY_CAP_USD_MICROS`
- `BOSS_BURST_CAP_USD_MICROS_PER_MIN`
- `BOSS_SOFTWARE_FACTORY_PR_BUDGET_USD_MICROS`
- `BOSS_COMMS_MESSAGE_BUDGET_USD_MICROS`
- `BOSS_COMMS_QUIET_HOURS_ENABLED`
- `BOSS_COMMS_QUIET_HOURS_START_UTC`
- `BOSS_COMMS_QUIET_HOURS_END_UTC`
- `BOSS_COMMS_THROTTLE_MAX_QUEUED_PER_HOUR`
- `BOSS_COMMS_THROTTLE_DEFER_MINUTES`
- `BOSS_EMAIL_REAL_SEND_ALLOWED`
- `BOSS_EMAIL_SMTP_HOST`
- `BOSS_EMAIL_SMTP_PORT`
- `BOSS_EMAIL_SMTP_USERNAME`
- `BOSS_EMAIL_SMTP_PASSWORD`
- `BOSS_EMAIL_SMTP_USE_TLS`
- `BOSS_EMAIL_SMTP_USE_SSL`
- `BOSS_EMAIL_FROM_ADDRESS`
- `BOSS_EMAIL_FROM_NAME`
- `BOSS_INFRA_REAL_PROVISION_ALLOWED`
- `BOSS_DIGITALOCEAN_TOKEN`
- `BOSS_DIGITALOCEAN_DEFAULT_IMAGE`
- `BOSS_DIGITALOCEAN_DEFAULT_TAGS`
- `BOSS_DIGITALOCEAN_VPC_UUID`
- `BOSS_DIGITALOCEAN_SSH_KEY_IDS`

## What is intentionally still constrained

- Identity is currently project-member based with optional API-key principal binding. Actor ids must resolve to active project members with the required permission, and bound API keys cannot impersonate another actor. External SSO/OIDC and delegated org-wide RBAC are not yet implemented.
- Local sandbox validation is implemented as allowlisted, no-shell commands in a configured sandbox clone. It is disabled by default and fails closed when enabled without repo path, commands, clean git state, or allowed executables.
- Deployment rollout tracking now records staged GitHub deployment, post-deploy health, release-readiness, and rollback/health-decision evidence. Real DigitalOcean provisioning exists for sandbox droplets, but it remains fail-closed unless `BOSS_INFRA_REAL_PROVISION_ALLOWED=true` and official credentials are configured.
- Real Slack/email delivery remains disabled unless policy and provider credentials explicitly permit it.
- Enterprise controls such as external SSO/OIDC and delegated org-wide RBAC are not complete.

## Local verification baseline

The current local release-grade baseline for this repo is:

- `make lint`
- `make test`
- `make build`
- `make smoke`

The latest passing local state at the time of this update was:

- `143` API/worker tests passed and `42` web tests passed
- production web build passed
- smoke passed
- OpenAPI parsed successfully

## Where to look next

- Product intent: [PRD-v1.0.md](/Volumes/DataSSD1/test/boss/docs/product/PRD-v1.0.md)
- Active contract: [api/openapi.yaml](/Volumes/DataSSD1/test/boss/api/openapi.yaml)
- Runtime overview: [README.md](/Volumes/DataSSD1/test/boss/README.md)
- Entitlement policy: [MODEL_ENTITLEMENT_BROKER.md](/Volumes/DataSSD1/test/boss/docs/product/MODEL_ENTITLEMENT_BROKER.md)
