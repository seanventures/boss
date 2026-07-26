# BOSS Technical Specification v2.0

Status: Technical companion to `docs/product/PRD-v2.0.md`
Date: June 3, 2026
Last updated: July 18, 2026
Supersedes: `docs/product/TECHNICAL_SPEC-v1.1.md`

## Architecture Overview

BOSS is a durable, API-first control plane for accountable AI labor. The technical architecture is organized into first-class planes:

- Control Plane: canonical tenant state, tasks, plans, decisions, approvals, policies, budgets, receipts, events, audit, roles, packs, Mission Control, and Metatron.
- Execution Plane: leases, stateless workers, local runners, cloud workers, browser workers, code/test/review workers, deployment workers, comms workers, worker pools, local model serving, and enterprise private workers.
- Communications Plane: outbox/inbox, ACK, persona rendering, quiet hours, escalation, channel rollout policies, delivery receipts, and deadletters.
- Elastic Resource Plane / ProviderOps: provider accounts, adapters, inventory, billing, deployments, ChangeSets, ResourceReceipts, drift, health, reconciliation, janitor, and autoscaler policy.
- Model Entitlement Broker / AI Capacity Router: adapter discovery, entitlement pools, capacity state, route decisions, cooldowns, fallback selection, local/subscription/API/enterprise gateway routing.
- Skills Registry: typed skills, risk tier, data boundary, permissions, retry policy, cost estimates, artifact requirements, receipt requirements.
- Integration Registry: connector/adapter definitions, installed connections, SecretRefs, granted capabilities, scope, health/freshness, provenance, rate-limit state, and revocation.
- Agent Group Runtime: versioned collaboration definitions, member-role resolution, topology/protocol, context boundaries, independence, dissent, bounded rounds, budgets, and durable runs.
- Workflow Registry and Runtime: immutable definitions/versions, deterministic work mappings, durable runs/step runs, branching, fan-out/join, approvals, subworkflows, retries/deadletters, compensation, and migration.
- Ground-truth and Assurance Runtime: metric observations, settled conclusions, typed claims/evidence, surface ownership, Assurance Cases, and scheduled adversarial verification.
- BOSS Account Connect: hosted account identity, licensing, marketplace identity, cloud org/workspace membership, team invites, billing portal, managed connector authorization, and optional telemetry/health sync.
- Enterprise Identity Runtime: OIDC/SAML/SCIM provider metadata, SecretRefs, certificate/JWKS configuration, and explicit per-project rollout policies. Provider metadata alone is not sufficient for live SSO or SCIM; runtime paths require matching rollout policy.

OpenClaw is not the BOSS foundation or durable runtime. External runtimes, legacy systems, and coding agents are adapters behind BOSS-owned state, policy, budget, routing, receipt, and observability layers.

## Resource Plane Kernel

The Resource Plane Kernel is core infrastructure, not a plugin. Provider adapters are plugins.

Kernel responsibilities:

- Provider account registry and permission model.
- `SecretRef` and credential-boundary enforcement.
- Resource inventory normalization.
- Billing/cost ledger ingestion.
- Cost quote and BudgetReservation enforcement.
- ChangeSet planning and state transitions.
- Provider actuation through typed skills.
- ResourceReceipt and dedupe.
- Reconciliation runs.
- Drift reports.
- Resource Janitor proposals.
- Provider health and rate-limit state.
- Budget-aware autoscaler inputs and provider spillover controls.

Provider write pipeline:

`Policy check -> Cost quote -> Budget reservation -> Approval if threshold/risk requires -> Provider call -> ResourceReceipt -> CostEvent -> Reconciliation`.

Provider write invariant: no provider write without ChangeSet, BudgetReservation, required approval, and ResourceReceipt.

## Provider Adapter Interface

Provider adapters implement a deterministic interface:

- `test_connection(provider_account, secret_ref) -> ProviderHealth`
- `detect_permissions(provider_account) -> Capability[]`
- `sync_inventory(provider_account) -> ExternalResource[]`
- `sync_billing(provider_account, window) -> CostEvent[]`
- `quote(changeset) -> CostQuote`
- `dry_run(changeset) -> PlanArtifact`
- `apply(changeset, operation, idempotency_key) -> ResourceReceipt`
- `reconcile(provider_account, desired_state) -> ReconciliationRun`

