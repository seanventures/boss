# BOSS Roadmap v2.0

Status: Product roadmap companion to `docs/product/PRD-v2.0.md`
Date: June 3, 2026
Last updated: July 18, 2026

This roadmap describes intended maturity phases. It is not an implementation-status report.

## Cross-cutting Capability Tracks

Four product tracks mature alongside the numbered phases and remain separate even when composed in one run:

1. **Agent Groups** — who collaborates and under which reusable protocol. Phase 1 establishes versioned group definitions/runs; later phases add richer templates, enterprise governance, and marketplace packs.
2. **Skills and Integrations** — what typed capability is requested and which installed/configured system implements it. The existing SkillSpec/MCP/provider-adapter foundation is extended with a generic Integration Registry and connection health/authority model.
3. **Reusable Workflows** — what happens, in what order, and under which deterministic gates. Phase 1 turns existing WorkflowSpec YAML into immutable registered versions, work-class mappings, and durable runs; later phases add composition, migration, and marketplace distribution.
4. **Ground-truth and Assurance** — whether claims and outcomes are supported by measured external evidence. Phases 2–3 add regression alarms, settled conclusions, evidence gates, surface ownership, Assurance Cases, and standing verification.

Composition rule: a mapped Workflow may invoke roles or Agent Groups and resolve Skills through Integration Connections; their outputs may feed an Assurance Case. No track owns or substitutes for another.

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
- Canonical schemas for Agent Groups, Integrations, Workflows, and Assurance references.
- WorkflowSpec validation and registry seed path for existing core YAML definitions.
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
- Immutable WorkflowVersions, deterministic WorkflowMappings, durable WorkflowRuns/StepRuns, and reusable subworkflows for core dogfood processes.
- Basic Agent Group runtime with producer-reviewer, bounded-debate, fan-out, and sequential-handoff protocols.
- Generic IntegrationDefinition/IntegrationConnection registry layered over SkillSpec, MCP, and provider adapters.
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
- Continuous ground-truth metrics/observations and regression alarms for collectors and services.
- Production-surface ownership registry and single-deployer enforcement.
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
- Evidence-backed solved/fixed/regressed/caused-by gates and Assurance Cases.
- Standing verifier scheduling over the existing VAG/refute engine.
- Workflow compensation, explicit run migration, and broader Agent Group templates.
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
- Agent Group, Integration, Workflow, and evaluation/assurance packs with explicit trust metadata and install boundaries.
- Simulation/forecasting.

Exit criteria:

- Packs have trust metadata and install boundaries.
- Community can import/export local packs.
- Cloud/Enterprise can use Account Connect marketplace identity.
