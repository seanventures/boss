# BOSS PRD v1.1

Status: Governing product-intent specification
Date: June 3, 2026
Supersedes: `docs/product/PRD-v1.0.md`

## Product Thesis

BOSS, the Business Operating System Sentinel, is a financially controlled autonomous company operating system. A CEO issues outcomes; BOSS turns them into durable tasks, plans, decisions, budget-gated jobs, role-scoped worker execution, provider/resource changes, deployment actions, receipts, cost evidence, audit trails, and self-improvement proposals.

BOSS is not merely a chat agent, code assistant, workflow runner, or cloud automation script. It is the durable control plane for AI labor and virtual company operations.

## Executive Summary

BOSS coordinates role-scoped AI workers, tools, approvals, budgets, verification, receipts, provider/model routing, local and cloud execution, communications, software factory workflows, and self-improvement. BOSS now also includes a governed Elastic Resource Plane for external cloud, hosting, deployment, database, DNS, billing, model-provider, and local execution resources.

The new Elastic Resource Plane, surfaced in the UI as ProviderOps and implemented by the Resource Plane Kernel, lets BOSS know which external resources exist, what they cost, who or what created them, which task/product/environment they belong to, whether they are still needed, and what can safely be changed.

External providers are not just tools that agents can call. They are company assets that require inventory, budget policy, deployment governance, receipts, reconciliation, health monitoring, drift detection, and cleanup.

The ProviderOps operating pipeline is:

`Discover -> Model -> Quote -> Reserve -> Approve -> Actuate -> Receipt -> Reconcile -> Optimize`

## Product Promise

- CEOs type outcomes and receive plans, options, risks, costs, decisions, and verified work products.
- Builders turn fuzzy direction into scoped implementation workflows with strict quality gates.
- Operators see what changed, why, what it cost, what was approved, what external resources were affected, and what happens next.
- Finance retains budget control through quotes, reservations, hard caps, cost events, provider billing reconciliation, and cost confidence labels.
- Platform owners can connect providers, inspect resources, manage deployments, detect drift, and clean waste without giving agents raw cloud credentials.

## North Star

A CEO can say, "Build the Decision Inbox default-action system and ship it to staging," and BOSS can safely produce requirements, technical design, tasks, PRs, tests, review packets, ChangeSets, provider cost quotes, budget reservations, decision cards, resource receipts, deployment records, health checks, cost reports, and rollback plans without exceeding approved budgets or bypassing approval gates.

## Core Loop

`CEO command -> intake and clarify -> plan and budget quote -> task graph and role staffing -> execution by bounded workers -> artifacts, tests, review packets, and ChangeSets -> decision cards and approvals -> provider/model/resource actuation through typed skills -> receipts and audit trail -> reconciliation -> optimization proposals -> learn from results -> propose improvements`

## Product Constitution

- Accountability before autonomy.
- Artifacts over memory.
- Bounded loops, not infinite agents.
- Human company analogs with machine-grade controls.
- Financial control shapes routing, execution, scaling, and provider changes.
- Self-improvement requires change management.
- Optimize across limits, never by bypassing them.
- No external effect without a `Receipt`.
- No provider action without a `ResourceReceipt`.
- No provider write without a `ChangeSet`.
- No spendy action without a `BudgetReservation`.
- No destructive action without explicit approval.
- No production DNS or deployment change without approval and rollback plan.
- No worker execution without a lease.
- No silent drops.
- No self-modification without PR/config diff, tests, approval, canary, and rollback.
- No high-risk default action unless policy explicitly allows it.
- No important work exists only in chat.
- No consumer-surface scraping, private endpoint use, credential sharing, or rate-limit evasion.
- No personal subscription source may be used for multi-tenant customer workloads unless the provider explicitly permits that usage mode.
- Agents never receive raw provider secrets. Agents request typed skills; deterministic BOSS services execute provider calls.
- Provider adapters are plugins. The Resource Plane is core product infrastructure.

## Architecture Position

