# Changelog

## v0.11.0 - 2026-08-09

This feature release makes BOSS materially more self-governing: model access,
agent-tool intake, improvement loops, assurance, collaboration, and work
shepherding now share durable policy, lease, budget, receipt, and evidence
boundaries.

### Added

- A sanitized, model-agnostic agent toolchain intake that inventories pinned
  upstream sources, performs bounded review without executing imported code,
  and admits only normalized, policy-approved capabilities.
- Governed autonomous improvement programs and trials with baseline evidence,
  isolated candidates, deterministic scorecards, proposal-only promotion,
  approval, canary, rollback, and audit history.
- A unified Model Gateway for SecretRef-backed API credentials and official
  local subscription sessions, with job/model leases, capacity fencing,
  budget reservations, receipted invocation, and project token/cost reporting.
- The Ground Truth and Assurance runtime: scheduled measurements, regression
  alarms, settled-conclusion tripwires, evidence-backed claims and Assurance
  Cases, single-owner production surfaces, and standing adversarial verifiers.
- Versioned executable Agent Groups and a typed Integration Registry with
  bounded member jobs, visible dissent, evidence gates, scoped connections,
  and intersection-only capability resolution.
- A Mission Control Assurance Center and improved task-launch visibility over
  the new canonical operator projections.

### Fixed

- Work shepherd decisions now distinguish dead workers, missing heartbeats,
  stranded jobs, never-claimed queues, and terminal job verdicts from ordinary
  slow work; every verdict records its supporting facts.
- Relaunch preserves the original execution profile, duplicate active work is
  suppressed, and the console immediately names and reorders launched work.
- Community deployments no longer inherit a paid-edition gate, and the web
  console uses the current API/origin and visual identity configuration.

### Release notes

- Application, engine, API client, Helm, and Kubernetes versions and image tags
  are aligned on `v0.11.0`.
- The release includes contributor-current `main` through `004b030` and the
  assurance/collaboration delivery from PR #144 without rewriting history.
- GitHub Actions remain intentionally disabled. Local clean, test, lint, docs,
  build, package, manifest, infrastructure, and smoke evidence is recorded in
  Plane BOSS-336.
- Hosted Account Connect, enterprise identity, live provider writes, and live
  cluster rollout remain fail-closed until credentials, approvals, and
  rollback evidence are explicitly configured.

## v0.10.0 - 2026-08-08

This feature release completes the governed QA benchmark and continuous
improvement delivery loop, together with the final mainline reconciliation.

### Added

- Pinned cross-layer QA benchmark applications (BugBox, HookWatch, and
  ApproveIt), launched through the normal budgeted, leased code-task pipeline
  and reported with durable plan and verification evidence.
- Evidence-aware scorecards for functionality, capability, interface, app
  quality, speed, efficiency, cost, tokens, effort, innovation, and
  reliability; missing evidence is explicitly unscored.
- A developer runner and `make qa-benchmark-e2e` canary path for queue → worker
  → terminal report → durable evaluation evidence in a configured deployment.
- A deduplicated benchmark-evaluation ledger and bounded worker sweep that
  continuously materialize changed terminal evidence without mutating product
  configuration.
- Proposal-only tuning handoff into the existing isolated evaluation, decision,
  canary, staged-rollout, and rollback controls. A developer-authored PR or
  config diff remains mandatory; BOSS never self-applies a tuning candidate.
- Deterministic all-agent catalog smoke coverage for the seeded Agent Studio
  personas.

### Release notes

- `main` was reconciled without reset or force push; the reconciliation commit
  retains both historical mainline ancestry and the canonical current tree.
- GitHub Actions remain intentionally disabled. Local focused test, lint,
  workflow, and production-build evidence is recorded in Plane; a real E2E
  benchmark still requires configured model, budget, Postgres, API, worker,
  sandbox, and repository dependencies.

## v0.9.1 - 2026-07-27

