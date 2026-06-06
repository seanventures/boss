# BOSS PRD v2.0 - Ultimate Product Spec

Status: Governing product-intent specification
Date: June 3, 2026
Supersedes: `docs/product/PRD-v1.1.md`
Product: BOSS - Business Operating System Sentinel

## Product Thesis

BOSS is not an AI assistant. BOSS is an operating system for accountable autonomous work.

BOSS is a durable control plane for AI labor: a system that lets one human operator run a company through a virtual staff of bounded, role-scoped AI workers. It combines a cloud-native control plane for tasks, decisions, approvals, receipts, policies, budgets, model/tool routing, provider resources, and collaboration with local and cloud execution nodes for software work, research, operations, deployment, and self-improvement. BOSS is built around production semantics - leases, receipts, idempotency, outbox/inbox ACK, policy gates, budget reservations, approvals, auditability, and trust scoring - so AI workers can operate like accountable company employees rather than uncontrolled chat agents.

The product moat is operational correctness applied to AI work: durable tasks, leases, receipts, idempotency, policy gates, approvals, outbox/inbox ACK, auditability, budget reservations, resource inventory, billing reconciliation, trust scoring, bounded autonomy, and self-improvement through spec, eval, canary, and rollback.

## Executive Summary

BOSS, the Business Operating System Sentinel, lets a CEO or operator run a virtual company of AI workers. Chat is an interface, not the system of record. The system of record is durable state and durable artifacts: tasks, plans, decisions, approvals, policies, receipts, budgets, events, role specs, resource inventories, route decisions, and evidence bundles.

Metatron is the executive/operator interface. Mission Control is the primary cockpit. The Software Factory is one department inside BOSS, not the whole company. BOSS also includes ProviderOps, the Elastic Resource Plane for external cloud, hosting, deployment, database, DNS, billing, model-provider, and local execution resources. The Model Entitlement Broker / AI Capacity Router routes work across authorized local models, official CLIs, official APIs, enterprise gateways, and managed provider lanes. The Budget Governor makes elasticity financially controlled.

BOSS is an open-core product: BOSS Community must be useful, self-hostable, and functional without hosted BOSS auth; BOSS Cloud adds hosted convenience and BOSS Account Connect; BOSS Enterprise monetizes operational trust through identity, scale, governance, compliance, support, managed workers, managed ProviderOps, and enterprise infrastructure.

## BOSS Constitution

1. Canonical truth is durable state, not chat.
2. Every meaningful side effect must have a `Receipt`.
3. Every spendy action must have a `BudgetReservation`.
4. Every provider write must go through a `ChangeSet`, `BudgetReservation`, required approval, and `ResourceReceipt`.
5. Every external message requiring acknowledgement must go through Outbox/Inbox ACK.
6. No silent drops: failures retry, deadletter, or escalate visibly; failures while writing audit/denial evidence must be logged with enough actor, project, permission, and object context to investigate the missing event.
7. No self-modification without spec, tests/evals, PR/change packet, approval where required, canary, and rollback.
8. Budget Governor always wins over Autoscaler.
9. Agents never receive raw provider secrets.
10. Agents request typed skills; deterministic service layers execute provider, model, and tool calls.
11. Local-first execution is preferred where policy, privacy, cost, and capability allow.
12. Public escalation must be logged, budgeted, and reviewable.
13. Optimize across authorized capacity; do not bypass limits.
14. Open-source Community edition must be useful and must not require hosted BOSS auth.
15. Provider adapters are plugins; the Resource Plane is core.
16. The open core must not be a crippled demo.
17. Enterprise monetizes operational trust: identity, scale, governance, compliance, support, and managed infrastructure.

## Architecture Position

OpenClaw is not selected as the BOSS foundation. BOSS owns the durable control plane, state, policy, receipts, routing, budget governance, observability, and Resource Plane. External runtimes are adapters, not the product brain.

The recommended architecture is a BOSS-owned durable kernel, adapter-agnostic local/cloud runners, MCP-compatible tools where useful, official provider APIs/CLIs, and deterministic service layers for policy, budgets, receipts, routing, and resource operations.

## Ultimate Product Architecture

### BOSS Control Plane

