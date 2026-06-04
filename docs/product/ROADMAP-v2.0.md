# BOSS Roadmap v2.0

Status: Product roadmap companion to `docs/product/PRD-v2.0.md`
Date: June 3, 2026

This roadmap describes intended maturity phases. It is not an implementation-status report.

## Phase 0 - Kernel / Local Dogfood

Goal: prove BOSS can run a local, durable, accountable command loop.

Scope:

- Domain objects.
- Tasks, decisions, policies, receipts, artifacts.
- Postgres-backed leases.
- Deadletters.
- Local auth.
- Local runner.
- Mission Control shell.
- Metatron status API.
- Simple budget governor.
- Basic Software Factory sandbox-real PR proposal and evidence path.
- PRD/spec export.

Exit criteria:

- Local install runs without hosted BOSS auth.
- Command-to-task-to-job chain is durable.
- Receipts and deadletters are visible.
- Tests cover lease, budget, receipt, and no-silent-drop basics.

## Phase 1 - BOSS Builds BOSS

Goal: dogfood BOSS as its first customer.

Scope:

- Spec-to-PR workflow.
- Branch/worktree isolation.
- Evidence bundles.
- Model Entitlement Broker.
- Local/subscription/API routing.
- Decision Inbox.
- Basic budget reservations.
- Receipts everywhere.
- Dogfood workspace.

Exit criteria:

- BOSS can produce a reviewable PR/evidence packet from a CEO command.
- Every spendy model/API action has BudgetReservation coverage.
- Every route writes route-decision evidence.

## Phase 2 - ProviderOps M0/M1

Goal: make external resources visible and safely changeable.

Scope:

- Read-only provider sync.
- Resource inventory.
- Cost ledger.
- ChangeSets.
- Receipted provider actuation.
- DigitalOcean, Cloudflare, Supabase, OpenAI, Anthropic MVP providers.

Exit criteria:

- At least three providers connect in read-only mode.
- Resources are labeled managed/unmanaged/orphaned.
- Provider writes require ChangeSet, BudgetReservation, approval where required, and ResourceReceipt.

## Phase 3 - Software Factory v1

Goal: make BOSS a reliable software-production department.

Scope:

- Code/test/review lanes.
- Browser/UI validation.
- CI integration.
- Release packets.
- Deployment registry.
- Staging deploys.

Exit criteria:

- Evidence bundles include tests, builds, diffs, review summary, receipts, and rollback.
- Staging deploys use DeploymentRun and ProviderOps records.

## Phase 4 - Cloud SaaS

Goal: make BOSS easy to adopt as a managed product.

Scope:

- BOSS Account Connect.
- Hosted auth.
- Managed control plane.
- Hosted worker pools.
- Managed connector gateway.
- Managed model gateway.
- Cloud billing.

Exit criteria:

- Cloud requires Account Connect.
- Community still runs without Account Connect.
- Managed worker and connector operations preserve BOSS receipts, budgets, and audit semantics.

## Phase 5 - Enterprise

Goal: make BOSS trusted in serious operating environments.

Scope:

- SSO, SCIM, SAML.
- RBAC/ABAC.
- Audit exports.
- Clustering.
- HA.
- Private workers.
- Enterprise ProviderOps.
- Compliance packs.
- Support/SLA.

Exit criteria:

- Attached and detached enterprise modes are defined.
- Private worker and private model gateway paths preserve local secret boundaries.
- Enterprise support and SLA boundaries are productized.

## Phase 6 - Marketplace and Sector Packs

Goal: turn BOSS into an extensible company-operations ecosystem.

Scope:

- Pack registry.
- Paid/free packs.
- Verified publishers.
- Private enterprise packs.
- Sector templates.
- Simulation/forecasting.

Exit criteria:

- Packs have trust metadata and install boundaries.
- Community can import/export local packs.
- Cloud/Enterprise can use Account Connect marketplace identity.