Adapters must not expose raw secrets to prompts, workers, or artifacts. Local adapters may hold secret material locally and report only `SecretRef`, health, metadata, and receipts.

## Provider Connection and Auth Model

Provider connections use `ProviderAccount`, `ProviderAdapter`, `SecretRef`, `Capability`, and `ProviderHealth`.

Required provider-account controls:

- Publish provider/auth definitions with named credential slots and explicit
  `available` versus `planned` runtime status for AWS, Azure, GCP,
  DigitalOcean, and OVHcloud.
- Scope to org, workspace, or project.
- Mark read-only, deploy-capable, billing-readable, destructive-actions-disabled.
- Detect and show missing permissions.
- Rotate credentials.
- Disable provider.
- Revoke provider access irreversibly inside BOSS and fail before adapter
  invocation when the account or any referenced credential is disabled or
  revoked.
- Audit every provider access.

Community may use local environment or local vault SecretRefs. Cloud uses managed connector authorization through BOSS Account Connect. Enterprise can run attached to Account Connect or detached with private credential stores.

`ProviderAccount` stores `auth_method`, named `credential_refs`,
non-secret `connection_metadata`, owner, credential version/rotation time, and
disable/revoke evidence. `credential_ref` remains the backwards-compatible
primary pointer for adapter execution. `SecretRef` stores scope, secret kind,
storage backend, rotation/verification time, predecessor/overlap lineage, and
revocation evidence—never secret material. Rotation activates the new named
pointers immediately, marks replaced pointers as `rotating` for a bounded
overlap, clears prior connection-test evidence, and increments the account
credential version.

## Resource Sync and Reconciliation Workers

Read-only sync workers:

1. Lease provider sync job.
2. Load ProviderAccount and SecretRef.
3. Test connection.
4. Detect permissions.
5. Sync inventory and billing where allowed.
6. Normalize ExternalResource, CostEvent, BillingAccount, Commitment, ProviderHealth, and ReconciliationRun records.
7. Emit events.
8. Deadletter failures visibly.

Reconciliation compares provider inventory with BOSS desired state and classifies drift as benign, risky, cost-impacting, security-impacting, or production-impacting. Production-impacting drift never auto-remediates without approval.

## ChangeSet Executor

ChangeSet state machine:

`draft -> quoted -> policy_checked -> budget_reserved -> pending_approval -> approved -> applying -> applied -> failed -> rolled_back -> deadlettered`.

Execution rules:

- No provider write without ChangeSet, BudgetReservation, required approval, and ResourceReceipt.
- No other spendy action without BudgetReservation.
- No destructive action without explicit approval.
- No production DNS/deploy change without approval and rollback plan.
- Provider-backed non-sandbox DeploymentRuns require an active DeploymentEnvironmentPolicy before ChangeSet planning.
- Production DeploymentEnvironmentPolicy records must use `production_allowed` runtime mode and may narrow allowed target kinds.
- Every provider write creates ResourceReceipt.
- Failed provider calls become visible events, deadletters, or decision escalations.

## Cost Ledger and Reconciliation

BOSS tracks four cost layers: estimated cost, reserved cost, observed cost, reconciled cost.

Cost confidence values:

- `exact_call_metered`
- `provider_reported`
- `invoice_reconciled`
- `estimated_from_price_catalog`
- `unknown`

Cost attribution dimensions: org, workspace, project, initiative, department, role, provider, resource, model, environment, task, receipt, ChangeSet, worker pool.

Never present estimates as exact.

## Model Entitlement Broker

The Model Entitlement Broker routes authorized AI capacity across local models, official CLIs, official APIs, enterprise model gateways, and managed cloud model gateways.

Core objects:

- `ProviderAdapter`
- `EntitlementPool`
- `EntitlementConnectionSession`
- `ModelRouteDecision`
- `CapacityReceipt`
- `ModelProviderHealth`
- `LocalRunner`
- `ExecutionNode`
- `WorkerPool`

Routing inputs:

- Task kind and task fit.
- Data boundary.
- Risk tier.
- Budget state.
- BudgetReservation requirement.
- Capacity state and cooldown.
- Provider health.
- Source owner and workload class.
- Subscription/API/local/enterprise policy.
- Quality tier and latency expectation.

Routing modes: `subscription_first`, `quality_first`, `budget_safe`, `parallel_sprint`, `local_first`, `enterprise_policy`.

