# BOSS Implementation Status

Last updated: June 4, 2026
Release target: `v0.4.0`

This document summarizes the currently implemented BOSS runtime surfaces in this repository so the codebase, README, and product intent stay aligned.

## What is implemented now

- Canonical tenant bootstrap:
  - `orgs`
  - `workspaces`
  - `projects`
  - dogfood seed for `boss-dogfood`
- Core control-plane objects:
  - tasks
  - task hierarchy, dependencies, owners, and due dates
  - departments
  - policy records
  - plans
  - meetings with structured run outputs, transcript/minutes artifacts, linked action tasks, decision cards, blocked state, and dissent records
  - jobs and leases
  - decisions
  - artifacts
  - receipts
  - budget reservations and cost receipts
  - events
  - deadletters with incident packets, investigation tasks, and safe job requeue links
  - health snapshots
- API-first control plane:
  - project discovery for runtime-scoped clients
  - edition, license-entitlement, and Account Connect hosted-auth/link/unlink/sync APIs; Community runs without hosted auth, while Cloud and Enterprise Cloud editions fail closed until linked, and hosted auth/entitlement sync use configured real HTTP adapters or fail closed visibly
  - Account Connect health-check, hosted marketplace catalog sync, auth-session, team-invite, and billing-portal session APIs with read-only hosted readiness/catalog evidence, receipt-backed hosted writes, visible blocked records when hosted configuration is missing, and Mission Control controls/history
  - enterprise cluster/trust records for attached/detached enterprise posture, identity/audit/governance/private-worker controls, support/SLA metadata, blocking-gap visibility, durable cluster validation packet artifacts, and safe-HOLD blocking decisions for failed required validation checks
  - enterprise OIDC/SAML/SCIM identity-provider metadata APIs with SecretRefs, blocking-gap evaluation, cluster identity-control integration, and Mission Control controls
  - Enterprise SCIM group/user provisioning APIs that map active users to BOSS org members with explicit merged roles and permissions
  - IdP-facing SCIM 2.0 `/scim/v2/{project_id}/Groups` and `/Users` endpoints with bearer-token fail-closed auth, active-provider gating, list/filter/create/replace/deactivate behavior, and the same org-member RBAC mapping as internal SCIM APIs
  - CEO command intake
  - canonical department, policy, plan, and meeting CRUD/list/run surfaces
  - task and job inspection
  - deterministic COO queue inspection and durable ops digest artifacts
  - deadletter remediation APIs for incident packet creation and terminal job requeue
  - change journal list/detail for diff, review, deployment, self-improvement, infrastructure, and role-governance artifacts
  - budget quote, reserve, release, and status
  - hierarchical budget allocations
  - budget dashboard forecast, burn-rate, time-to-cap, and top-cost-driver fields
  - infrastructure provisioning review, direct execution, queued execution, resource inspection, termination, and TTL janitor control
  - decision inbox and transitions
  - decision digest artifact creation with optional outbox draft handoff
  - artifact, receipt, event, and audit export access
  - replay bundle creation
  - Metatron status query
  - comms inbox and outbox
  - self-improvement proposal workflow
  - role and pack discovery/install
  - RoleSpec scope checks with drift events, trust changes, and automatic autonomy downgrade
  - agent-module catalog and install/uninstall/rollback lifecycle with marketplace metadata, required-entitlement gates, denied-install records, rollback snapshots, default initial-agent installation, and runtime prompt resolution
  - entitlement source and routing-log visibility, including API-key and official OAuth/CLI session connection lifecycle, capacity-state visibility, and structured route rejection explanations for OpenAI Codex, Anthropic Claude Code, and xAI Grok Build subscription sources
  - Model Entitlement Broker v2 APIs for model adapter inventory, entitlement pool inventory, official subscription connection-session start/list/complete records, idempotent capacity probes with durable `CapacityReceipt` rows, and canonical filterable `ModelRouteDecision` simulate/list surfaces
  - ProviderOps provider accounts, connection tests, resource inventory sync through manual API/UI or leased `providerops.sync.readonly` worker jobs, durable provider inventory snapshot artifacts for successful and failed reconciliation runs, read-only DigitalOcean/OpenAI/Anthropic billing sync, cost ledger reads, duplicate-safe BillingAccount/CostEvent records, spendy ChangeSet-to-CostEvent recording, cost anomalies with read-only provider kill switches, ChangeSet planning/approval/apply attempts, real Cloudflare DNS record upsert actuation, real Cloudflare Worker deployment actuation, real Git-connected Cloudflare Pages deployment actuation, real DigitalOcean droplet create/destroy actuation, real DigitalOcean App Platform deployment actuation, real Supabase project create/pause actuation, real Supabase migration apply actuation, ResourceReceipts, deployment targets, provider health, drift reports, and reconciliation-run visibility
  - DeploymentRun planning with release-packet artifacts, provider deployment ChangeSet linkage, BudgetReservation enforcement for spendy deploys, real Cloudflare Worker, Cloudflare Pages, DigitalOcean App Platform, and Supabase migration execution, real health-check execution, rollback artifacts, and health-failure decisions
  - Resource Janitor report generation and approval-required cleanup ChangeSet proposals for orphaned/expired non-production resources
  - ProviderOps Manager, FinOps Sentinel, Infra Steward, Deployment Manager, Resource Janitor, Secrets Steward, and Model Spend Analyst roles installed by Core Pack v1 with complete RoleSpec contracts, provider-boundary permissions, and contained agent-module prompt bundles
  - Budget-Aware Autoscaler worker-pool registry and ScalePlan runs that apply local desired capacity, enter degraded mode under red budget, and turn cloud spillover into BudgetReservation-backed ChangeSets instead of direct provider calls
  - project member directory and actor-scoped write authorization