BOSS owns the durable control plane, canonical state, policy enforcement, receipts, routing, budget governance, and observability.

OpenClaw is not the recommended foundation, durable brain, required runtime, or strategic architecture dependency for BOSS. If OpenClaw appears in older planning material, it should be treated only as optional legacy adapter inspiration or an external runtime adapter candidate. BOSS remains adapter-agnostic and controlled by its own durable kernel.

### Architecture Surfaces

- BOSS Control Plane: org/workspace/project state, canonical objects, policies, decisions, budgets, receipts, audit, API, and executive console.
- Execution Plane: leased worker jobs, local and cloud workers, bounded retries, deadletters, and artifact production.
- Communications Plane: outbox/inbox, ACK, throttling, quiet hours, deterministic threading, deadletters, and communication receipts.
- Elastic Resource Plane / ProviderOps: provider connection center, inventory graph, billing ledger, deployment registry, ChangeSets, resource receipts, reconciliation, health, drift, janitor, and autoscaler policy.
- Resource Plane Kernel: deterministic provider/resource service that owns desired state, ChangeSet validation, cost quote enforcement, provider actuation, resource receipts, reconciliation, and drift classification.
- Local Runner / Local Execution Nodes: local workers and provider adapters that can keep secrets and restricted data on customer infrastructure while reporting only `SecretRef`, health, receipts, and metadata to the control plane.
- Model Entitlement Broker / AI Capacity Router: provider/model capacity inventory, rules, routing logs, cost and quota status, and fail-closed source selection.
- Skills Registry: typed model/tool/provider skills with schemas, cost estimators, risk tier, retry policy, permissions, and data-boundary rules.

### Architecture Rules

- Direct provider APIs are allowed in early versions only through typed provider adapters and modeled `ChangeSet` records.
- Later versions may export or sync Terraform/OpenTofu-style plans, but BOSS desired state remains canonical in the database plus durable artifacts.
- Cloud, local, and provider execution must be budget-governed.
- Budget Governor always wins over Autoscaler.
- Provider adapters can be installed, replaced, or marketplace-distributed, but they do not own durable policy or state.

## Primary Personas

- Founder/CEO builder: issues outcomes, reviews decisions, controls spend, and dogfoods BOSS on BOSS.
- Operator/COO: monitors queues, deadletters, incidents, provider health, deployments, and resource waste.
- Finance/FinOps: manages budgets, reservations, billing reconciliation, anomalies, and forecast risk.
- Engineering/Release: uses Software Factory and Deployment Console for PRs, environments, health checks, and rollback.
- Agent platform admin: installs roles, packs, skills, provider adapters, and agent modules.
- Security/compliance: validates data boundaries, secret handling, provider access, receipts, approvals, and audit exports.

## Surface Areas

- Executive Console and Metatron
- Decision Inbox
- Canonical work system
- Budget Control Center
- Durable execution runtime
- Role system and company packs
- Agent Modules and Marketplace
- Software Factory
- Skills Registry and model routing
- Model Entitlement Broker
- Elastic Resource Plane / ProviderOps
- Communications and presence
- Audit, replay, and observability
- Self-improvement and Agent Factory

## Core Requirements

### Executive Console

- Accept natural-language CEO outcomes plus constraints and approval mode.
- Show what changed, top risks, blockers, costs, provider health, resource waste, and next actions.
- Back every status response with fresh references to tasks, decisions, receipts, events, artifacts, resources, ChangeSets, and cost records.

### Decision Inbox

- Decision cards must show recommendation, options, risks, cost impact, deadline, default action, evidence links, and rollback instructions when relevant.
- Supported flows include approve, reject, revise, delegate, expire, and execute.
- Tier-2+ actions require explicit approvals.
- High-risk defaults must be safe by default, typically `HOLD`.

### Canonical Work System

- Stable IDs and lifecycles for tasks, plans, decisions, policies, roles, meetings, receipts, artifacts, events, health snapshots, provider resources, ChangeSets, deployment targets, and cost events.
- Every generated artifact must be versioned and linked.
- Every material mutation must emit an event.

