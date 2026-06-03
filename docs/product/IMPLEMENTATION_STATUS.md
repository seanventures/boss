# BOSS Implementation Status

Last updated: June 2, 2026
Release target: `v0.2.0`

This document summarizes the currently implemented BOSS runtime surfaces in this repository so the codebase, README, and product intent stay aligned.

## What is implemented now

- Canonical tenant bootstrap:
  - `orgs`
  - `workspaces`
  - `projects`
  - dogfood seed for `boss-dogfood`
- Core control-plane objects:
  - tasks
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
  - task and job inspection
  - budget quote, reserve, release, and status
  - decision inbox and transitions
  - artifact, receipt, event, and audit export access
  - replay bundle creation
  - Metatron status query
  - comms inbox and outbox
  - self-improvement proposal workflow
  - role and pack discovery/install
  - agent-module catalog and install flow
  - entitlement source and routing-log visibility
- Worker runtime:
  - pull-based Postgres leasing
  - heartbeat and TTL enforcement
  - completion, failure, retry, reclaim, and deadletter handling
  - budget-state claim-time controls
- Budget governor behavior:
  - spend requires reservation
  - duplicate or blocked effects release budget
  - `orange` defers non-critical work
  - `red` halts non-`p0` work
  - `red` freezes higher-risk merge, deploy, and outbound comms effects
- Software factory loop:
  - command -> plan artifact -> plan approval decision
  - approved plan -> reserved PR proposal job
  - installed agent module selected into queued proposal work
  - PR proposal -> receipt-backed GitHub draft PR
  - GitHub validation evidence captured into `test_report` and `review_packet`
  - PR review approval -> merge gate or merge job, depending on policy
  - merge -> deployment review decision
  - deployment review approval -> deployment job, depending on policy
  - optional post-deploy health verification with blocking follow-on decision on failure
- Governance surfaces:
  - Core Pack install for dogfood
  - packaged agent-module catalog, install flow, and role lineage
  - model entitlement broker with routing logs
  - runtime role governance with trust scoring, review artifacts, and autonomy-change records
  - self-improvement proposal, canary, rollout, and rollback records
  - fail-closed self-improvement evidence gates for tests, canary scope, rollback steps, metrics, and evidence refs
  - audit export and replay-bundle artifact generation
- Executive console grounding:
  - runtime project selection instead of fixed single-project UI scope
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
- `BOSS_DAILY_CAP_USD_MICROS`
- `BOSS_MONTHLY_CAP_USD_MICROS`
- `BOSS_BURST_CAP_USD_MICROS_PER_MIN`
- `BOSS_SOFTWARE_FACTORY_PR_BUDGET_USD_MICROS`
- `BOSS_COMMS_MESSAGE_BUDGET_USD_MICROS`

## What is intentionally still constrained

- Identity, membership, and RBAC are not yet implemented. Approval actor names are still caller-supplied.
- The software factory captures real GitHub validation evidence, but it does not yet execute arbitrary sandbox test commands itself.
- Deployment can now run a real post-deploy HTTP health check when `BOSS_SANDBOX_DEPLOY_HEALTHCHECK_URL` is configured, but it is not yet a full health-verified staged rollout system.
- Enterprise controls such as SSO, break-glass flows, and multi-user admin governance are not complete.

## Local verification baseline

The current local release-grade baseline for this repo is:

- `make lint`
- `make test`
- `make smoke`

The latest passing local state at the time of this update was:

- `83` tests passed
- smoke passed
- OpenAPI parsed successfully

## Where to look next

- Product intent: [PRD-v1.0.md](/Volumes/DataSSD1/test/boss/docs/product/PRD-v1.0.md)
- Active contract: [api/openapi.yaml](/Volumes/DataSSD1/test/boss/api/openapi.yaml)
- Runtime overview: [README.md](/Volumes/DataSSD1/test/boss/README.md)
- Entitlement policy: [MODEL_ENTITLEMENT_BROKER.md](/Volumes/DataSSD1/test/boss/docs/product/MODEL_ENTITLEMENT_BROKER.md)