This patch release completes the BOSS-280 budget-settlement integrity work from
contributor PR #98.

### Fixed

- Settle recorded model spend before releasing reservations for
  `completed_no_pr`, non-final decomposition, and automated review-repair
  outcomes.
- Settle only the previously unbilled delta when a run resumes under a new
  reservation, preventing both dropped spend and double-counted receipts.
- Preserve the BOSS-281 Task Board lifecycle regression coverage alongside the
  new budget-settlement tests.

### Release notes

- The patch retains every v0.9.0 feature and fail-closed external rollout
  boundary.
- GitHub Actions remain intentionally disabled; local clean, lint, test, build,
  docs, manifest, infrastructure, dependency, and smoke evidence is recorded
  in Plane BOSS-282.

## v0.9.0 - 2026-07-26

This release integrates the System Configuration, project-scoped GitHub
operations, Agent Studio/A2A, and Plane-first Work Orchestration slices tracked
in BOSS-237, BOSS-246–268, and BOSS-270–279.

### Added

- Added a unified System Configuration workspace for provider connections,
  scoped resources, scaling and health, and cloud FinOps with safe credential
  lifecycle controls and partial-feed resilience.
- Added full project-scoped GitHub connection, repository binding, immutable
  topology, branch policy, fenced branch ownership, synchronization, conflict
  resolution, governed merge, reconciliation, and Mission Control operations.
- Added Agent Studio with seeded specialist personas, immutable versions,
  guided agent creation, reusable workflows, multi-agent panels, test runs,
  scoped capabilities, and a durable, auditable A2A communication bus.
- Added explainable work ranking, hierarchical resource quotas, dynamic
  allocation, durable tracked jobs, Plane-first tracker bindings, Product
  Manager ownership, embedded work visibility, Mattermost status channels,
  and bot delivery presence.
- Added a safe `make clean` target for generated caches and build artifacts.

### Changed

- Aligned application, engine, Helm, and Kubernetes metadata and image tags on
  `v0.9.0`.
- Consolidated the outstanding reviewed feature stacks onto one release commit
  while preserving their independent audit, policy, budget, lease, Receipt,
  ChangeSet, and rollback boundaries.

### Release notes

- Plane remains the mandatory system of record; the release issue and shipped
  feature tickets carry commit, tag, validation, and release evidence.
- Live Mattermost sending and unfinished provider executors remain fail-closed
  until their explicit credentials and rollout policies are configured.
- GitHub Actions are intentionally disabled; this source release is gated by
  the equivalent local clean, lint, test, build, docs, schema, smoke, manifest,
  infrastructure, and dependency validations.

## v0.8.0 - 2026-07-26

This release integrates Paramg's standalone OSS engine split, the consolidated
critical-fix branches, and the assurance/workflow product slices tracked in
Plane BOSS-175–234.

### Added

- Shipped `boss-engine` as a standalone Python distribution and container with
  the complete service graph, typed tools, engine-native agent, read-only-aware
  MCP server, CLI, compatibility shims, and package/container contract tests.
- Added reusable assurance workflows and product contracts for continuous
  ground truth, claim evidence, settled-conclusion tripwires, surface
  ownership, adversarial verification, agent groups, governed integrations,
  and mapped workflow composition.
- Added project-scoped task archive authorization/auditing, cross-project
  dispatch protection, completed-application branch lineage, advisory
  ship/repair/block disposition, and SSH deployment destination/rollback
  safeguards.
- Added Apache-2.0 licensing evidence at the repository, engine, API client,
  and specpack distribution boundaries.

### Changed

- Moved the canonical API/worker service implementation into
  `engine/boss_engine/services` while retaining identity-preserving import
  shims for existing callers.
- Aligned application, engine, Helm, and Kubernetes release metadata on
  `v0.8.0`.

### Release notes