Owns orgs, workspaces, projects, users, memberships, tasks, decisions, approvals, plans, policies, receipts, trust scores, role specs, role packs, meetings, artifacts, workflows, routing decisions, audit logs, Mission Control state, and Metatron responses.

### Execution Plane

Owns stateless workers, local runners, code workers, review workers, test workers, browser workers, deployment workers, comms workers, local model serving, managed cloud worker pools, and enterprise private workers. Worker execution always requires a lease with TTL, heartbeat, bounded retry, and deadletter behavior.

### Communications Plane

Owns Slack, email, chat, optional voice/SMS later, outbox/inbox ACK, persona rendering, quiet hours, escalation rules, threading, channel rollout policies, delivery receipts, and communication deadletters.

### Elastic Resource Plane / ProviderOps

Owns provider accounts, resource inventory, provider capabilities, resource graph, billing/cost ledger, deployments, ChangeSets, provider receipts, drift detection, Resource Janitor, provider health, budget-aware autoscaling, and resource reconciliation.

### Model Entitlement Broker / AI Capacity Router

Owns local models, official CLI adapters, official API adapters, provider health, entitlement/capacity pools, usage/cooldown tracking, routing decisions, fallback decisions, and no-paid-API-without-budget policy.

### Skills Registry

Owns typed skills, risk tiers, retries, dry-run capability flags, cost estimates, data boundaries, permissions, artifact requirements, and receipt requirements. Skills are the typed boundary agents request; deterministic services decide whether and how to execute them.

### BOSS Account Connect

Owns hosted login, account identity, cloud org/workspace membership, licensing/entitlements, marketplace identity, team invites, billing portal, optional telemetry/health sync, and hosted connector authorization.

Community works with local auth and no hosted dependency. BOSS Cloud uses BOSS Account Connect. Enterprise can run attached or detached. Local runner secrets can stay local; cloud stores only `SecretRef`, metadata, health, receipts, and approvals.

## Canonical Domain Objects

Core objects: `Org`, `Workspace`, `Project`, `User`, `Membership`, `Task`, `Plan`, `Decision`, `Approval`, `Policy`, `RoleSpec`, `RolePack`, `Department`, `KPI`, `Meeting`, `Receipt`, `ArtifactManifest`, `OutboxMessage`, `InboxMessage`, `RoutingDecision`, `TrustScore`, `ImprovementProposal`, `EvaluationRun`, `CanaryRun`, `Deadletter`, `SnapshotHealth`.

Budget/finance objects: `BudgetPolicy`, `BudgetAllocation`, `BudgetReservation`, `CostReceipt`, `CostEvent`, `Commitment`, `BillingAccount`, `ReconciliationRun`.

Model/capacity objects: `ProviderAccount`, `ProviderAdapter`, `EntitlementPool`, `EntitlementConnectionSession`, `ModelRouteDecision`, `CapacityReceipt`, `ModelProviderHealth`, `LocalRunner`, `ExecutionNode`, `WorkerPool`.

ProviderOps objects: `ExternalResource`, `Capability`, `DeploymentTarget`, `DeploymentEnvironmentPolicy`, `DeploymentRun`, `ChangeSet`, `ResourceReceipt`, `DriftReport`, `ProviderHealth`, `SecretRef`, `ResourcePolicy`, `ScalePlan`, `ResourceInventorySnapshot`.

Commercial objects: `Edition`, `LicenseEntitlement`, `AccountConnectOrg`, `AccountConnectHealthCheck`, `HostedMarketplaceListing`, `HostedMarketplaceSyncRun`, `PackListing`, `PackInstall`, `MarketplacePublisher`, `EnterpriseCluster`, `EnterpriseIdentityProvider`.

### YAML Examples

```yaml
ProviderAccount:
  id: pa_do_prod
  provider: digitalocean
  scope_type: project
  scope_id: boss-dogfood
  credential_ref: secretref://project/boss-dogfood/digitalocean
  status: active
  flags:
    read_only: true
    deploy_capable: false
    billing_readable: true
    destructive_actions_disabled: true
```

```yaml
ExternalResource:
  id: er_do_droplet_123
  provider: digitalocean
  provider_account_id: pa_do_prod
  resource_type: droplet
  external_id: "123"
  ownership_state: discovered_unmanaged
  environment: staging
  cost_confidence: provider_reported
```

