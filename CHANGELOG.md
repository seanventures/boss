# Changelog

All notable changes to BOSS are documented in this file.

## v0.4.0 - 2026-06-03

This release adds the commercial, Account Connect, enterprise-governance, SCIM, and entitlement-gated agent-module runtime surfaces that make PRD v2.0 executable beyond documentation.

### Added

- Added the first Enterprise self-hosted Kubernetes package with container Dockerfiles, API/web/worker workloads, bootstrap Job, HPA/PDB guardrails, shared artifact PVC, secret-template guardrails, and a manifest validation command/test.
- Added Community, Cloud, Enterprise Cloud, and Enterprise Self-hosted edition introspection with fail-closed Cloud Account Connect requirements and local Community mode that works without hosted BOSS auth.
- Added BOSS Account Connect link, unlink, and hosted entitlement sync APIs with visible denied events when hosted credentials or connection state are missing.
- Added license entitlement records and runtime entitlement checks that distinguish edition capabilities from active/trial hosted entitlements.
- Added enterprise cluster inventory with trust metadata for SSO, SCIM, RBAC/ABAC, audit export, private workers, managed ProviderOps, compliance packs, support plans, SLA tier, and detached-license state.
- Added SCIM group and user provisioning records that map active SCIM users into effective org-member access and remove access on deactivation.
- Added entitlement-gated packaged agent module installs with install governance records, denial audit events, rollback snapshots, and private enterprise module coverage.
- Added OpenAPI, TypeScript API client, specpack objects, workflows, policies, user stories, and traceability coverage for the new commercial and enterprise runtime surfaces.

## v0.3.0 - 2026-06-03

This release ships the Elastic Resource Plane / ProviderOps module as core BOSS infrastructure.

### Added

- Added BOSS PRD v2.0 - Ultimate Product Spec. Added open-core commercial strategy, Community/Cloud/Enterprise editions, BOSS Account Connect, Elastic Resource Plane / ProviderOps, ProviderOps roles, ChangeSets, resource receipts, billing/cost ledger, Resource Janitor, drift detection, Budget-aware Autoscaler, Model Entitlement Broker refinements, clustering/HA enterprise strategy, marketplace/pack ecosystem, and clarified OpenClaw is not the strategic foundation.
- Added Elastic Resource Plane / ProviderOps as a core BOSS module; added provider/resource inventory, billing ledger, ChangeSets, resource receipts, deployment registry, drift detection, Resource Janitor, ProviderOps roles, budget-aware provider actuation, and clarified OpenClaw is not the BOSS foundation.
- Added the first ProviderOps runtime slice with Resource Plane tables, provider-account APIs, official read-only adapter tests/syncs, ChangeSet approval/apply gates, ResourceReceipts, provider health, drift/reconciliation feeds, deployment targets, API-client support, and fail-closed credential/provider-write behavior.
- Added the ProviderOps executive-console dashboard for provider registration/testing, inventory sync, Resource Explorer, ChangeSet Review, Deployment Registry, provider health, drift, costs, reconciliation, and ResourceReceipt visibility.
- Added Resource Janitor cleanup proposal runs that create report artifacts and approval-required cleanup ChangeSets while skipping production resources, unsupported cleanup skills, read-only/destructive-disabled provider accounts, and duplicate open cleanup proposals.
- Updated Core Pack v1 to install ProviderOps Manager, FinOps Sentinel, and Resource Janitor roles along with Resource Plane policy, provider workflows, and provider skill catalogs.
- Added Budget-Aware Autoscaler worker pools and ScalePlans, including local desired-capacity updates, red-budget degraded-mode decisions, and cloud spillover ChangeSet proposals backed by BudgetReservations.
- Added DeploymentRun planning with release-packet artifacts, provider deployment ChangeSet linkage, spendy-deploy reservation enforcement, real health-check reports, rollback artifacts, and health-failure decisions.
- Added real Cloudflare DNS record upsert actuation behind ProviderOps ChangeSet approval, Receipt, ResourceReceipt, provider-health, and managed-resource inventory gates while keeping unsupported provider skills fail-closed.
- Added real Cloudflare R2 bucket creation behind ProviderOps ChangeSet approval, BudgetReservation, Receipt, ResourceReceipt, provider-health, managed-resource inventory, and observed CostEvent gates.
- Added real Cloudflare Worker deployment actuation behind DeploymentRun-planned ChangeSets, approvals, deployable script descriptors, ResourceReceipts, managed Worker resources, observed CostEvents, and DeploymentRun status updates.
- Added real Git-connected Cloudflare Pages deployment actuation behind DeploymentRun-planned ChangeSets, approvals, Pages project descriptors, ResourceReceipts, managed Pages deployment resources, observed CostEvents, and DeploymentRun status updates.
- Added real DigitalOcean droplet create/destroy ProviderOps actuation through the official adapter, gated by ChangeSets, approvals, BudgetReservations for spendy creates, ResourceReceipts, and the explicit real-provisioning policy switch.
- Added real DigitalOcean App Platform deployment actuation through DeploymentRun-planned ChangeSets, approvals, BudgetReservations, ResourceReceipts, managed deployment resources, observed CostEvents, and DeploymentRun status updates.
- Added real Supabase migration apply actuation through DeploymentRun-planned ChangeSets, approvals, Management API credentials, ResourceReceipts, managed migration resources, observed CostEvents, and DeploymentRun status updates.
- Added real Supabase project creation actuation through approved, budget-reserved ChangeSets with env-backed database password SecretRefs and tests proving raw database passwords are not persisted.
- Added real destructive Supabase project pause actuation through approved ChangeSets, rollback instructions, provider destructive-action gates, ResourceReceipts, and managed project resource updates.
- Added read-only ProviderOps billing ingestion for DigitalOcean billing history, OpenAI organization costs, and Anthropic organization cost reports, including duplicate-safe CostEvents, BillingAccount linkage, cost confidence labels, and unsupported-provider deadletters.
- Added standards-based OIDC bearer JWT transport authentication with JWKS signature verification, issuer/audience checks, project-member principal resolution, API-session visibility, and fail-closed missing-provider-config handling.
- Added delegated org-wide RBAC with `org_members`, API-driven org-member management, same-org project authorization fallback, and tests proving org delegates can act across projects without per-project duplication.
- Added console Org Delegation management, org-delegate actor selection, auth-session method/OIDC visibility, and break-glass support for same-org delegated member targets.
- Added official OAuth/CLI session connection metadata for OpenAI Codex, Anthropic Claude Code, and xAI Grok Build subscription entitlements, with broker enforcement that rejects unconnected subscriptions and keeps public subscriptions out of multi-tenant routing.
- Added Model Entitlement Broker connect/disconnect APIs and console controls for official OAuth/session references, capacity-state fail-closed routing, and structured route explanations with auth, connection, capacity, reset, and rejection metadata.
- Added spendy ProviderOps ChangeSet-to-CostEvent recording so successful provider actuation writes receipt-linked observed cost events with explicit confidence labels before provider billing reconciliation.
- Added ProviderOps CostAnomalies and read-only provider kill switches when observed provider spend crosses the budget freeze threshold, with decision cards and dashboard visibility.