- All external provider effects remain policy-, approval-, budget-, dedupe-,
  and Receipt-governed. Publishing this source release does not automatically
  deploy images or mutate a target environment.
- GitHub Actions are intentionally disabled; the release is gated by the
  equivalent local lint, test, build, schema, smoke, packaging, container, and
  manifest validations recorded in Plane.

## v0.7.0 - 2026-07-16

This release integrates the governed skills, deployment-promotion, durable
preview, and conversational feedback stacks tracked in Plane BOSS-35–63.

### Added

- Added a project skill registry with eager/on-demand loading, role-aware
  resolution, leased real-mode execution, runtime permission/budget/retry
  enforcement, and Receipt/CostReceipt evidence.
- Added local signed capability bundles, MCP publisher/provenance and per-tool
  risk metadata, Ed25519 trust verification, hot enable/disable, and governed
  lazy attachment of resolver-approved tools.
- Added provider-neutral CI pipeline contracts, GitHub Actions, Jenkins, and
  GitLab adapters, deterministic CI configuration proposals, customer
  DeploymentRun staged rollout, health-gated staging-to-production promotion,
  auto-stop/rollback evidence, and a live console timeline.
- Added restart-safe application previews, retained micro-VM rootfs serving,
  Docker/host fallback, command detection, strict health checks, streamed logs,
  actionable failures, route-faithful HTTP/WebSocket proxying, and execution
  substrate provenance in the console.
- Added durable per-task feedback threads, clarification and scoped/full-rerun
  proposals, permissioned confirmation before retry dispatch, completed-task
  extension, and preview route/element feedback capture.

### Release notes

- All external effects remain behind policy, budget, approval, dedupe, and
  Receipt boundaries. Provider rollback remains `rollback_pending` until an
  approved Resource Plane ChangeSet performs the provider action.
- GitHub-hosted checks are currently prevented from starting by the repository
  account billing/spending-limit gate; release validation is recorded from the
  equivalent local lint, test, build, docs, schema, smoke, and manifest gates.

## 2026-07-16 — Local skill marketplace and governed MCP (BOSS-48–50, BOSS-55–56)

- Added canonical local SkillSpec and MCP capability-bundle import/export, detached Ed25519 checksum signatures, configured trust verification, explicit untrusted overrides, and optional exclusion of untrusted project extensions.
- Added MCP publisher/provenance, per-tool risk overrides, and synthetic SkillRuntime execution with budget reservations, retries, invocation evidence, deduplicated Receipts, and normal high-risk approvals.
- Added project skill enable/disable and load-mode controls, resolver-approved lazy on-demand schema attachment, live installed-RoleSpec enforcement before invocation, scope-violation evidence, and startup reference diagnostics.

## 2026-07-15 — GitHub community agents (BOSS-84, BOSS-86–88)

- Added a strict signed `/v1/webhooks/github` ingress for four typed community events, action filtering, self-bot suppression, and delivery-deduplicated leased jobs.
- Added issue triage that shares the orchestration classifier, comments exactly one verdict, proposes accepted feature plans without dispatching them, and creates hold-default decisions.
- Added diff-only PR review with Receipt-backed GitHub reads/writes, inline defect annotations, an author-addressed value/ROI recommendation, and per-head-SHA idempotency. Fork code is never checked out or executed and reviews never merge.
- Added decision follow-ons: triage approval dispatches the proposed plan; PR approval records merge authorization while preserving the separately governed human merge path. An optional budgeted/receipted model review role is disabled by default.

## 2026-07-15 — Conversational orchestration agent (BOSS-78–82)

- Added durable actor-owned agent threads/messages, a permissioned and rate-limited chat endpoint, structured action history, and audit events.
- Added a transport-free intent router over existing governed initiators plus a feature-catalog-backed truthful capability manifest. Optional model classification is budget-reserved and Receipt-backed.
- Closed the command-proposal plan-id gap and added explicit persisted confirm-before-dispatch fan-out; revisions re-propose and ambiguous replies never dispatch.
- Added a thin Mission Control BOSS Agent panel with multi-turn history, linked outcomes, rendered plan phases, and a confirm-and-dispatch human gate.