- Worker runtime:
  - pull-based Postgres leasing
  - resolved agent module, role, prompt, invariant, cadence, eval-rubric, skill allow/deny, escalation, and runtime-contract context attached to leased `agent.*` jobs before execution, with API coverage for every seeded initial module/job kind
  - heartbeat and TTL enforcement
  - completion, failure, retry, reclaim, and deadletter handling
  - deadletter remediation that creates linked replacement jobs instead of mutating terminal job history
  - queue inspection/digest support that summarizes ready jobs, stale leases, retry pressure, deadletters, pending decisions, blockers, and recommended actions
  - role-specific initial-agent worker strategies for every advertised `agent.*` capability, including output contracts, required sections, evidence requirements, safety gates, decision triggers, grounding status, and durable `agent_brief` artifacts with audit events
  - private enterprise marketplace validation jobs that produce durable `entitlement_gate_report` artifacts instead of inert module job declarations
  - leased infrastructure create/delete job kinds
  - budget-state claim-time controls
- Budget governor behavior:
  - spend requires reservation
  - allocation-backed reservations cannot exceed active allocation availability
  - child allocations cannot exceed parent available budget
  - duplicate or blocked effects release budget
  - `orange` defers non-critical work
  - `red` halts non-`p0` work
  - `red` freezes higher-risk merge, deploy, and outbound comms effects
  - `red` freezes infrastructure provisioning before lease claim
- Software factory loop:
  - command -> plan artifact -> plan approval decision
  - approved plan -> reserved PR proposal job
  - installed agent module selected into queued proposal work
  - PR proposal -> receipt-backed GitHub draft PR
  - GitHub validation evidence captured into `test_report` and `review_packet`
  - optional receipt-backed local sandbox validation commands captured into `test_report` and `review_packet`
  - PR review approval -> merge gate or merge job, depending on policy
  - merge -> deployment review decision
  - deployment review approval -> deployment job, depending on policy
  - optional post-deploy health verification with blocking follow-on decision on failure
  - durable deployment rollout records with staged evidence and rollback/health-decision linkage