```yaml
ChangeSet:
  id: cs_cloudflare_dns_staging
  state: pending_approval
  operations:
    - skill_id: cloudflare.dns.record.upsert
      action: upsert
      resource_type: dns_record
  cost_quote_usd_micros: 0
  budget_reservation_id: br_dns_001
  rollback_plan: Restore previous DNS record value from receipt payload.
```

```yaml
ResourceReceipt:
  id: rr_cloudflare_dns_001
  changeset_id: cs_cloudflare_dns_staging
  provider_account_id: pa_cf_prod
  dedupe_key: dns-staging-boss-20260603
  effect_kind: cloudflare.dns.record.upsert
  provider_ref: zone/example/record/abc
  status: succeeded
```

```yaml
DeploymentTarget:
  id: dt_boss_staging
  app_id: boss
  repo: seanheiney/boss
  environment: staging
  provider: cloudflare
  domains: [staging.boss.example]
  required_resources: [er_cf_pages_boss]
  rollback_points: [deploy_20260603_001]
```

```yaml
BudgetReservation:
  id: br_model_plan_001
  scope_type: project
  scope_id: boss-dogfood
  category: model_api
  amount_usd_micros: 1000000
  state: reserved
  expires_at: "2026-06-03T18:00:00Z"
```

```yaml
EntitlementPool:
  id: ep_openai_codex_owner
  provider: openai
  access_class: official_cli
  owner_type: user
  owner_id: founder
  allowed_workload_classes: [owner_sandbox]
  capacity_state: available
  multi_tenant_allowed: false
```

```yaml
RoleSpec:
  role_id: providerops_manager
  mission: Govern provider accounts, resources, drift, health, and cleanup proposals.
  boundaries:
    must: [propose_changesets, surface_provider_risk]
    must_not: [hold_raw_secrets, bypass_resource_plane]
  permissions:
    allowed_skills: [providerops.sync, changeset.plan]
```

```yaml
PackSpec:
  id: providerops_pack
  version: 2.0.0
  installs:
    roles: [providerops_manager, finops_sentinel, resource_janitor]
    workflows: [providerops_readonly_sync, changeset_apply_with_receipts]
    policies: [resource_plane_policy, budget_governor_policy]
```

```yaml
Edition:
  id: community
  license_candidate: AGPLv3
  hosted_account_connect_required: false
  included_capabilities: [local_auth, local_runner, basic_budget_governor, basic_providerops_readonly]
```

```yaml
LicenseEntitlement:
  id: ent_cloud_team
  edition: cloud
  org_id: acct_org_123
  seats: 10
  capabilities: [account_connect, managed_workers, hosted_providerops]
```

## Elastic Resource Plane / ProviderOps

Module name: Elastic Resource Plane. UI name: ProviderOps. Technical subsystem: Resource Plane Kernel.

Mission: Give BOSS a centralized, budget-governed, auditable way to connect to external providers, manage infrastructure, deploy applications, track resources, understand billing, enforce spend controls, and let agents safely extend their capabilities.

Pipeline: `Discover -> Model -> Quote -> Reserve -> Approve -> Actuate -> Receipt -> Reconcile -> Optimize`.

MVP providers: DigitalOcean, Cloudflare, Supabase, OpenAI, Anthropic.

Next providers: Gemini / Google AI, xAI / Grok, GitHub Actions / CI, Stripe, AWS/GCP/Azure later.

Capabilities:

- Provider Connection Center.
- Resource Inventory Graph.
- Cost and Billing Ledger.
- Deployment Registry.
- ChangeSet Planner.
- Provider Skill Catalog.
- Resource Janitor.
- Resource Health Monitor.
- Budget-Aware Autoscaler.
- Provider Drift Detector.
- Secrets and credential boundary management.
- Resource reconciliation.
- Provider deadletters.
- Resource ownership and tagging.
- Cost confidence labels.

Provider Connection Center requirements:

- Add provider account.
- Store credentials securely.
- Test connection.
- Detect permissions.
- Show missing permissions.
- Scope provider to org, workspace, or project.
- Mark provider as read-only, deploy-capable, billing-readable, and destructive-actions-disabled.
- Rotate credentials.
- Disable provider.
- Audit all provider access.

