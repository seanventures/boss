# Changelog

All notable changes to BOSS are documented in this file.

## v0.5.0 - 2026-06-03

API baseline release for the UI team: pack import/export, job list, SecretRef CRUD, OpenAPI parity CI, and a scaffolded BOSS Account Connect service.

### Added

- Added `POST /v1/packs/import` and `GET /v1/packs/{pack_id}/export` HTTP endpoints backed by existing SpecPack service logic.
- Added `GET /v1/jobs` list endpoint with status/kind/task filters, paging, and terminal-job inclusion controls.
- Added SecretRef CRUD APIs (`GET/POST/PATCH/DELETE /v1/secret-refs`) with metadata-only storage and providerops actor enforcement.
- Added OpenAPI route parity validation script and CI/Makefile gate (`make openapi-validate`).
- Added `apps/account_connect` FastAPI/Postgres service matching the `AccountConnectAdapter` contract in `commercial.py`.
- Added local Account Connect docker-compose service, postgres init database, and `.env.example` wiring.
- Added staging Helm deploy and cluster validation recording scripts.
- Extended `@boss/api-client` with `importPack`, `exportPack`, `listJobs`, and SecretRef CRUD helpers.

## v0.4.1 - 2026-06-05

Patch release for the validated `main` rollout of the v0.4 commercial, enterprise, ProviderOps, Model Entitlement Broker, and infrastructure readiness work.

### Changed

- Updated release metadata, deployment image tags, Kubernetes examples, Helm chart metadata, and documentation from `v0.4.0` to `v0.4.1` because `v0.4.0` was already published from an earlier commit.
- Removed an accidental tracked image artifact from the release tree while leaving local developer copies ignored.

## v0.4.0 - 2026-06-03

This release adds the commercial, Account Connect, enterprise-governance, SCIM, and entitlement-gated agent-module runtime surfaces that make PRD v2.0 executable beyond documentation.

### Added

- Added the first Enterprise self-hosted Kubernetes package with container Dockerfiles, API/web/worker workloads, bootstrap Job, HPA/PDB guardrails, shared artifact PVC, secret-template guardrails, and a manifest validation command/test.
- Added the first Enterprise Helm chart for self-hosted BOSS with configurable images, replicas, HPA/PDBs, artifact persistence, bootstrap Job, fail-closed safety defaults, and explicit operator-managed secrets.
- Added Community, Cloud, Enterprise Cloud, and Enterprise Self-hosted edition introspection with fail-closed Cloud Account Connect requirements and local Community mode that works without hosted BOSS auth.
- Added BOSS Account Connect link, unlink, and hosted entitlement sync APIs with visible denied events when hosted credentials or connection state are missing.
- Added BOSS Account Connect hosted health-check APIs and Mission Control readiness history so configured hosted endpoints are checked for real and missing configuration records blocked evidence instead of fake healthy status.
- Added hosted marketplace catalog sync through Account Connect with durable sync-run/listing records, Mission Control visibility, and blocked/failed states when hosted configuration or responses are unavailable.
- Added BOSS Account Connect hosted-auth session APIs and Mission Control history with receipt-backed session creation/completion, callback-state validation, hosted-org linking from the adapter response, and visible blocked records when hosted configuration is missing.
- Added license entitlement records and runtime entitlement checks that distinguish edition capabilities from active/trial hosted entitlements.
- Added enterprise cluster inventory with trust metadata for SSO, SCIM, RBAC/ABAC, audit export, private workers, managed ProviderOps, compliance packs, support plans, SLA tier, and detached-license state.
- Added Enterprise cluster validation runs for auditable HA/Kubernetes/private-worker readiness evidence with required-check blocking gaps and Mission Control visibility.
- Added SCIM group and user provisioning records that map active SCIM users into effective org-member access and remove access on deactivation.
- Added entitlement-gated packaged agent module installs with install governance records, denial audit events, rollback snapshots, and private enterprise module coverage.
- Added Model Entitlement Broker enterprise/private and managed cloud model gateway capacity sources with explicit env-backed credential checks, route/pool visibility, restricted-data enterprise routing, and red-budget paid/gateway route denial.
- Added Enterprise audit-export governance with `audit_export` actor enforcement, audit-export disable gates, retention/export-mode metadata in audit exports, and `audit.export_generated` events.
- Added Enterprise support/SLA case records with local case tracking, SLA due dates, lifecycle updates, and receipt-backed hosted escalation that blocks visibly when Account Connect support is not configured.
- Added Mission Control support/SLA panels for open and blocked enterprise support cases, hosted escalation receipts, SLA due dates, and case lifecycle/assignment updates.
- Added constrained Enterprise SAML runtime support with service-provider metadata, signed RSA-SHA256 ACS verification, certificate fingerprint checks, hashed expiring bearer sessions, and fail-closed SAML audit events.
- Added per-project communications channel rollout policies so Slack/email real sends require explicit `real_send_allowed` policy, allowed data boundary, BudgetReservation, Receipt, provider credentials, and visible policy-block decisions when not configured.
- Added per-project Enterprise identity rollout policies so OIDC/SAML/SCIM metadata does not imply live rollout; SAML ACS and SCIM provisioning fail closed until the matching policy explicitly allows authentication or provisioning.
- Added ProviderOps deployment environment rollout policies so provider-backed staging/production DeploymentRuns and direct deployment-skill ChangeSets fail visibly until an explicit environment policy, required BudgetReservation, approval gate, health-check posture, and rollback plan are present.
- Added validated infra environment profiles and an operations runbook for local dogfood, staging, production, and private-enterprise scaling, including Budget Governor precedence, provider-write gates, rollout approvals, rollback evidence, HA Postgres requirements, and kill-switch expectations.
- Added OpenAPI, TypeScript API client, specpack objects, workflows, policies, user stories, and traceability coverage for the new commercial and enterprise runtime surfaces.
- Changed web lint to generate Next route types before TypeScript checks so clean-checkout validation does not depend on a prior production build.

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