### Budget Governor

- Every spendy action follows quote, reserve, execute, consume/release, cost receipt.
- Budget states are `green`, `yellow`, `orange`, and `red`.
- Hard caps must apply at job leasing, model/tool invocation, provider actuation, cloud worker provisioning, infra provisioning, and outbound comms.

### Durable Runtime

- Jobs move through `queued`, `leased`, `running`, `succeeded`, `failed_retryable`, `failed_final`, `deadlettered`, and `cancelled`.
- Workers are stateless and pull jobs through leases.
- Side effects are idempotent through receipts and dedupe keys.

### Roles and Packs

- Every installed role has a valid `RoleSpec`.
- Core Pack v1 includes Metatron, COO, CAO, CTO, PM, Designer, QA, Release, Program Manager, VP Agents, and ProviderOps roles as they mature.
- Roles cannot operate outside scope without policy escalation.
- Agents may analyze, propose, summarize, and prepare artifacts. Enforcement remains deterministic in the Resource Plane Kernel, Budget Governor, Policy Engine, and Lease Service.

### Agent Modules and Marketplace

- Agent logic must be packageable as a contained install unit rather than living only inside the monolith.
- A packaged `AgentModule` may include role charter, prompt bundle, runtime job kinds, default skill bindings, configuration schema, and marketplace metadata.
- Projects must be able to install, inspect, and version modules independently of company packs.
- Runtime execution should resolve through the installed module when one governs the job kind.
- Modules must not bypass budget, receipt, approval, entitlement, resource plane, or lease enforcement.

### Software Factory

- The dogfood path is spec -> plan -> chunked implementation -> tests -> review packet -> merge decision -> ChangeSet/deployment decision where external resources are affected.
- Real software changes require tests or an explicit test-gap note.
- Merge and deploy remain approval gated.
- Deployment changes route through Deployment Registry and ProviderOps when they affect external environments, DNS, hosting, databases, or provider resources.

### Model Entitlement Broker

- Treat OpenAI, Claude, Gemini, Cursor, Grok, and approved local models as entitlement pools, not unlimited compute.
- Support official CLI, official coding agent, official API, and approved local model sources.
- Support official OAuth, device OAuth, and CLI-authenticated public subscription connections where providers expose them, including Codex/ChatGPT, Claude Code Pro/Max, and Grok Build; BOSS stores only connection references, not raw OAuth tokens.
- Expose API-driven connect/disconnect lifecycle for model entitlement sources, recording only SecretRefs or local official-session references plus non-secret metadata, scopes, capacity state, and audit events.
- Route tasks based on task fit, observed capacity, provider rules, cost, quality, risk, and budget state.
- Fail closed when a source is out of capacity, rate-limited, blocked, disallowed, missing credentials, blocked by data boundary, or blocked by provider terms.
- Routing logs must include selected-source auth/connection/capacity context and rejected-source reasons with enough detail for operators to fix configuration or wait for capacity reset.
- Public subscription sources remain owner-scoped or single-tenant only unless provider rules explicitly permit multi-tenant backend use.
- Model-provider costs and rate limits are resource-plane concerns when they affect provider accounts, cost ledgers, commitments, or quota health.

## Elastic Resource Plane / ProviderOps

### Summary

ProviderOps is the product surface for the Elastic Resource Plane. The Resource Plane Kernel is the deterministic subsystem that models provider accounts, external resources, billing, deployments, ChangeSets, provider health, reconciliation, drift, janitor actions, and autoscaling under budget policy.

### Mission

BOSS should know what external resources exist, what they cost, who or what created them, which task/product/environment they belong to, whether they are still needed, and what can safely be changed.

### User Stories