- Communications reliability:
  - outbox drafts, queueing, delivery, ACK, blocked, retrying, and deadlettered states
  - blocked delivery emits visible events and decision cards
  - outbox queue/delivery rejects cross-project message references
  - quiet-hours and per-channel throttling defers non-urgent outbox delivery jobs through `run_after`
  - urgent outbox delivery queue requests bypass quiet-hours/throttle deferral while preserving budget, lease, and receipt gates
  - deterministic inbound decision replies transition decisions through the decision service or deadletter visibly
  - Slack delivery remains disabled unless explicit policy and webhook config are present
  - SMTP email delivery remains disabled unless explicit policy and SMTP config are present
- Governance surfaces:
  - optional API-key authentication for `/v1/*` routes with fail-closed missing-key behavior
  - optional API-key principal binding so authenticated keys cannot impersonate another project actor
  - optional OIDC bearer JWT authentication with JWKS signature verification, issuer/audience checks, principal-claim or subject-map resolution to project members, and fail-closed missing-provider-config behavior
  - delegated org-wide RBAC through `org_members`, with org delegates available across projects in the same org, selectable in the console actor switcher, manageable through the Org Delegation UI, and still bound by explicit permissions
  - auth-session introspection through `GET /v1/auth/session`
  - break-glass access grants with requester identity, project or org target member, reason, scope, TTL, temporary permissions, event logs, revocation, expiry, and mandatory postmortem task creation
  - enterprise cluster/trust writes gated by explicit `enterprise_manage` permission and audited with `enterprise.cluster_upserted` events
  - enterprise cluster validation-run writes gated by `enterprise_manage`, with required-check blocking gaps, durable validation packet artifacts, safe-HOLD blocking decisions for failed required checks, and `enterprise.cluster_validation_recorded` events
  - enterprise identity-provider writes gated by `enterprise_manage`, audited with `enterprise.identity_provider_upserted`, and blocked visibly when required OIDC/SAML/SCIM metadata is incomplete
  - enterprise SCIM provisioning writes gated by `enterprise_manage`, with group/user audit events and active-user mapping into the same org-member RBAC directory used by the rest of BOSS
  - Core Pack install for dogfood
  - packaged agent-module catalog, entitlement-aware install flow, install governance records, and role lineage
  - deterministic RoleSpec validation for pack roles and module-provided roles
  - deterministic SkillSpec validation for typed IO, cost, retry, permissions, data boundary, policy, and real-only mode
  - model entitlement broker with routing logs, official subscription connection checks, durable connection sessions, connect/disconnect APIs, capacity-state fail-closed denials, multi-tenant denials for personal/public subscription sources, capacity receipt dedupe, and filterable ModelRouteDecision API/Mission Control audit surfaces
  - data-boundary-aware routing that blocks restricted/local-only work from unapproved external model sources and records the boundary in routing decisions
  - data-boundary-aware GitHub effect gates that block restricted/local-only PR, merge, and deployment effects before receipts or external calls are opened
  - data-boundary-aware comms records and Slack/email delivery gates that block restricted/local-only messages before receipts or external calls are opened
  - data-boundary-aware infrastructure gates that block restricted/local-only provisioning before receipts or external calls are opened
  - runtime role governance with trust scoring, review artifacts, and autonomy-change records
  - autonomy ladder preflight checks that emit allowed/denied events before role-governed actions
  - project members seeded for CEO, system, Metatron, VP Agents, release manager, and executive-console actors
  - permission checks on command, task, decision, comms, health, audit, pack, module, entitlement, role-governance, and self-improvement writes
  - denied write attempts emit `policy.denied` events when the project can be resolved
  - self-improvement proposal, canary, rollout, and rollback records
  - fail-closed self-improvement evidence gates for tests, canary scope, rollback steps, metrics, and evidence refs
  - audit export and replay-bundle artifact generation
  - compliance evidence bundle generation for members, roles, policies, break-glass grants, decisions, receipts, reservations, cost receipts, deadletters, and compliance-relevant events
  - data-boundary classification persisted on tasks, jobs, artifacts, and receipts, with task-to-job/artifact inheritance and audit export visibility
  - Resource Plane Kernel enforcement that fails closed when provider credentials are missing, blocks unsupported/destructive provider actuation, supports Cloudflare DNS record upsert, Cloudflare R2 bucket create, Cloudflare Worker/Pages deployment, DigitalOcean droplet create/destroy, DigitalOcean App Platform deployment, Supabase project create/pause, and Supabase migration apply through official adapters, records visible provider health, deadletter, event, Receipt, ResourceReceipt, CostEvent, and CostAnomaly records, and can switch provider accounts to read-only mode when observed provider cost crosses the freeze threshold
  - Deployment health checks that perform real HTTP/HTTPS checks through the Resource Plane boundary and create rollback decisions instead of fake success on failure
  - Resource Janitor safety checks that skip production resources, unsupported cleanup skills, read-only/destructive-disabled provider accounts, and duplicate open cleanup proposals
  - Budget Governor precedence over autoscaling, including red-budget degraded-mode decisions and approval-gated cloud worker ChangeSets