Resource Inventory Graph requirements:

- Sync external resources on schedule.
- Normalize resources across providers.
- Show ownership: managed by BOSS, discovered unmanaged, orphaned, external/manual.
- Link resources to project, task, deployment, receipts, budget, and owner role.
- Show health, last seen, estimated/actual cost.
- Flag unmanaged resources.

Cost and Billing Ledger requirements:

- Ingest provider billing data where available.
- Estimate cost where exact billing data is unavailable.
- Reconcile estimates to actuals.
- Attribute cost by org, workspace, project, task, role, provider, resource, model, and environment.
- Show confidence: exact, provider-reported, invoice-reconciled, estimated, unknown.
- Generate cost anomalies.
- Link costs to receipts.

Deployment Registry requirements:

- Each app has repo, environments, deployment targets, required resources, domains, environment variables/secret refs, deployment history, rollback points, health checks, and release receipts.
- Provider-backed non-sandbox deployment runs require an active `DeploymentEnvironmentPolicy` before BOSS can plan a provider ChangeSet.
- Production environment policies must explicitly use `production_allowed`; metadata-only target registration must never imply production rollout permission.
- Environment policies declare required gates: ChangeSet, BudgetReservation, approval, health check, rollback plan, and optional allowed target kinds.

ChangeSet Planner requirements:

- Agent proposes changes.
- BOSS generates a ChangeSet with resources to create/update/delete, cost quote, risk tier, rollback plan, approvals needed, and expected receipts.
- User approves or rejects.
- Worker applies with receipts.
- Reconciler verifies result.

Provider Skill Catalog examples: `cloudflare.dns.record.upsert`, `cloudflare.worker.deploy`, `cloudflare.r2.bucket.create`, `digitalocean.droplet.create`, `digitalocean.droplet.destroy`, `digitalocean.app.deploy`, `digitalocean.billing.sync`, `supabase.project.create`, `supabase.project.pause`, `supabase.migration.apply`, `openai.costs.sync`, `anthropic.usage_cost.sync`, `gemini.rate_limits.sync`, `xai.rate_limits.sync`.

Resource Janitor detects orphaned droplets, unused databases, idle staging apps, old preview environments, dangling DNS records, stale API keys, untagged resources, model keys with no owner, unattached volumes, and environments past TTL.

Budget kill switches: freeze all provisioning, freeze destructive actions, freeze model API spend, freeze public-cloud spillover, enter local-only mode, enter read-only provider mode, destroy expired preview environments, drain cloud workers.

ProviderOps roadmap:

- M0 Read-only ProviderOps.
- M1 Receipted actuation.
- M2 Deployment console.
- M3 Budget-aware elastic scaling.
- M4 Optimization and marketplace.

Acceptance criteria:

1. BOSS can connect at least three providers in read-only mode.
2. BOSS can inventory resources and label them managed, unmanaged, or orphaned.
3. BOSS can ingest actual billing data where provider APIs support it.
4. BOSS can estimate cost where exact billing data is unavailable.
5. Every provider write requires a ChangeSet.
6. Every provider write requires a BudgetReservation.
7. Every provider write produces a ResourceReceipt.
8. Production-impacting changes require explicit approval.
9. Destructive actions require explicit approval.
10. Resource Explorer shows cost, owner, provider, environment, status, and receipt links.

## Model Entitlement Broker / AI Capacity Router

Mission: Maximize productive AI work across the user's authorized local models, official subscription CLIs, official APIs, and provider accounts while respecting provider terms, quotas, budgets, data boundaries, and safety policies.

Invariant: Optimize across authorized capacity. Do not bypass limits.

Access classes:

- `local_model`
- `official_cli`
- `official_api`
- `enterprise_model_gateway`
- `managed_cloud_model_gateway`

Initial providers/adapters:

- OpenAI API.
- Codex CLI where officially authenticated.
- Cursor CLI where officially authenticated.
- Claude Code / Anthropic API.
- Gemini CLI / Gemini API.
- xAI API.
- Local vLLM-compatible models.
- Future OpenRouter, Bedrock, Vertex, or Azure OpenAI only if explicitly approved.