- As a CEO, I want to see provider spend, resource waste, and risky changes before approving work, so I can scale BOSS without runaway costs.
- As an operator, I want a resource inventory graph, so every cloud, DNS, database, model, deployment, and local runner asset has ownership and health.
- As a release manager, I want every deployment target and rollback point visible, so staging and production changes are governed.
- As a FinOps Sentinel, I want cost estimates reconciled to provider billing, so BOSS never presents guesses as exact spend.
- As a Resource Janitor, I want orphaned resources converted into cleanup ChangeSets, so waste can be safely reduced.
- As a security admin, I want agents blocked from raw provider secrets, so provider access is typed, audited, and least-privilege.

### Non-Goals

- Do not replace provider consoles on day one.
- Do not let agents hold raw cloud credentials.
- Do not auto-delete production resources.
- Do not present estimated cost as exact cost.
- Do not make Terraform/OpenTofu the canonical state store.
- Do not position OpenClaw as the durable BOSS runtime.

### MVP Providers

- DigitalOcean
- Cloudflare
- Supabase
- OpenAI
- Anthropic

### Next Providers

- Gemini / Google AI
- xAI / Grok
- GitHub Actions / CI
- Stripe
- AWS, GCP, and Azure later

### Core Capabilities

- Provider Connection Center
- Resource Inventory Graph
- Cost and Billing Ledger
- Deployment Registry
- ChangeSet Planner
- Provider Skill Catalog
- Resource Janitor
- Resource Health Monitor
- Budget-Aware Autoscaler
- Provider Drift Detector

### Provider Connection Center

Requirements:

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

### Resource Inventory Graph

Requirements:

- Sync external resources on schedule.
- Normalize resources across providers.
- Show ownership: managed by BOSS, discovered unmanaged, orphaned, external/manual.
- Link resources to project, task, deployment, receipts, budget, and owner role.
- Show health, last seen, estimated cost, and actual cost where available.
- Flag unmanaged resources.

### Cost and Billing Ledger

Requirements:

- Ingest provider billing data where available.
- Estimate cost where exact billing data is unavailable.
- Reconcile estimates to actuals.
- Attribute cost by org, workspace, project, task, role, provider, resource, model, and environment.
- Show confidence: exact, provider-reported, invoice-reconciled, estimated, or unknown.
- Generate cost anomalies.
- Link costs to receipts.

### Deployment Registry

Each app has:

- repository
- environments: local, preview, staging, production
- deployment targets
- required resources
- domains
- environment variables and `SecretRef` records
- deployment history
- rollback points
- health checks
- release receipts

### ChangeSet Planner

Requirements:

- Agent proposes changes.
- BOSS generates a `ChangeSet`.
- ChangeSet includes resources to create/update/delete, cost quote, risk tier, rollback plan, approvals needed, and expected receipts.
- User approves or rejects.
- Worker applies with receipts.
- Reconciler verifies result.

### Provider Skill Catalog

Initial skill IDs:

- `cloudflare.dns.record.upsert`
- `cloudflare.worker.deploy`
- `cloudflare.r2.bucket.create`
- `digitalocean.droplet.create`
- `digitalocean.droplet.destroy`
- `digitalocean.app.deploy`
- `digitalocean.billing.sync`
- `supabase.project.create`
- `supabase.project.pause`
- `supabase.migration.apply`
- `openai.costs.sync`
- `anthropic.usage_cost.sync`
- `gemini.rate_limits.sync`
- `xai.rate_limits.sync`

### Resource Janitor

Detects:

- orphaned droplets
- unused databases
- idle staging apps
- old preview environments
- dangling DNS records
- stale API keys
- untagged resources
- model keys with no owner
- unattached volumes
- environments past TTL

Actions:

- propose cleanup
- estimate savings
- create ChangeSet
- require approval for destructive changes
- execute cleanup
- write receipts

### Resource Health Monitor

Tracks:

- provider API availability
- rate limits
- usage caps
- deployed app health checks
- worker node heartbeats
- database connection health
- DNS status
- deployment status
- failed provider calls

### Budget-Aware Autoscaler

Inputs:

- job queue depth
- provider health
- resource cost
- current commitments
- budget headroom
- rate limits
- approval policy
- local node availability
- task priority

Outputs:

- scale up local workers
- create cloud workers
- scale down or drain
- pause noncritical work
- enter degraded mode
- ask CEO for budget increase