- Executive console grounding:
  - runtime project selection instead of fixed single-project UI scope
  - active actor selection from the project and org member directories, passed through all console write actions
  - auth-session visibility for transport authentication method, principal binding, and OIDC enablement
  - org delegation management for organization-wide actors and permissions
  - commercial edition and Account Connect panel with edition status, local-vs-hosted auth posture, license capabilities, hosted-org link/sync/unlink controls, hosted readiness checks, and entitlement rows
  - enterprise trust panel with cluster posture, blocking gaps, support/SLA/compliance metadata, and SCIM group/user provisioning controls that write through the API
  - keyboard skip navigation and explicit accessible names for dynamic action controls
  - Metatron activity queries grounded in live event data
  - self-improvement canary, rollout, and rollback submission with required evidence payloads
  - API-driven agent marketplace visibility and module installation
  - agent module entitlement requirements, hosted marketplace listing/sync history, and install/denial history visible from the marketplace panel
  - entitlement readiness labels that distinguish active, credential-gated, adapter-ready, and registry-only sources
  - entitlement auth, connection-state, and capacity-state labels for API keys, official CLI OAuth, device OAuth, and local-host sources, with console actions for official session connect/disconnect
  - ProviderOps dashboard for provider-account registration/testing, inventory sync, Resource Explorer, Resource Janitor cleanup proposals, ChangeSet Review, Deployment Registry, provider health, drift, cost, reconciliation, and ResourceReceipt visibility
  - ProviderOps dashboard controls for worker-pool registration and Budget-Aware Autoscaler review, with ScalePlan history and ChangeSet/decision references
  - Deployment Registry controls for planning DeploymentRuns and running deployment health checks with release, health, rollback, ChangeSet, and decision references

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
- `BOSS_SANDBOX_LOCAL_VALIDATION_ENABLED`
- `BOSS_SANDBOX_VALIDATION_REPO_PATH`
- `BOSS_SANDBOX_VALIDATION_COMMANDS`
- `BOSS_SANDBOX_VALIDATION_ALLOWED_COMMANDS`
- `BOSS_SANDBOX_VALIDATION_TIMEOUT_SECONDS`
- `BOSS_SANDBOX_VALIDATION_OUTPUT_LIMIT_CHARS`
- `BOSS_SANDBOX_VALIDATION_FETCH_REMOTE`
- `BOSS_REQUIRE_API_AUTH`
- `BOSS_API_KEYS`
- `BOSS_API_KEY_PRINCIPALS`
- `BOSS_OIDC_ENABLED`
- `BOSS_OIDC_ISSUER`
- `BOSS_OIDC_AUDIENCE`
- `BOSS_OIDC_JWKS_URL`
- `BOSS_OIDC_PRINCIPAL_CLAIM`
- `BOSS_OIDC_ALLOWED_ALGORITHMS`
- `BOSS_OIDC_SUBJECT_PRINCIPALS`
- `BOSS_EDITION`
- `BOSS_ACCOUNT_CONNECT_URL`
- `BOSS_ACCOUNT_CONNECT_TOKEN`
- `BOSS_ACCOUNT_CONNECT_TIMEOUT_SECONDS`
- `BOSS_ENTERPRISE_SCIM_BEARER_TOKENS`
- `BOSS_DAILY_CAP_USD_MICROS`
- `BOSS_MONTHLY_CAP_USD_MICROS`
- `BOSS_BURST_CAP_USD_MICROS_PER_MIN`
- `BOSS_SOFTWARE_FACTORY_PR_BUDGET_USD_MICROS`
- `BOSS_COMMS_MESSAGE_BUDGET_USD_MICROS`
- `BOSS_COMMS_QUIET_HOURS_ENABLED`
- `BOSS_COMMS_QUIET_HOURS_START_UTC`
- `BOSS_COMMS_QUIET_HOURS_END_UTC`
- `BOSS_COMMS_THROTTLE_MAX_QUEUED_PER_HOUR`
- `BOSS_COMMS_THROTTLE_DEFER_MINUTES`
- `BOSS_EMAIL_REAL_SEND_ALLOWED`
- `BOSS_EMAIL_SMTP_HOST`
- `BOSS_EMAIL_SMTP_PORT`
- `BOSS_EMAIL_SMTP_USERNAME`
- `BOSS_EMAIL_SMTP_PASSWORD`
- `BOSS_EMAIL_SMTP_USE_TLS`
- `BOSS_EMAIL_SMTP_USE_SSL`
- `BOSS_EMAIL_FROM_ADDRESS`
- `BOSS_EMAIL_FROM_NAME`
- `BOSS_INFRA_REAL_PROVISION_ALLOWED`
- `BOSS_DIGITALOCEAN_TOKEN`
- `BOSS_CLOUDFLARE_API_TOKEN`
- `BOSS_SUPABASE_ACCESS_TOKEN`
- `BOSS_SUPABASE_PROJECT_DB_PASS` for local env-backed Supabase project-create `SecretRef` values such as `env:BOSS_SUPABASE_PROJECT_DB_PASS`
- `OPENAI_ADMIN_KEY` or `OPENAI_API_KEY` for OpenAI organization cost sync, depending on the provider account SecretRef and permissions
- `ANTHROPIC_ADMIN_KEY` or `ANTHROPIC_API_KEY` for Anthropic organization cost reports, depending on the provider account SecretRef and permissions
- `BOSS_DIGITALOCEAN_DEFAULT_IMAGE`
- `BOSS_DIGITALOCEAN_DEFAULT_TAGS`
- `BOSS_DIGITALOCEAN_VPC_UUID`
- `BOSS_DIGITALOCEAN_SSH_KEY_IDS`