Invariants:

- Optimize across authorized capacity; do not bypass limits.
- No consumer UI scraping.
- No private endpoint use.
- No credential sharing.
- No account pooling.
- No rate-limit circumvention.
- No personal subscription use for multi-tenant customer workloads.
- No paid API fallback without BudgetReservation and approval where policy requires.

## Budget Reservation Protocol

All spendy model/tool/provider/cloud-worker actions must execute through:

`Quote -> Reserve -> Execute -> Receipt -> Reconcile`.

Budget states: `green`, `yellow`, `orange`, `red`, `frozen`.

Budget kill switches:

- Freeze all provisioning.
- Freeze destructive actions.
- Freeze model API spend.
- Freeze public-cloud spillover.
- Enter local-only mode.
- Enter read-only provider mode.
- Destroy expired preview environments.
- Drain cloud workers.

Budget Governor always wins over Autoscaler.

## Local Runner Model

Local runners are execution nodes that can host workers, browser automation, code execution, provider adapters, local models, and customer-held secrets. They still use leases, typed jobs, policy checks, BudgetReservations, receipts, and deadletters.

Local runners may be Community-managed, Cloud-attached, or Enterprise-private. Detached Enterprise runners must support local license activation and private SecretRef resolution.

## Agent Group Runtime

Canonical records:

- `AgentGroupSpec`: stable identity, scope, purpose, policy references, and active version.
- `AgentGroupVersion`: immutable member RoleSpecs, topology, protocol, context policy, allowed capability constraints, independence rules, budget/call bounds, completion rule, and escalation target.
- `AgentGroupRun`: pinned version, subject/task/workflow/assurance references, state, policy snapshot, budget, timestamps, and disposition.
- `AgentGroupMemberRun`: resolved role/module, input bundle version, leased job, attempts, outputs, costs, receipts, and status.
- `AgentFinding` and `DissentRecord`: separately attributable conclusions and evidence references.

Group-run state machine:

`queued -> resolving -> running -> waiting_member|waiting_decision -> completed|failed|deadlettered|cancelled`.

Completion rules decide whether the collaboration protocol is finished, not whether a claim is true. Evidence/approval/safety gates execute after group completion. Every spendy member call uses BudgetReservation and Receipt; every member job uses lease, heartbeat, bounded retry, and deadletter.

## Skill and Integration Runtime

`SkillSpec` remains the requested capability boundary. `IntegrationDefinition` lists connector metadata and supported SkillSpecs. `IntegrationConnection` is a scoped installation with `SecretRef`, permission grants, health/freshness, rate-limit state, owner, provenance, and enabled/revoked status. `ExecutorBinding` maps a SkillSpec and compatible IntegrationDefinition to a deterministic local/server/plugin executor.

Resolution input is `(tenant, project, actor, RoleSpec, AgentGroupVersion?, WorkflowStep?, SkillSpec, risk, data_boundary, policy_snapshot)`. The result is either one allowed executor/connection with an explanation or a fail-closed Decision/denial. Resolution must recheck connection health, revocation, permissions, ownership where relevant, and current project registry state immediately before execution.

Each `SkillInvocation` records the requested and effective SkillSpec versions, executor binding, IntegrationConnection, typed input/output hashes, attempts, evidence/artifacts, cost, BudgetReservation, Receipt/dedupe key, and ChangeSet/ResourceReceipt for provider effects. Raw credentials never enter agent context.

## Reusable Workflow Runtime

Canonical records:

- `WorkflowDefinition`: stable workflow identity, scope, owner, pack/provenance, and active version.
- `WorkflowVersion`: immutable typed graph containing steps, edges, inputs/outputs, gates, role/Agent Group and Skill/Integration bindings, retry/deadletter, timers, compensation, and completion rules.
- `WorkflowMapping`: deterministic selector over command/task/event/object kind plus tenant/project/policy scope and priority, pointing to one approved WorkflowVersion.
- `WorkflowRun`: pinned definition/version, mapping/policy snapshot, inputs, state, current frontier, budget, evidence, decisions, timestamps, and final output.
- `WorkflowStepRun`: step state, leased job or Decision/AgentGroupRun/subworkflow reference, attempts, inputs/outputs, evidence, costs, receipts, and compensation state.
- `WorkflowMigration`: explicit validated transition of a paused run between compatible versions, including rollback evidence.