### Provider Drift Detector

Requirements:

- Compare provider inventory with BOSS desired state.
- Classify drift: benign, risky, cost-impacting, security-impacting, production-impacting.
- Create drift report.
- Propose remediation.
- Never auto-delete production resources without approval.

### UX Requirements

ProviderOps dashboard must show:

- connected providers
- provider health
- API quota/rate-limit status
- month-to-date spend
- forecast end-of-month spend
- open ChangeSets
- resources by provider
- resources by project
- orphan/waste alerts
- deployments needing attention

Additional UX surfaces:

- Resource Explorer
- Cost Explorer
- ChangeSet Review UI
- Deployment Console

### ProviderOps Acceptance Criteria

- BOSS can connect providers, test permissions, and show missing permissions.
- BOSS can sync read-only resource inventory without creating changes.
- Every provider write requires ChangeSet, policy result, budget reservation when spendy, approval when required, and ResourceReceipt.
- Resource inventory links external resources to project/task/deployment/budget/receipt/owner where known.
- Cost ledger records confidence level and never presents estimates as exact.
- Drift reports classify risk and propose remediation.
- Destructive actions and production DNS/deploy changes require explicit approval and rollback plan.

### Risks and Conflicts

- Provider APIs differ in permission models and billing precision.
- Some providers do not expose exact real-time cost.
- Read-only inventory can reveal unmanaged resources without enough data to assign ownership.
- Direct API actuation can drift from infrastructure-as-code workflows.
- Local-first secret handling can limit cloud-hosted observability.

## Canonical Resource Plane Objects

- `ProviderAccount`
- `ProviderAdapter`
- `ExternalResource`
- `Capability`
- `DeploymentTarget`
- `BillingAccount`
- `CostEvent`
- `Commitment`
- `ChangeSet`
- `ResourceReceipt`
- `DriftReport`
- `ProviderHealth`
- `ResourcePolicy`
- `SecretRef`
- `ReconciliationRun`

### ProviderAccount

```yaml
kind: ProviderAccount
id: pa_digitalocean_main
provider: digitalocean
scope: { org_id: org_boss, workspace_id: boss-dogfood, project_id: boss-dogfood }
credential_ref: secretref_do_main
status: active
modes: [read_only, billing_readable]
destructive_actions_disabled: true
```

### ProviderAdapter

```yaml
kind: ProviderAdapter
id: adapter_digitalocean_v1
provider: digitalocean
version: 1.0.0
capabilities: [resource.sync, droplet.create, droplet.destroy, billing.sync]
execution_location: control_plane_or_local_runner
```

### ExternalResource

```yaml
kind: ExternalResource
id: res_do_droplet_123
provider_account_id: pa_digitalocean_main
provider_resource_id: "123"
resource_type: droplet
ownership: managed_by_boss
state: active
links: { project_id: boss-dogfood, task_id: tsk_123, deployment_target_id: dep_staging }
cost: { estimated_monthly_usd_micros: 12000000, confidence: provider_reported }
```

### BillingAccount

```yaml
kind: BillingAccount
id: bill_do_main
provider_account_id: pa_digitalocean_main
currency: USD
billing_scope: org
status: active
```

### CostEvent

```yaml
kind: CostEvent
id: cost_evt_001
provider_account_id: pa_digitalocean_main
external_resource_id: res_do_droplet_123
amount_usd_micros: 42000
cost_layer: observed
confidence: provider_reported
linked_receipt_id: rr_do_create_123
```

### Commitment

```yaml
kind: Commitment
id: commit_do_droplet_123
resource_id: res_do_droplet_123
minimum_cost_usd_micros: 600000
period: hourly
ttl_at: "2026-06-10T00:00:00Z"
```

### ChangeSet

