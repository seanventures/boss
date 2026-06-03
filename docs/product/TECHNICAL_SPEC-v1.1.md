# BOSS Technical Specification v1.1

Status: Technical companion to `docs/product/PRD-v1.1.md`
Date: June 3, 2026

## Architecture

BOSS is a durable control plane with stateless execution workers, API-first product surfaces, and a governed Elastic Resource Plane.

Core planes:

- Control Plane: tenant state, canonical objects, policies, decisions, budgets, receipts, audit, API, and executive console.
- Execution Plane: pull-based jobs, leases, workers, retries, deadletters, and local/cloud runner coordination.
- Communications Plane: outbox/inbox, ACK, throttling, retries, deadletters, and communication receipts.
- Elastic Resource Plane / ProviderOps: provider accounts, adapters, inventory, billing, deployments, ChangeSets, resource receipts, reconciliation, drift, provider health, janitor, and autoscaling policy.
- Model Entitlement Broker / AI Capacity Router: AI capacity inventory, provider rules, rate limits, cost posture, routing logs, and fail-closed source selection.

OpenClaw is not a BOSS foundation or durable runtime. Older references to OpenClaw should be treated only as optional/legacy adapter inspiration. BOSS owns the durable kernel, policy, state, budgets, receipts, and observability.

## Resource Plane Kernel

The Resource Plane Kernel is a deterministic service/module in the BOSS control plane. It owns:

- Provider account records.
- Provider adapter registration.
- `SecretRef` lookup and local-runner credential delegation.
- Desired state for provider-managed resources.
- Read-only sync and reconciliation.
- ChangeSet planning, policy checks, quoting, approval gates, and execution.
- Resource receipts and dedupe.
- Cost events, commitments, and billing reconciliation.
- Provider health, rate-limit, and quota status.
- Drift reports and janitor proposals.

Agents may request typed skills and propose ChangeSets. They do not receive raw provider secrets and do not call providers directly.

## Provider Adapter Interface

Provider adapters are plugins behind the Resource Plane Kernel.

Required adapter capabilities:

- `test_connection(provider_account, secret_ref) -> ProviderHealth`
- `detect_permissions(provider_account) -> Capability[]`
- `sync_inventory(provider_account) -> ExternalResource[]`
- `sync_billing(provider_account, window) -> CostEvent[]`
- `quote(changeset) -> CostQuote`
- `dry_run(changeset) -> PlanArtifact`
- `apply(changeset, operation, idempotency_key) -> ResourceReceipt`
- `reconcile(provider_account, desired_state) -> ReconciliationRun`

Adapters must be idempotent where providers support it and must return provider references for receipts.

## Provider Connection and Auth Model

Provider connections are represented by `ProviderAccount`.

Credentials are never embedded in prompts, worker payloads, artifacts, or provider specs. The control plane stores only `SecretRef` records and metadata. Secret material lives in a configured vault, environment secret store, or approved local runner.

ProviderAccount flags:

- `read_only`
- `deploy_capable`
- `billing_readable`
- `destructive_actions_disabled`
- `disabled`

Provider accounts can be scoped to org, workspace, or project.

## Credential Vault and SecretRef

`SecretRef` is the canonical pointer to secret material.

Required fields:

- `id`
- `scope_type`
- `scope_id`
- `provider`
- `secret_kind`
- `storage_backend`
- `storage_ref`
- `rotation_due_at`
- `last_verified_at`
- `status`

Cloud-hosted BOSS may store only metadata while local runners hold actual provider secrets.

## Resource Sync and Reconciliation Workers

Read-only sync workers:

1. Lease `providerops.sync` job.
2. Load ProviderAccount and SecretRef through kernel.
3. Test connection and permissions.
4. Pull inventory and billing where allowed.
5. Normalize provider objects into `ExternalResource`.
6. Write `CostEvent`, `ProviderHealth`, and `ReconciliationRun`.
7. Emit events and deadletter failures.

Reconciliation workers compare provider inventory with BOSS desired state and produce `DriftReport` records.

## ChangeSet Executor

ChangeSet execution pipeline:

`draft -> quoted -> policy_checked -> budget_reserved -> pending_approval -> approved -> applying -> applied -> failed -> rolled_back -> deadlettered`

Execution rules:

- No provider write without ChangeSet.
- No spendy operation without BudgetReservation.
- No destructive action without explicit approval.
- No production DNS/deploy change without approval and rollback plan.
- Every provider write creates a ResourceReceipt.
- Failed provider calls become visible events, deadletters, or decision escalations.

## Cost Ledger and Reconciliation

BOSS tracks four cost layers:

- Estimated cost
- Reserved cost
- Observed cost
- Reconciled cost

Cost confidence values:

- `exact_call_metered`
- `provider_reported`
- `invoice_reconciled`
- `estimated_from_price_catalog`
- `unknown`

Cost attribution dimensions:

- org
- workspace
- project
- task
- role
- provider
- resource
- model
- environment
- receipt
- ChangeSet

## Provider Health and Drift Detection

ProviderHealth includes:

- provider API availability
- permission status
- rate-limit status
- quota/usage cap status
- billing access status
- failed call counts
- last successful sync
- current degraded reason

Drift classification:

- benign
- risky
- cost-impacting
- security-impacting
- production-impacting

Production-impacting drift never auto-remediates without approval.

## Database Tables, High-Level

Resource Plane Kernel tables should include:

- `provider_accounts`
- `provider_adapters`
- `secret_refs`
- `capabilities`
- `external_resources`
- `deployment_targets`
- `billing_accounts`
- `cost_events`
- `commitments`
- `changesets`
- `changeset_operations`
- `resource_receipts`
- `provider_health`
- `drift_reports`
- `resource_policies`
- `reconciliation_runs`

These tables should use org/workspace/project scope, stable IDs, timestamps, lifecycle states, data boundary classifications, and event emission on material changes.

## API Endpoints, Planned

These endpoints are planned product/API surfaces. They should not be added to the active OpenAPI runtime contract until implemented.

- `GET /v1/providers`
- `POST /v1/providers`
- `POST /v1/providers/{id}/test`
- `GET /v1/resources`
- `POST /v1/resources/sync`
- `GET /v1/costs`
- `GET /v1/changesets`
- `POST /v1/changesets`
- `POST /v1/changesets/{id}/approve`
- `POST /v1/changesets/{id}/apply`
- `GET /v1/deployments`
- `POST /v1/deployments`
- `GET /v1/provider-health`
- `GET /v1/drift-reports`

## Local Runner Integration

Local runners can host provider adapters and secrets for restricted data or customer-owned infrastructure. The cloud control plane stores metadata, health, receipts, and `SecretRef` only.

Local runner jobs remain leased through the same worker runtime. Provider calls still require ChangeSets, policy checks, budget reservations, approvals, and receipts.

## Terraform/OpenTofu Position

BOSS desired state is canonical in database records plus artifacts. Direct provider APIs are allowed in early versions through ChangeSets. Later versions should optionally export or sync Terraform/OpenTofu-style plans for teams that want infrastructure-as-code parity.

Terraform/OpenTofu is not the BOSS source of truth unless explicitly configured as a downstream export/sync target.

## Budget-Aware Autoscaling

Scheduler inputs:

- queue depth
- provider health
- current commitments
- budget headroom
- rate limits
- local runner capacity
- cloud spillover policy
- task priority

Scheduler outputs:

- scale local workers
- provision cloud workers
- drain workers
- enter degraded mode
- request budget increase decision

Budget Governor always wins over Autoscaler.