## v0.2.0 - 2026-06-02

This is the first clean founder-dogfood release cut from the current API-first control plane.

### Added

- Canonical Postgres-backed control plane for:
  - tasks
  - jobs and leases
  - decisions
  - artifacts
  - receipts
  - budget reservations and cost receipts
  - events
  - deadletters
  - health snapshots
- API-first surfaces for:
  - CEO command intake
  - Metatron status queries
  - budget quote, reserve, release, and status
  - decision inbox and decision actions
  - receipts, audit export, and replay bundles
  - role and pack discovery/install
  - entitlement source management and routing logs
  - comms inbox/outbox
  - self-improvement proposals
- Pull-based worker runtime with:
  - leases
  - heartbeats
  - retries
  - reclaim
  - deadletters
- Software-factory founder flow:
  - command -> plan -> approval -> PR proposal -> PR review -> merge gate / merge job -> deploy review / deploy job
- GitHub-backed sandbox PR, merge, and deployment integrations
- Model entitlement broker with provider-aware routing logs
- Health snapshot generation and top-risk derivation
- Communications plane with receipt-backed, reservation-backed outbound delivery
- Self-improvement workflow with proposal, canary, rollout, and rollback records

### Changed

- Budget state now affects execution at lease time:
  - `orange` defers non-critical work
  - `red` halts non-`p0` work
  - `red` freezes higher-risk merge, deploy, and outbound comms effects
- Sandbox PR proposals now capture real GitHub check-run evidence into `test_report` and `review_packet` artifacts.
- Merge approval now fails closed unless the linked validation report is in a passed state.
- Documentation now includes a shipped implementation-status report in `docs/product/IMPLEMENTATION_STATUS.md`.

### Verification

- `make lint`
- `make test`
- `make smoke`
- OpenAPI parse check

### Known constraints

- Identity, membership, and RBAC are not complete yet.
- Deployment is recorded through real GitHub deployment state, but not yet through a full health-verified staged rollout pipeline.
- Role trust scoring, autonomy changes, and role-drift enforcement are not fully runtime-enforced.