```yaml
kind: ChangeSet
id: chg_staging_dns_001
state: quoted
risk_tier: 2
operations:
  - action: upsert
    skill_id: cloudflare.dns.record.upsert
    target: res_cf_zone_boss
cost_quote_usd_micros: 0
rollback_plan: Restore previous DNS record value.
approvals_required: [ceo, release_manager]
expected_receipts: [cloudflare.dns.record.upsert:boss.example.com]
```

### ResourceReceipt

```yaml
kind: ResourceReceipt
id: rr_cf_dns_001
changeset_id: chg_staging_dns_001
dedupe_key: cloudflare.dns.record.upsert:boss.example.com:A
provider_ref: zones/zone_123/dns_records/record_456
status: succeeded
payload_hash: sha256:abc123
```

### DeploymentTarget

```yaml
kind: DeploymentTarget
id: dep_boss_staging
app_id: boss
environment: staging
repo: seanheiney/boss
provider_account_ids: [pa_cloudflare_main, pa_supabase_main]
required_resources: [res_cf_worker_boss_staging, res_supabase_project_staging]
domains: [staging.boss.example.com]
health_checks: [https://staging.boss.example.com/health]
rollback_points: [release_2026_06_03_001]
```

## Resource Lifecycle State Machines

ExternalResource states:

`discovered_unmanaged -> planned -> quoted -> reserved -> pending_approval -> provisioning -> active -> degraded -> draining -> pending_delete -> deleted -> orphaned -> error`

ChangeSet states:

`draft -> quoted -> policy_checked -> budget_reserved -> pending_approval -> approved -> applying -> applied -> failed -> rolled_back -> deadlettered`

Deployment states:

`created -> building -> deploying -> deployed -> healthy -> unhealthy -> rollback_pending -> rolling_back -> rolled_back -> failed`

## Financial Control Model

### Four Cost Layers

- Estimated cost
- Reserved cost
- Observed cost
- Reconciled cost

### Cost Confidence Levels

- `exact_call_metered`
- `provider_reported`
- `invoice_reconciled`
- `estimated_from_price_catalog`
- `unknown`

### Spend Protection Pipeline

`Policy check -> Cost quote -> Budget reservation -> Approval if risk/cost threshold exceeded -> Provider call -> Resource receipt -> Cost event -> Reconciliation`

### Budget Kill Switches

- Freeze all provisioning.
- Freeze destructive actions.
- Freeze model API spend.
- Freeze public-cloud spillover.
- Enter local-only mode.
- Enter read-only provider mode.
- Destroy expired preview environments through approved cleanup policy.
- Drain cloud workers.

## ProviderOps Roles

- ProviderOps Manager: owns provider connection posture, inventory quality, and ChangeSet review packets.
- FinOps Sentinel: watches cost events, billing confidence, anomalies, commitments, and forecasts.
- Infra Steward: models infrastructure resources, provider permissions, and resource policies.
- Deployment Manager: owns deployment targets, release receipts, health checks, and rollback evidence.
- Resource Janitor: detects waste and proposes cleanup ChangeSets.
- Secrets Steward: manages `SecretRef` policies, rotation reminders, and local-runner secret boundaries.
- Model Spend Analyst: monitors model-provider usage, rate limits, commitments, and cost anomalies.

These roles may analyze, propose, and summarize. Enforcement remains deterministic in the Resource Plane Kernel.

## ProviderOps Maturity Roadmap

### M0 - Read-Only ProviderOps

Goal: see everything before changing anything.

Build:

- ProviderAccount
- ProviderAdapter
- ExternalResource
- BillingAccount
- CostEvent
- Resource inventory sync
- Basic provider connection center
- Read-only DigitalOcean sync
- Read-only Cloudflare sync
- Read-only Supabase sync
- OpenAI/Anthropic cost sync
- Cost confidence labels
- Resource Explorer

Acceptance:

BOSS can connect providers, list resources, sync costs where available, attribute discovered resources, and show unmanaged/orphaned candidates without making changes.

### M1 - Receipted Actuation

Goal: safely change external resources.

Build:

- ChangeSet
- BudgetReservation
- ResourceReceipt
- Approval gates
- DigitalOcean droplet create/destroy
- Cloudflare DNS record upsert
- Supabase project create
- Model provider route enable/disable
- Provider call deadletters
- Dry-run previews