Requirements:

- `ProviderAdapter` objects.
- `EntitlementPool` objects.
- `EntitlementConnectionSession` objects for official CLI/OAuth subscription connection attempts.
- `CapacityReceipt` objects.
- `ModelRouteDecision` objects.
- Provider cooldowns.
- Usage/rate-limit state.
- Subscription-first mode for personal dogfood.
- API fallback only with BudgetReservation and approval where required.
- Parallel execution only across isolated branches, worktrees, or tasks.
- No scraping consumer UIs.
- No credential sharing.
- No account pooling.
- No rate-limit circumvention.
- No personal subscription use for multi-tenant customer workloads.

Routing modes: `subscription_first`, `quality_first`, `budget_safe`, `parallel_sprint`, `local_first`, `enterprise_policy`.

Acceptance criteria:

1. BOSS detects configured model/provider adapters.
2. BOSS routes jobs according to task fit, budget, data boundary, capacity, quality, and policy.
3. BOSS writes ModelRouteDecision for every model/tool selection.
4. BOSS writes CapacityReceipt for subscription/CLI work.
5. BOSS writes CostReceipt for API work.
6. BOSS falls back safely when a provider is rate-limited or unhealthy.
7. BOSS never calls a paid API fallback without an approved budget reservation when policy requires approval.

## Budget Governor / Budget Slider

The Budget Governor is a core runtime module. The Budget Slider, also called Operational Intensity, is the operator control that maps strategy into caps, burst behavior, model tier selection, loop cadence, worker concurrency, and cloud spillover permissions.

Financial objects: `BudgetPolicy`, `BudgetAllocation`, `BudgetReservation`, `CostReceipt`, `CostEvent`, `Commitment`.

Hierarchical budgets apply at org, workspace, project, initiative, department, role, provider, model, and worker-pool level. Budget policy supports hard caps, soft caps, burst caps, monthly/daily budgets, per-provider caps, per-model caps, per-worker-pool caps, commitment accounting, reservations before execution, reconciliation after provider data arrives, anomalies, and kill switches.

Spend pipeline: `Quote -> Reserve -> Execute -> Receipt -> Reconcile`.

Budget states: `green`, `yellow`, `orange`, `red`, `frozen`.

Acceptance criteria:

1. No spendy model/tool/provider action starts without a budget reservation.
2. Hard caps prevent net-new spend.
3. Soft caps degrade routing and slow loops.
4. Forecast panel shows burn rate, forecast EOD/month, and time-to-cap.
5. Commitments are tracked for resources that keep billing after creation.
6. Budget Governor always wins over Autoscaler.

## Autonomous Software Factory

The Software Factory is one BOSS department, not the whole company.

Capabilities:

- Intent-to-spec.
- PRD generation.
- Architecture plan.
- Task decomposition.
- Branch/worktree isolation.
- Code generation.
- Test generation.
- UI/browser validation.
- Security/dependency review.
- CI execution.
- Review packets.
- Evidence bundles.
- Release packets.
- Staged deployments.
- Merge/deploy decisions.
- Rollback plans.

Evidence bundles must include spec refs, task refs, diffs, test results, build logs, browser traces/screenshots where relevant, security/dependency outputs, confidence score, review summary, receipts, and rollback plan.

Definition of done: software work is complete only when objective evidence demonstrates builds pass, tests pass, runtime behavior satisfies acceptance criteria, required artifacts are attached, and required receipts exist.

## Roles, Role Packs, and Agent Factory

Core v2 role pack:

- Metatron.
- COO.
- Chief Architect.
- CTO.
- PM.
- Product Designer.
- QA Manager.
- Build & Release Manager.
- Program Manager.
- VP of Agents.
- ProviderOps Manager.
- FinOps Sentinel.
- Infra Steward.
- Deployment Manager.
- Resource Janitor.
- Secrets Steward.
- Model Spend Analyst.

Role system requirements include RoleSpec template, RolePack format, PackSpec format, autonomy levels, trust scoring, role drift detection, eval rubrics, staged role rollout, role retirement, and role split/merge proposals.

Agent Factory responsibilities:

- Maintain objective graph.
- Maintain coverage map.
- Maintain workload map.
- Propose roles.
- Evaluate roles.
- Detect drift.
- Propose autonomy upgrades/downgrades.
- Build packs.
- Canary packs.
- Roll back bad role changes.

No new role may ship without objective linkage, bounded scope, eval plan, permissions, budget, staged rollout, and rollback plan.

## Metatron and Mission Control

Metatron is the executive/operator interface. It provides status graph, what changed, blockers, decision needs, risk summaries, incident mode, command palette, routing/cost/receipt visibility, and freshness-aware answers with durable references. Metatron can render to dashboard, chat, TUI, and later voice/video.

Mission Control is the home dashboard and primary cockpit. It includes task board, decision inbox, ProviderOps dashboard, Resource Explorer, Cost Explorer, Deployment Console, receipts/audit view, role health/trust, queue/worker status, routing explanations, meeting/incident timelines, Software Factory evidence bundle views, and command palette.

Core insight: Mission Control makes agents visible. BOSS must make them controllable.

## Meetings and Consensus

Meetings are first-class objects. Meeting types include daily standup, weekly operating review, monthly business review, architecture review, PRD validity conference, GTM conference, release go/no-go, incident review, staffing/role review, ProviderOps change review, and budget review.

Required outputs: transcript, minutes, decisions, action items, blocked state if unfinished, and dissent record when applicable.

Consensus protocol: `propose -> critique -> revise -> vote -> record dissent -> publish decision`.

## Communications and Presence

BOSS supports Slack-style internal chat, email as durable command/receipt backbone, optional Google Chat, optional SMS/voice escalation later, persona identity rendering, urgency tiers, quiet hours, ACK-required critical messages, durable Outbox/Inbox objects, no silent drops, deadletters, and escalations. Real Slack/email sends require an explicit per-project channel rollout policy, allowed data boundary, BudgetReservation, Receipt, provider credentials, and visible policy-block decisions when not configured.

## Knowledge, Memory, and SSP

Artifacts are durable memory. BOSS maintains searchable artifact graph, meeting history, decision history, receipts, knowledge base, citations to durable refs, Session Continuity Packs / SSP, lessons learned, regressions, and role/task/outcome links.

Rule: no critical continuity should depend on a live chat thread.

## Enterprise Governance and Compliance

Enterprise governance requirements include RBAC, ABAC, SSO/OIDC/SAML, SCIM, identity-provider rollout policies, audit exports, retention policies, BYOK/KMS later, connector permission governance, security policy packs, compliance evidence workflows, private workers, private model gateway, data residency, VPC/private networking later, air-gapped enterprise later, and enterprise support/SLA.

## Commercial Strategy and Open-Core Packaging

BOSS should be open enough to create trust, adoption, extensibility, and developer mindshare, while monetizing the operational trust features companies pay for: identity, scale, governance, compliance, uptime, support, managed workers, managed ProviderOps, and cloud convenience.

Licensing posture:

- Be honest about "open source".
- If commercial use or competitive hosting is restricted, do not call that restricted part open source.
- True open-source Community should use an OSI-compatible license.
- Enterprise modules may be commercial, proprietary, or source-available.
- Legal review is required before final license selection.

Recommended model: open-core plus managed cloud plus enterprise.

Recommended initial license strategy, subject to legal review:

- BOSS Community server/control plane: AGPLv3 candidate.
- SDKs, schemas, CLIs, pack formats, example packs: Apache-2.0 or MIT candidate.
- Enterprise modules: commercial/proprietary.
- Cloud: paid SaaS.
- Alternative to evaluate: fair-source/delayed-open-source model such as FSL, clearly not true open source during restriction period.

### BOSS Community

Self-hosted, useful, not a crippled demo. Includes local auth, single-org/single-workspace mode, local control plane, local runner, core canonical objects, basic Metatron, basic Mission Control, basic Decision Inbox, basic receipts/dedupe, basic Budget Governor, basic Software Factory, basic Model Entitlement Broker, basic ProviderOps read-only inventory, local model routing, role/skill/pack formats, and import/export.

Community must not require hosted BOSS auth.

### BOSS Cloud