Mapping resolution is deterministic: most-specific active scope wins, then explicit priority; ambiguous equal-precedence mappings fail closed. When no mapping exists, intake may create a workflow proposal or Decision but cannot synthesize an authoritative run graph silently.

Workflow-run states:

`queued -> validating -> running -> waiting_timer|waiting_decision|blocked -> completed|failed|deadlettered|cancelled|compensating -> compensated`.

Supported graph semantics include sequential edges, conditionals, parallel fan-out/join, bounded loops, human decisions, Agent Group steps, Skill invocations, Integration-backed actions, and pinned subworkflows. Step effects are idempotent; crash recovery resumes from durable state. Published versions are immutable. Existing `packages/specpack/workflows/*.yaml` files are seed definitions and must pass schema/policy validation before activation.

## Ground-truth and Assurance Runtime

Canonical records include `GroundTruthMetricDefinition`, `GroundTruthObservation`, `SettledConclusion`, `Claim`, `Evidence`, `SurfaceOwnership`, `AssuranceCase`, and `VerificationRun`.

An Assurance Case references, but does not contain or redefine, AgentGroupRuns, WorkflowRuns, SkillInvocations, and IntegrationConnections. Policy evaluation snapshots the metric/evidence contract, settled conclusions, surface ownership, risk, tenant/project rules, and required verifier. A blocked or conflicting result creates a durable Decision/event; lower-scope policy cannot weaken mandatory higher-scope requirements.

Metric evaluators and verifier runs are leased jobs with dedupe, retry/deadletter, and budgets/receipts where spendy. Fixed/solved claims require reproducible red-to-green evidence. Caused-by claims require an isolation experiment or remain `inconclusive`. Surface ownership is revalidated at the provider/deployment boundary.

## BOSS Account Connect

BOSS Account Connect owns hosted login, account identity, cloud org/workspace membership, license entitlements, marketplace identity, team invites, billing portal, managed connector authorization, hosted model/provider credential management, optional telemetry/health sync, and cloud backup/sync if enabled.

Rules:

- Community works with local auth and no Account Connect dependency.
- Cloud requires Account Connect.
- Enterprise can run attached or detached.
- Account Connect must not become a hard dependency for open-source Community operation.
- Hosted service readiness is recorded through `AccountConnectHealthCheck` rows. Checks call only the configured Account Connect endpoint; missing configuration records a blocked check instead of a fake healthy result.
- Hosted marketplace catalogs are recorded through `HostedMarketplaceSyncRun` and `HostedMarketplaceListing` rows. Sync calls only the configured Account Connect endpoint; missing configuration or invalid hosted responses record blocked/failed sync runs instead of synthetic listings.
- Hosted login handshakes are represented locally as `AccountConnectAuthSession` records with receipts, callback-state validation, visible blocked states when `BOSS_ACCOUNT_CONNECT_URL` is missing, and link creation only from a successful hosted adapter response.
- ProviderOps read-only sync can run manually through `POST /v1/resources/sync` or as a leased worker job kind, `providerops.sync.readonly`. The worker calls the same Resource Plane Kernel API; failed provider sync runs fail the job so the failure is visible as both reconciliation evidence and a job deadletter.
- Local bootstrap is primarily a CLI/operator action through `make seed`. The mutating `POST /v1/bootstrap/seed` API must fail closed unless `BOSS_BOOTSTRAP_SEED_API_ENABLED=true`; when enabled against an existing project, the actor must have `member_manage`, denied calls must record policy-denial evidence, and successful reseeds must emit `bootstrap.seed_executed`.

## Audit Failure Observability

Policy-denial, Account Connect denial, and budget-denial helpers should write durable audit events when possible. If the audit write fails, the API must not convert the original user-facing denial into success or hide the failure. It should preserve the fail-closed response and emit a structured server log with actor, project, permission or event type, target object, and failure reason so operators can investigate the missing audit event.

## Edition and Entitlement Checks

Edition checks should be capability-based, not hard-coded into UI branches.

Objects:

- `Edition`
- `LicenseEntitlement`
- `AccountConnectOrg`
- `AccountConnectAuthSession`
- `EnterpriseCluster`
- `EnterpriseIdentityProvider`
- `PackListing`
- `PackInstall`
- `MarketplacePublisher`