Acceptance:

No provider write occurs unless a ChangeSet, budget reservation, policy result, approval result, and ResourceReceipt exist.

### M2 - Deployment Console

Goal: deploy BOSS apps safely.

Build:

- DeploymentTarget
- Environment
- AppService
- DeploymentRun
- Release packet linkage
- Cloudflare Worker/Pages deploy
- DigitalOcean App Platform deploy
- Supabase migration apply
- Rollback receipts

Acceptance:

BOSS can deploy a staging app, attach DNS, run health checks, write deployment receipts, and show rollback instructions.

### M3 - Budget-Aware Elastic Scaling

Goal: controlled capacity growth.

Build:

- ScalePlan
- WorkerPool
- Cloud worker provisioning
- Drain/terminate workflows
- Budget kill switches
- Provider spillover rules
- Local-first/cloud-spillover routing
- Cloudflare/DigitalOcean provider health in scheduler

Acceptance:

BOSS can scale workers up/down based on queue pressure, but cannot exceed budget, provider caps, or approval policy.

### M4 - Optimization and Marketplace

Goal: make resources smarter over time.

Build:

- Orphan cleanup automation
- Cost anomaly detection
- Forecasting
- Resource pack templates
- Provider skill pack marketplace
- Move-workload-to-cheaper-target proposals
- Infrastructure refactor proposals

Acceptance:

BOSS can propose cost-saving changes with expected savings, risk, rollout plan, and rollback plan.

## Conflicts and Resolutions

### Conflict 1: Plugin vs Core Plane

Resolution: Provider adapters are plugins. The Resource Plane is core.

### Conflict 2: Fully Elastic vs Financial Control

Resolution: Budget Governor always wins over Autoscaler.

### Conflict 3: Direct API Changes vs Infrastructure as Code

Resolution: M0/M1 can use direct APIs through ChangeSets. Later add Terraform/OpenTofu export/sync. BOSS desired state remains canonical in DB plus artifacts.

### Conflict 4: Billing Precision vs Provider Reality

Resolution: Use cost confidence levels. Never present estimates as exact.

### Conflict 5: Agents Can Manage Cloud vs Security

Resolution: Agents never receive raw secrets. They request typed skills. Resource Plane executes provider calls.

### Conflict 6: Local-First vs Cloud Control Plane

Resolution: Support local provider adapters where secrets stay on the local runner; cloud stores only SecretRef, health, receipts, and metadata.

### Conflict 7: OpenClaw Legacy Runtime Substrate vs Modern BOSS Architecture

Resolution: OpenClaw is not selected as the foundation. If mentioned, classify it as optional/legacy adapter inspiration only. The BOSS architecture is adapter-agnostic and controlled by BOSS's own durable kernel.

## Acceptance Catalog Anchors

- Command creates task, job, artifact, and visible console update.
- Budget denial prevents execution and creates a decision card.
- Reservation lifecycle updates reserved and actual spend correctly.
- Decision approval updates state and emits events.
- Receipt dedupe prevents duplicate effects.
- Lease recovery reclaims expired work.
- Deadletters are visible through UI and API.
- Self-improvement cannot bypass tests, approval, canary, or rollback.
- Provider write cannot occur without ChangeSet, policy result, approval if required, and ResourceReceipt.
- Cost views show confidence level for every provider cost.
- Destructive provider actions and production DNS/deploy changes require approval and rollback plan.

## Current Bootstrap Interpretation

This repository starts with real-only sandbox integrations:

- Real GitHub operations only against `seanheiney/boss-sandbox`.
- Real OpenAI model calls only when an active reservation exists.
- DigitalOcean sandbox actions only when explicitly enabled and policy-gated.
- No fake adapters, no synthetic PR receipts, and no automatic production merge/deploy.

## Reference Links

Model/provider entitlement references remain in `docs/product/MODEL_ENTITLEMENT_BROKER.md`.
