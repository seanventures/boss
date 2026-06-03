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
  - PR proposal -> receipt-backed GitHub draft PR
  - GitHub validation evidence captured into `test_report` and `review_packet`
  - PR review approval -> merge gate or merge job, depending on policy
  - merge -> deployment review decision
  - deployment review approval -> deployment job, depending on policy
- Governance surfaces:
  - Core Pack install for dogfood
  - model entitlement broker with routing logs
  - self-improvement proposal, canary, rollout, and rollback records
  - audit export and replay-bundle artifact generation

## Current end-to-end founder flow

The current founder-dogfood control loop is:

1. Submit a CEO command.
2. BOSS creates a task, initial job, and audit events.
3. A worker leases and completes planning work.
4. BOSS creates a plan artifact and blocking decision.
5. Approval of that decision reserves budget and queues a PR proposal job.
6. The proposal job creates a real sandbox PR against `seanheiney/boss-sandbox`.
7. BOSS captures GitHub check-run evidence into the test report and review packet.
8. Approval of the PR review decision only queues merge work if validation passed and merge policy is enabled.
9. Merge can create a deploy review decision.
10. Deployment can be recorded through the GitHub Deployments API when deploy policy is enabled.

## Operational knobs already in use

- `BOSS_ENABLE_SANDBOX_MERGE`
- `BOSS_ENABLE_SANDBOX_DEPLOY`
- `BOSS_SANDBOX_MERGE_METHOD`
- `BOSS_SANDBOX_DEPLOY_ENVIRONMENT`
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
- Deployment currently records real GitHub deployment state, but it is not yet a full health-verified staged rollout system.
- Trust scoring, role drift enforcement, and autonomy ladder upgrades/downgrades are not yet runtime-enforced.
- Enterprise controls such as SSO, break-glass flows, and multi-user admin governance are not complete.

## Local verification baseline

The current local release-grade baseline for this repo is:

- `make lint`
- `make test`
- `make smoke`

The latest passing local state at the time of this update was:

- `71` tests passed
- smoke passed
- OpenAPI parsed successfully

## Where to look next

- Product intent: [PRD-v1.0.md](/Volumes/DataSSD1/test/boss/docs/product/PRD-v1.0.md)
- Active contract: [api/openapi.yaml](/Volumes/DataSSD1/test/boss/api/openapi.yaml)
- Runtime overview: [README.md](/Volumes/DataSSD1/test/boss/README.md)
- Entitlement policy: [MODEL_ENTITLEMENT_BROKER.md](/Volumes/DataSSD1/test/boss/docs/product/MODEL_ENTITLEMENT_BROKER.md)