Community capabilities must remain useful. Enterprise capabilities should focus on scale, governance, compliance, private infrastructure, managed workers, support, and commercial safety.

## Enterprise Identity Provider Registry

Enterprise identity rollout is tracked as auditable control-plane state before any external login path is enabled.

Objects:

- `EnterpriseIdentityProvider`: OIDC, SAML, or SCIM metadata with status, test status, blocking gaps, and SecretRefs.
- `EnterpriseSamlSession`: hashed, expiring bearer session created only after a signed SAML assertion is verified against active provider metadata.
- `EnterpriseScimGroup`: external group mapped to BOSS roles and permissions.
- `EnterpriseScimUser`: external user mapped to an effective BOSS org-member record.
- SCIM 2.0 runtime endpoints: `/scim/v2/{project_id}/Groups` and `/scim/v2/{project_id}/Users` for IdP push provisioning.

Rules:

- Agents never receive raw IdP secrets; SCIM connector credentials are represented only as `SecretRef`.
- Incomplete OIDC/SAML/SCIM provider records remain visible but cannot satisfy enterprise cluster identity controls.
- If an operator marks an incomplete IdP record active, BOSS stores it as `blocked` with explicit `blocking_gaps`.
- Complete active IdP records can satisfy enterprise cluster OIDC/SAML/SCIM blocking-gap evaluation.
- SAML runtime login is available through `/saml/v2/{project_id}/metadata` and `/saml/v2/{project_id}/acs` only when `BOSS_ENTERPRISE_SAML_ENABLED=true`.
- SAML ACS accepts API JSON or form-encoded `SAMLResponse`, verifies an enveloped RSA-SHA256 XML signature on the Assertion, validates the signing certificate fingerprint, issuer, audience, time conditions, and member mapping, then stores only a hashed expiring bearer token.
- Unsigned, unsupported, expired, unmapped, or misconfigured SAML attempts fail closed and emit visible enterprise SAML events.
- IdP-facing SCIM runtime provisioning is fail-closed unless `BOSS_ENTERPRISE_SCIM_BEARER_TOKENS` is configured and the project has an active, non-blocked SCIM identity-provider record. SCIM create/update/delete maps through deterministic BOSS group/user provisioning and never stores raw SCIM bearer tokens.
- Live OIDC/SAML auth still requires environment-specific issuer, audience, JWKS/cert, redirect, and claim-mapping configuration.

## Clustering and Enterprise HA

Community baseline:

- One control plane.
- One Postgres.
- One or few local workers.
- Basic local runner.

Enterprise architecture:

- Multi-worker clustering.
- Queue partitioning.
- HA Postgres guidance/charts.
- Kubernetes deployment.
- Worker autoscaling.
- Regional worker pools.
- Tenant isolation.
- Failover.
- Private clusters.
- Advanced budget/resource governance.
- Air-gapped updates later.

The baseline Kubernetes self-hosted package is implemented under `deploy/kubernetes/base`, with an equivalent Helm chart under `deploy/helm/boss`. It runs API, web, and worker as independent workloads, applies schema/bootstrap through a Job, enables API auth by default, mounts a shared artifacts PVC, adds HPA/PDB guardrails, and leaves `boss-secrets` as an explicit operator-created Secret so fake credentials are never applied by default. Live multi-node rollout and cloud-specific ingress/DNS/storage overlays remain environment-specific.

`EnterpriseClusterValidationRun` records local readiness evidence for Kubernetes manifests, Helm charts, multi-node readiness, failover drills, backup/restore checks, private worker pools, ingress/TLS, and support/SLA readiness. Validation runs are auditable evidence and can create blocking gaps from failed required checks, but they are not claims that a live production rollout happened.

## Marketplace and Pack Registry

Pack registry objects include `PackSpec`, `PackListing`, `MarketplacePublisher`, `PackInstall`, signatures/trust metadata later, and private enterprise registries.

Pack types:

- Role packs.
- Skill packs.
- Company templates.
- Sector packs.
- Provider adapters.
- Policy packs.
- Workflow packs.
- Evaluation packs.

BOSS Account Connect provides marketplace identity for Cloud and attached Enterprise. Community must support local import/export of packs without hosted identity.

## API Surface Inventory

Agent Groups (planned):