All notable changes to BOSS are documented in this file.

## Unreleased — Gap Phase 4 (observability, testing depth, enterprise)

### Added

- Added project-scoped outbound webhook endpoints and durable deliveries with env-only secret
  references, exact-body HMAC-SHA256 signing, Receipt-backed attempts, lease retries/backoff,
  deterministic dedupe, boundary gates, delivery observability, and deadletter state.
- Added an auth-exempt signed inbound webhook receiver with raw-body constant-time verification,
  durable GitHub delivery idempotency, repository routing, `pull_request`/`check_suite`/
  `issue_comment` normalization, rejection/unrouted audit events, and reactive job dispatch.
- Added GitHub community response primitives for issue/PR comments, inline reviews, and unified
  diff reads through the existing App-token path, plus a least-privilege GitHub App manifest,
  installation runbook, and fail-closed 503 behavior when the inbound secret is absent.

- Consolidated real-time dashboard (`dashboard.py`): `GET /v1/dashboard` aggregates
  budget/queue/workload/incidents + provider-health + latest canary/eval + spend run-rate + ack
  backlog with a monotonic cursor; `GET /v1/dashboard/poll` is a bounded long-poll that returns when
  the cursor advances (real-time without a fixed-interval refresh).
- Chaos/load fault-injection harness (`chaos.py` + `chaos_runs`): `POST /v1/chaos/run` injects real
  faults — `lease_reclaim`/`worker_crash` (prove a crashed worker's job is reclaimed) and
  `deadletter_storm` (prove the anomaly detector raises an incident). `GET /v1/chaos` lists runs.
- InfraCommitment reconciler (`finops_reconciler.py` + `commitment_reconciliations`):
  `POST /v1/finops/reconcile-commitments` scores commitment utilization vs actual cost
  (on_track/under/over → review decision). `POST /v1/finops/initiative-budget` requires a human
  Decision for tier-2+ initiatives above the approval threshold.
- SOC2 evidence collection (`soc2.py`): `POST /v1/enterprise/soc2-evidence` generates a Trust
  Services Common-Criteria evidence bundle from live audit/policy/break-glass/change/proof data.

## Unreleased — Gap Phase 3 workstream E-2 (software-factory scale-up)

### Added

- Factory scale-up (`factory_scale.py`): WIP limits + queue-depth gating (`GET /v1/factory/wip`)
  so a new code task is admitted only when an in-flight slot is free.
- Fail-closed quality gate with an explicit lint stage + CI-green check, a numeric merge-confidence
  score, an optional second-model cross-check, and a stored review-packet artifact —
  `POST /v1/factory/merge-check` (preview) and `POST /v1/factory/quality-gate` (persist). Merge is
  allowed only when required gates pass AND confidence ≥ floor. New quality_gates columns:
  lint_passed, ci_green, cross_check_agreement, confidence, review_packet_artifact_id.
- Spec→chunk planner (`POST /v1/factory/chunk-plan`): splits a plan into small, independently
  shippable PR chunks (by items or by files) instead of one giant PR.

## Unreleased — Gap Phase 3 workstream E-1 (meeting consensus)

### Added

- Meeting consensus completeness (`meeting_consensus.py`): an explicit vote phase with
  quorum/threshold/default rules. `tally_votes` decides approved / rejected / no_quorum and emits
  dissent records; wired into `POST /v1/meetings/{id}/run` (supply `votes` → the engine derives
  consensus/dissent; a no-quorum vote blocks the meeting) plus a `POST /v1/meetings/tally` preview.
- Per-phase tool budgets + phase timeouts (pure config/helpers the LLM phase-machine consults).
- Stall prevention: `POST /v1/meetings/sweep-stalled` fails out meeting runs stuck past their phase
  timeout and opens an incident Decision for each — a silent stall becomes a visible item.

## Unreleased — Gap Phase 3 workstream D (comms robustness)

### Added

- Inbox acknowledgment ledger (`ack_state` table): every inbound advances `received → parsed →
  routed → actioned | deadlettered | acknowledged` with full transition history and a terminal
  flag. `GET /v1/comms/inbox/ack-state?pending_only=` is the durable "no silent drops" view.
- Email command contracts: structured subject tokens `[org:][ws:][proj:][thread:]`, deterministic
  thread IDs (stable across Re:/Fwd:), and malformed-command deadletter carrying a remediation hint.
- Slack shaping (`slack_comms.py`): `department_channel` mapping, persona prefix/signature rendering
  (`POST /v1/comms/slack/render`), and digest batching that collapses many draft Slack outbox
  messages into one digest per channel (`POST /v1/comms/slack/digest`).

## Unreleased — Gap Phase 2 (earned autonomy & controlled self-improvement)

### Added

- Self-improvement runtime: `POST /v1/self-improvement/proposals/{id}/run-canary` auto-runs the
  canary + eval-harness suites and records the outcome; `…/rollout/start` and `…/rollout/advance`
  drive a staged rollout (ship-narrow→widen) with **auto-rollback** when a health observation
  breaches the thresholds. New `rollout_state` column on `self_improvement_proposals`.
- Eval-harness regression suites (`eval_harness.py`): routing fallback chain, policy audit
  round-trip, software-factory fail-closed gate, and autonomy-ladder monotonicity — persisted to
  the new `eval_runs` table; `POST /v1/evals/run` + `GET /v1/evals`.
- Evidence-based autonomy engine (`autonomy_engine.py`): `POST /v1/roles/{role_key}/autonomy/evaluate`
  gathers a role's allowed/denied/scope-violation events since its last review and runs a governance
  review — auto-proposing an upgrade decision on clean evidence or auto-downgrading on a violation.
- Router health + rationale (`router_health.py`): `POST /v1/provider-health/poll` snapshots every
  model lane into `provider_health`; `POST /v1/router/rationale` persists the selected lane + ranked
  fallback chain + rejected sources as a `router_rationale` artifact. The `router_fallback` canary is
  now live (no longer skipped) and proves the chain is coherent before an incident.
- Skill-invocation framework (`skill_runtime.py`): enforces per-skill permission (allowed_callers),
  cost (budget reservation in the skill's category), and retry (retry.max_attempts), recording each
  call to the new `skill_invocations` table. **Mock mode** runs the full gauntlet with no side
  effects — `POST /v1/skills/{skill_id}/invoke` (API mock-only) + `GET /v1/skills/invocations`; a
  `skill_mock_invocation` canary proves the no-side-effect guarantee.
- Skill executor registry (`skill_executors.py`) with decorator/direct registration, fail-closed
  server context, Receipt-backed GitHub PR bindings, Deployment Registry bindings, and
  duplicate-effect suppression for existing provider Receipts.

## v0.6.0 - 2026-06-03

Consolidation release restoring production API contracts and commercial services after bad merge conflict resolution on main.

### Fixed

- Restored `commercial.py` hosted Account Connect, marketplace, and auth-session logic removed during merge conflict resolution.
- Restored API pagination on tasks, KPIs, receipts, and events; pack import/export HTTP routes; `GET /v1/jobs`; and SecretRef CRUD endpoints.
- Restored `@boss/api-client` pagination helpers, pack import/export, job list, and SecretRef CRUD methods.
- Restored OpenAPI contract parity with implemented routes (207 routes validated).
- Restored SpecPack `import_pack` / `export_pack_yaml` with local-upload receipt dedupe keys.
- Aligned regression tests with project-scoped SCIM member IDs and Account Connect entitlement isolation.

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