## What is intentionally still constrained

- Identity is currently project/org-member based with optional API-key principal binding and standards-based OIDC bearer JWT verification. Actor ids must resolve to active project members or delegated org members with the required permission, and bound API/OIDC principals cannot impersonate another actor. Live external IdP rollout requires environment-specific issuer, audience, JWKS URL, and claim/subject mapping configuration.
- Commercial edition and Account Connect state are persisted locally and API-visible. Community mode remains useful without hosted BOSS auth. Cloud and Enterprise Cloud modes fail closed until a hosted org link exists. Local agent-module marketplace lifecycle now supports entitlement-gated install, rollback from saved snapshots, and uninstall with base-role restoration. Hosted Account Connect health checks, hosted marketplace catalog syncs, auth sessions, entitlement sync, team-invite creation/revoke, and billing portal session creation are available through `BOSS_ACCOUNT_CONNECT_URL` and otherwise create visible blocked records/events rather than fake success, but live hosted Account Connect service deployment, full hosted team/billing service operation, and hosted marketplace distribution still require external service configuration.
- Enterprise cluster/trust records are implemented as planning and governance state. They normalize SSO/OIDC/SAML/SCIM, RBAC/ABAC, audit retention/export, private worker, compliance-pack, and support/SLA posture and surface blocking gaps. Enterprise identity-provider metadata records now track OIDC, SAML, and SCIM configuration with SecretRefs and can satisfy cluster identity-control gaps when complete and active. SCIM group/user provisioning now supports both internal admin APIs and IdP-facing SCIM 2.0 push endpoints that fail closed unless `BOSS_ENTERPRISE_SCIM_BEARER_TOKENS` and an active SCIM provider record are configured. Baseline Kustomize and Helm self-hosted packages now provide API/web/worker workloads, bootstrap Job, HPA/PDBs, service wiring, shared artifacts PVC, and secret-template guardrails. Cluster validation runs create durable validation packet artifacts and safe-HOLD decisions for failed required checks, but BOSS does not yet provide a verified live multi-node rollout in a target cluster or external support-system integrations without environment-specific credentials and rollout approvals.
- Local sandbox validation is implemented as allowlisted, no-shell commands in a configured sandbox clone. It is disabled by default and fails closed when enabled without repo path, commands, clean git state, or allowed executables.
- Deployment rollout tracking now records staged GitHub deployment, post-deploy health, release-readiness, and rollback/health-decision evidence. Real Cloudflare Worker deployment, Git-connected Cloudflare Pages deployment, DigitalOcean provisioning for sandbox droplets and DigitalOcean App Platform deployments, Supabase project creation, and Supabase migration apply remain fail-closed unless official credentials, env-backed SecretRefs, and required policy switches are configured.
- ProviderOps read-only adapter calls support DigitalOcean, Cloudflare, Supabase, OpenAI, and Anthropic through official APIs. Each reconciliation run now creates a durable `providerops_inventory_snapshot` artifact linked from run metadata, including failed runs with deadletter/error references. Billing-readable provider accounts can ingest DigitalOcean billing history, OpenAI organization costs, and Anthropic organization cost reports into duplicate-safe CostEvents with explicit confidence labels; unsupported billing providers fail closed with deadletters instead of invented estimates. Spendy provider ChangeSets now create receipt-linked CostEvents with explicit confidence labels after successful actuation, and threshold-crossing observed provider cost creates a CostAnomaly, decision, and provider read-only kill switch. Provider write actuation is currently constrained to explicitly implemented skills: Cloudflare DNS record upsert, Cloudflare R2 bucket create, Cloudflare Worker deployment, Git-connected Cloudflare Pages deployment, DigitalOcean droplet create/destroy, DigitalOcean App Platform deployment, Supabase project create/pause, and Supabase migration apply are supported through the Resource Plane Kernel, while unsupported provider skills and missing credentials fail closed with ResourceReceipts/deadletters until each skill receives explicit actuation support.
- Real Slack/email delivery remains disabled unless policy and provider credentials explicitly permit it.
- Enterprise SSO rollout remains environment-specific, but the runtime now supports real OIDC verification plus delegated org-wide RBAC in the current member directory.

## Local verification baseline

The current local release-grade baseline for this repo is:

- `make lint`
- `make test`
- `make build`
- `make smoke`

The latest passing local state at the time of this update was:

- API/worker tests and web tests pass under the current suite
- production web build passed
- smoke passed
- OpenAPI parsed successfully

## Where to look next

- Product intent: [PRD-v2.0.md](/Volumes/DataSSD1/test/boss/docs/product/PRD-v2.0.md)
- Technical spec: [TECHNICAL_SPEC-v2.0.md](/Volumes/DataSSD1/test/boss/docs/product/TECHNICAL_SPEC-v2.0.md)
- Active contract: [api/openapi.yaml](/Volumes/DataSSD1/test/boss/api/openapi.yaml)
- Runtime overview: [README.md](/Volumes/DataSSD1/test/boss/README.md)
- Entitlement policy: [MODEL_ENTITLEMENT_BROKER.md](/Volumes/DataSSD1/test/boss/docs/product/MODEL_ENTITLEMENT_BROKER.md)