Paid managed SaaS. Includes BOSS Account Connect, managed login/auth, hosted control plane, hosted Mission Control, managed Postgres/object store/queues, managed backups, managed secrets vault, managed connector gateway, managed model gateway, hosted budget/cost dashboard, managed workers, hosted ProviderOps, and automatic updates.

### BOSS Enterprise Cloud

Paid enterprise SaaS. Includes SSO/OIDC/SAML, SCIM, RBAC/ABAC, audit exports, policy packs, private workspaces, dedicated workers, data residency options, private networking/VPC later, enterprise budget controls, legal/compliance workflows, custom adapters, custom model routing policies, premium support/SLA.

### BOSS Enterprise Self-Hosted

Paid on-prem/private cloud. Includes HA deployment, clustering, multi-node worker pools, Kubernetes support, air-gapped mode later, offline license activation, private model gateway, private connector gateway, private ProviderOps, advanced audit/compliance, LTS builds, and support/SLA.

### BOSS Account Connect

BOSS Account Connect owns hosted login, cloud org/workspace identity, license entitlements, marketplace identity, team invites, billing portal, managed connector authorization, hosted model/provider credential management, optional telemetry/health sync, hosted readiness checks, and cloud backup/sync if enabled.

Rules:

- Community works without Account Connect.
- BOSS Cloud requires Account Connect.
- Enterprise can run attached or detached.
- Account Connect is valuable but optional for self-host Community.
- Hosted login must be represented in the local control plane by durable auth-session records, receipts, state validation, visible blocked/failed states, and an auditable Account Connect org link only after the hosted adapter returns a verified hosted organization.
- Hosted readiness checks must be represented by durable `AccountConnectHealthCheck` records. Missing hosted configuration must create a blocked health-check record; BOSS must not invent a healthy state.
- Hosted marketplace catalog sync must be represented by durable sync-run and listing records. Missing hosted configuration or invalid hosted responses create blocked/failed sync runs; BOSS must not invent hosted marketplace listings.

### Clustering and Scaling Monetization

Community includes one control plane, one Postgres, one/few local workers, and basic local runners.

Enterprise includes multi-worker clustering, queue partitioning, HA Postgres guidance/charts, Kubernetes deployment, worker autoscaling, regional worker pools, tenant isolation, failover, private clusters, advanced budget/resource governance, and air-gapped updates later.

Commercial modules include advanced ProviderOps, write-capable provider actuation, billing/cost reconciliation, drift detection, Resource Janitor automation, managed worker fleets, managed model gateway, managed connector gateway, advanced audit/compliance, enterprise policy packs, marketplace, and support/SLA.

Clean boundary: Community helps you run BOSS. Enterprise helps a company trust BOSS.

## Marketplace and Ecosystem

BOSS ecosystem includes role packs, skill packs, company templates, sector packs, provider adapters, policy packs, workflow packs, evaluation packs, free/paid packs, verified publishers, private enterprise pack registry, marketplace identity via BOSS Account Connect, import/export, and pack signing/trust metadata later.

Examples: SaaS Company Pack, Agency Pack, Ecommerce Pack, Real Estate Ops Pack, Local Services Pack, Regulated Industry Overlay, ProviderOps Pack, Software Factory Pack, GTM Pack, Enterprise Compliance Pack.

## Maturity Roadmap

Phase 0 - Kernel / Local dogfood:

- Domain objects.
- Tasks, decisions, policies, receipts, artifacts.
- Postgres-backed leases.
- Deadletters.
- Local auth.
- Local runner.
- Mission Control shell.
- Metatron status API.
- Simple budget governor.
- Basic Software Factory proposal/dry-run mode.
- PRD/spec export.

Phase 1 - BOSS builds BOSS:

- Spec-to-PR workflow.
- Branch/worktree isolation.
- Evidence bundles.
- Model entitlement broker.
- Local/subscription/API routing.
- Decision inbox.
- Basic budget reservations.
- Receipts everywhere.
- Dogfood workspace.

Phase 2 - ProviderOps M0/M1:

- Read-only provider sync.
- Resource inventory.
- Cost ledger.
- ChangeSets.
- Receipted provider actuation.
- DigitalOcean/Cloudflare/Supabase/OpenAI/Anthropic MVP providers.

Phase 3 - Software Factory v1:

- Code/test/review lanes.
- Browser/UI validation.
- CI integration.
- Release packets.
- Deployment registry.
- Staging deploys.

Phase 4 - Cloud SaaS:

- BOSS Account Connect.
- Hosted auth.
- Managed control plane.
- Hosted worker pools.
- Managed connector gateway.
- Managed model gateway.
- Cloud billing.

Phase 5 - Enterprise:

- SSO/SCIM/SAML.
- RBAC/ABAC.
- Audit exports.
- Clustering.
- HA.
- Private workers.
- Enterprise ProviderOps.
- Compliance packs.
- Support/SLA.

Phase 6 - Marketplace and sector packs:

- Pack registry.
- Paid/free packs.
- Verified publishers.
- Private enterprise packs.
- Sector templates.
- Simulation/forecasting.

## Success Metrics

Core operations: time-to-first-value, duplicate effect rate, silent drop rate, evidence-backed completion rate, decision throughput, approval latency, rollback rate, deadletter rate, stuck lease rate.

Audit operations: material-action audit-event coverage, audit-write failure count, policy-denial evidence coverage, failed receipt/event reconciliation time, and operator-visible investigation context for any failed audit write.

Software factory: spec-to-PR time, PR acceptance rate, test pass rate, escaped defect rate, confidence score calibration, time-to-green-CI.

Financial: budget overrun rate, forecast accuracy, cost per task, cost per PR, local/public routing savings, orphan resource savings, provider spend anomaly count.

ProviderOps: unmanaged resource count, orphaned resource count, drift count, cleanup savings, provider health uptime, reconciliation lag, cost confidence distribution.

Commercial: Community installs, activation rate, Cloud conversion, enterprise trial conversion, managed worker usage, marketplace pack installs, support burden by edition.

Trust/autonomy: trust score trends, autonomy upgrades/downgrades, policy violations, role drift incidents, operator corrections per role.

## Conflicts and Resolutions

1. Plugin vs core plane. Resolution: provider adapters are plugins. The Resource Plane is core.
2. Fully elastic vs financial control. Resolution: Budget Governor always wins over Autoscaler.
3. Direct API changes vs Infrastructure as Code. Resolution: M0/M1 can use direct APIs through ChangeSets. Later add Terraform/OpenTofu export/sync. BOSS desired state remains canonical in DB plus artifacts.
4. Billing precision vs provider reality. Resolution: use cost confidence levels. Never present estimates as exact.
5. Agents can manage cloud vs security. Resolution: agents never receive raw secrets. They request typed skills. Resource Plane executes provider calls.
6. Local-first vs cloud control plane. Resolution: support local provider adapters where secrets stay on the local runner; cloud stores only SecretRef, health, receipts, and metadata.
7. OpenClaw legacy/runtime substrate vs modern BOSS architecture. Resolution: OpenClaw is not selected as the foundation. If mentioned, classify it as optional/legacy adapter inspiration only.
8. Open source adoption vs monetization. Resolution: Community must be useful and self-hostable. Enterprise monetizes scale, governance, compliance, managed infrastructure, support, and commercial safety.
9. Central auth moat vs open-source trust. Resolution: BOSS Account Connect is required for Cloud but optional for Community. Community must run with local auth.
10. Personal subscriptions vs production workloads. Resolution: personal subscription/CLI capacity is for owner dogfood/personal work only. Production/customer/multi-tenant workloads use approved APIs or enterprise gateways.

## Release-Grade Acceptance Criteria

- Every implemented requirement maps to tests and traceability records.
- Every spendy action has quote/reserve/execute/receipt/reconcile coverage.
- Every provider write has ChangeSet, BudgetReservation, approval when required, ResourceReceipt, and rollback plan.
- Every provider-backed non-sandbox deployment run has an explicit environment rollout policy before any provider ChangeSet is planned.
- Every critical message has Outbox/Inbox ACK, retry/deadletter, and escalation path.
- Every model route decision records selected/rejected sources, capacity, budget state, data boundary, and provider-policy result.
- Community runs locally without BOSS Account Connect.
- Cloud uses BOSS Account Connect.
- Enterprise attached/detached modes are designed before enterprise packaging ships.
- No personal subscription or public model capacity path is described as rate-limit bypass.