- `GET/POST /v1/agent-groups`
- `GET/POST /v1/agent-groups/{id}/versions`
- `GET/POST /v1/agent-group-runs`
- `GET /v1/agent-group-runs/{id}/findings`

Skills and Integrations:

- `GET /v1/skills`
- `GET/POST /v1/integration-definitions`
- `GET/POST /v1/integration-connections`
- `POST /v1/integration-connections/{id}/test`
- `POST /v1/integration-connections/{id}/revoke`
- `GET /v1/skill-invocations`

Reusable Workflows:

- `GET/POST /v1/workflows`
- `GET /v1/workflows/{id}` and `POST /v1/workflows/{id}/versions`
- `GET/POST /v1/workflow-mappings` and `POST /v1/workflow-mappings/resolve`
- `GET/POST /v1/workflow-runs`
- `GET /v1/workflow-runs/{id}` and `POST /v1/workflow-runs/{id}/advance`
- `POST /v1/workflow-runs/{id}/pause`
- `POST /v1/workflow-runs/{id}/resume`
- `POST /v1/workflow-runs/{id}/cancel`
- `POST /v1/workflow-runs/{id}/migrate`
- `POST /v1/workflow-runs/{id}/migrations/rollback`
- `POST /v1/workflow-runs/{id}/steps/claim`
- `POST /v1/workflow-step-runs/{id}/heartbeat|complete|fail`
- `POST /v1/workflow-runs/{id}/compensate`
- `POST /v1/workflow-runs/{id}/compensations/claim`
- `POST /v1/workflow-compensation-runs/{id}/complete|fail`

Ground-truth and Assurance (planned):

- `GET/POST /v1/ground-truth-metrics`
- `GET /v1/ground-truth-observations`
- `GET/POST /v1/settled-conclusions`
- `GET/POST /v1/assurance-cases`
- `GET/POST /v1/claims`
- `GET/POST /v1/evidence`
- `GET/POST /v1/surface-ownership`
- `GET/POST /v1/verification-runs`

ProviderOps:

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
- `GET /v1/deployment-environment-policies`
- `POST /v1/deployment-environment-policies`
- `GET /v1/deployment-runs`
- `POST /v1/deployment-runs`
- `GET /v1/provider-health`
- `GET /v1/drift-reports`

Model Entitlement:

- `GET /v1/model-adapters`
- `GET /v1/entitlement-pools`
- `POST /v1/entitlement-pools/{id}/probe`
- `GET /v1/model-route-decisions` with filters for provider, source, model, task kind, workload class, budget state, data boundary, risk tier, routing mode, denial reason, selected-only, and denied-only audit review.
- `POST /v1/model-route-decisions/simulate`
- `GET /v1/entitlement-connection-sessions`
- `POST /v1/entitlements/sources/{source_id}/connection-sessions`
- `POST /v1/entitlement-connection-sessions/{session_id}/complete`

Budget:

- `GET /v1/budget/status`
- `POST /v1/budget/reserve`
- `POST /v1/budget/release`
- `POST /v1/budget/consume`
- `GET /v1/budget/forecast`

Commercial:

- `GET /v1/edition`
- `GET /v1/entitlements`
- `POST /v1/account-connect/link`
- `POST /v1/account-connect/unlink`
- `POST /v1/account-connect/sync`
- `GET /v1/account-connect/health-checks`
- `POST /v1/account-connect/health-checks`
- `GET /v1/account-connect/auth-sessions`
- `POST /v1/account-connect/auth-sessions`
- `POST /v1/account-connect/auth-sessions/{id}/complete`
- `GET /v1/account-connect/team-invites`
- `POST /v1/account-connect/team-invites`
- `POST /v1/account-connect/team-invites/{id}/revoke`
- `GET /v1/account-connect/billing-portal-sessions`
- `POST /v1/account-connect/billing-portal-sessions`
- `GET /v1/marketplace/listings`
- `GET /v1/marketplace/sync-runs`
- `POST /v1/marketplace/sync`
- `GET /v1/enterprise/identity-providers`
- `POST /v1/enterprise/identity-providers`
- `GET /v1/enterprise/cluster-validation-runs`
- `POST /v1/enterprise/cluster-validation-runs`
- `GET /v1/packs`
- `POST /v1/packs/install`

This endpoint inventory is the product/API contract map; `docs/product/IMPLEMENTATION_STATUS.md` remains the shipped-runtime status source for each surface.
