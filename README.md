# BOSS

BOSS, the Business Operating System Sentinel, is a financially controlled autonomous company operating system. This repository is the real control plane and dogfood environment for BOSS itself.

Current release: `v0.11.0`

## BOSS Community (open source)

**Community is a fully functional self-hosted product** — not a crippled trial of BOSS Cloud.

| | |
|---|---|
| **Install** | `curl -fsSL https://raw.githubusercontent.com/seanheiney/boss/main/install.sh \| bash` |
| **Story** | [boss.sean.network/community/](https://boss.sean.network/community/) |
| **Spec** | [`docs/product/BOSS_COMMUNITY_EDITION_SPEC.md`](docs/product/BOSS_COMMUNITY_EDITION_SPEC.md) |
| **Features** | [`docs/community/FEATURES.md`](docs/community/FEATURES.md) |
| **Quickstart** | [`docs/community/QUICKSTART.md`](docs/community/QUICKSTART.md) |

### One-line installer

```bash
curl -fsSL https://raw.githubusercontent.com/seanheiney/boss/main/install.sh | bash
```

Idiot-proof Community setup (Docker): checks the machine, prompts for your model key (BYOK), starts Postgres + API + worker + Mission Control, seeds the project, and verifies health. See [`install.sh`](install.sh) and [`scripts/install/README.md`](scripts/install/README.md).

You run the control plane, bring your own model and infrastructure keys, and use CLI, MCP, or Mission Control. Cloud is an optional Easy Connect path when you want BOSS to operate hosted team, marketplace, or managed capacity.

```text
Outcome → Plan → Work → Evidence → Decision
```

## Operating invariants

- No external effect without a `Receipt`.
- No spendy action without a `BudgetReservation`.
- No worker execution without a lease.
- No silent drops.
- No self-modification without PR, tests, approval, and rollback.

## Real-only posture

This repo does not use fake providers or synthetic side effects. The first real integrations are:

- GitHub for branch, commit, and pull-request creation against `seanheiney/boss-sandbox`
- OpenAI for coding and planning calls via `OPENAI_API_KEY`
- DigitalOcean for sandbox droplet provisioning only when `BOSS_INFRA_REAL_PROVISION_ALLOWED=true`
- ProviderOps read-only inventory and health checks for DigitalOcean, Cloudflare, Supabase, OpenAI, and Anthropic through official APIs only

If a required credential is missing, the API must fail closed and create visible decisions and events rather than silently degrading to a mock path.

## API-first rule

- The control plane API is the primary product surface.
- `apps/web` is a client of that API, not a second source of business logic.
- New product capabilities should land in `apps/api` and `api/openapi.yaml` first, then be consumed by UI through the shared client in `packages/api-client`.
- This keeps UI swappable and makes customer/API access a first-class path rather than an afterthought.

## API docs

- [`docs/api/README.md`](docs/api/README.md) — auth, conventions, domain index, discovery endpoints
- [`docs/product/API_SURFACE.md`](docs/product/API_SURFACE.md) — product-level API surface summary
- [`api/openapi.yaml`](api/openapi.yaml) — OpenAPI contract (`GET /openapi.json` when running)
- [`docs/api/ROUTE_INDEX.md`](docs/api/ROUTE_INDEX.md) — generated METHOD + path list

## Repo layout

- `AGENTS.md`: active repository rules
- `docs/api/`: HTTP API guides and generated route index
- `docs/community/`: open-source Community hub (features, quickstart)
- `docs/product/BOSS_COMMUNITY_EDITION_SPEC.md`: Community product + feature contract
- `docs/product/BOSS_CLOUD_ENTERPRISE_SPEC.md`: Cloud / Enterprise path
- `docs/product/PRD-v2.0.md`: governing product intent
- `docs/product/TECHNICAL_SPEC-v2.0.md`: active technical architecture companion
- `docs/product/ROADMAP-v2.0.md`: product maturity roadmap
- `docs/product/IMPLEMENTATION_STATUS.md`: shipped runtime status and current constraints
- `docs/operations/INFRA_RUNBOOK.md`: scaling, environment, rollout, and rollback runbook
- `docs/operations/GOLDENEYE_DEPLOYMENT.md`: goldeneye self-hosted production deployment package
- `CHANGELOG.md`: release notes
- `api/openapi.yaml`: active API contract
- `db/schema.sql`: active starter schema
- `apps/web`: Next.js executive console shell
- `apps/api`: FastAPI control plane
- `apps/worker`: Python worker shell
- `packages/api-client`: shared TypeScript client for the control-plane API
- `packages/specpack`: active spec objects, workflows, and policies
- `packages/specpack/packs`: spec-backed company pack definitions installable into projects
- `packages/specpack/modules`: packaged agent modules with contained prompts, runtime job kinds, and configuration defaults
- `templates/boss-sandbox`: starter contents for the real sandbox target repository
- `reference/boss_build_spec_pack`: original imported build pack

## Prerequisites

- Node `20.x`
- `pnpm` `9.x`
- Python `3.11+`
- Docker with Compose
- GitHub credentials available through `BOSS_GITHUB_TOKEN`, `GITHUB_TOKEN`, or the macOS keychain
- `OPENAI_API_KEY`
- Optional ProviderOps credentials for read-only connection tests, inventory sync, and billing sync: `BOSS_DIGITALOCEAN_TOKEN`, `BOSS_CLOUDFLARE_API_TOKEN`, `BOSS_SUPABASE_ACCESS_TOKEN`, `OPENAI_API_KEY` or `OPENAI_ADMIN_KEY`, and `ANTHROPIC_API_KEY` or `ANTHROPIC_ADMIN_KEY`. Supabase project creation ChangeSets should reference a local env-backed `SecretRef` such as `env:BOSS_SUPABASE_PROJECT_DB_PASS` for the database password.

## Design partners (private repository)

If you were invited onto this **private** repo, start here — do not assume a
public clone or Cloud tenant exists:

- Checkable week-1 path: [`docs/operations/DESIGN_PARTNER_ONBOARDING.md`](docs/operations/DESIGN_PARTNER_ONBOARDING.md)
- Engine-only Docker/MCP pack: [`engine/DESIGN_PARTNER_ONBOARDING.md`](engine/DESIGN_PARTNER_ONBOARDING.md)
- Pointer with no secret values: `make onboard`

Access is still a human GitHub invite. This is not the public OSS flip.

## Setup

1. Copy `.env.example` to `.env` and fill in the required secrets.
   If `localhost:5432` is already occupied on your machine, set `BOSS_POSTGRES_PORT` and make `DATABASE_URL` match that port before starting BOSS.
2. Run `make install`.
3. Run `make seed`.
4. Run `make dev`.

To opt into signed GitHub issue triage and pull-request review, install the repository-scoped GitHub App and configure `/v1/webhooks/github` using the [GitHub App community enablement runbook](docs/operations/WEBHOOKS.md#enable-the-github-app-community-surface). The integration is disabled by default, fails closed when its App credentials or inbound secret are absent, never executes fork code, and never auto-merges.

## Commands

- `make onboard`: print the private design-partner week-1 path (no secrets)
- `make dev`: start Postgres, web, API, and worker
- `make test`: run web and Python tests
- `make qa-live`: live control-plane gate against a running API (`BOSS_API_KEY` required; never printed)
- `make lint`: run TypeScript and Python checks
- `make build`: run the production web build
- `make seed`: apply schema and seed the dogfood org/workspace/project
- `make smoke`: run a local schema/bootstrap smoke check
- `make k8s-validate`: validate Kubernetes and Helm deployment guardrails
- `make infra-validate`: validate the environment scaling and rollout profile matrix
- `make release-blockers`: report external hosted/provider rollout blockers without making network calls; exits non-zero when required local prerequisites are blocked
- `make real-smoke-plan`: run a real reservation -> OpenAI plan flow against local Postgres
- `make real-smoke-pr`: run a real reservation -> queued PR job -> leased worker -> sandbox PR proposal flow
- `make real-smoke-merge`: run the full sandbox PR proposal flow and, when merge policy is enabled, approve and execute the merge job
- `make real-smoke-deploy`: run the full sandbox PR proposal -> merge -> deployment flow when both merge and deploy policies are enabled
- `make worker-drain-once`: poll the control plane once and execute at most one leased job

Goldeneye deployment uses the root `Dockerfile`, root `docker-compose.yaml`,
`deploy/goldeneye.compose.yaml`, and `app.env.tmpl`; see
`docs/operations/GOLDENEYE_DEPLOYMENT.md`. The production compose files consume
goldeneye-injected services and do not run bundled Postgres, Redis, Supabase, or
object storage.

The database-backed commands now wait for Postgres readiness before seeding, testing, or smoke execution, which avoids race conditions during local startup.

When `BOSS_SANDBOX_LOCAL_VALIDATION_ENABLED=true`, PR proposals also run real validation commands in the configured `BOSS_SANDBOX_VALIDATION_REPO_PATH` clone. Commands are split by newline or semicolon, run without a shell, must use executable names from `BOSS_SANDBOX_VALIDATION_ALLOWED_COMMANDS`, are bounded by `BOSS_SANDBOX_VALIDATION_TIMEOUT_SECONDS`, and are receipt-backed as `sandbox.validation.run`. By default BOSS fetches the proposal branch from `origin`; set `BOSS_SANDBOX_VALIDATION_FETCH_REMOTE=false` only for local test fixtures.

The executive console now supports API-driven CEO command intake plus live visibility into recent tasks, artifacts, events, decisions, deadletters, installed packs, and installed roles.

The console also exposes a Metatron status-query surface. It answers scoped operational questions directly from the live control plane and returns freshness plus explicit references instead of free-form unsupported chat.

The console is now project-scoped at runtime rather than being hard-wired to `boss-dogfood`. It lists available projects from the control plane, lets the operator switch project context, and sends that selected `project_id` through all command, decision, comms, audit, governance, and self-improvement actions.

The console is also actor-scoped through the project member directory. The seed creates default CEO, system, Metatron, VP Agents, release manager, and executive-console actors, and write APIs reject unknown actors or actors lacking the required permission.

Denied write attempts emit `policy.denied` events when BOSS can resolve the target project, keeping permission failures visible in the audit feed.

The dogfood seed now installs the spec-backed `core_v1` company pack into `boss-dogfood`, which provisions the default operating roles from `packages/specpack/roles`, including ProviderOps Manager, FinOps Sentinel, and Resource Janitor for governed resource-plane operations.

The dogfood seed now also installs the first packaged agent modules, including `metatron_operator_v1` and `codex_software_engineer_v1`, so role lineage, prompt bundles, and runtime job defaults are grounded in installable marketplace units instead of only pack metadata.

The dogfood seed also registers the initial entitlement-source inventory from `packages/specpack/models/providers.yaml`, and brokered OpenAI planning/review calls now write routing logs before execution.

ProviderOps now exposes the first Resource Plane Kernel slice. Provider accounts, provider-health checks, resource syncs, ChangeSets, deployment targets, drift reports, reconciliation runs, ResourceReceipts, CostEvents, and CostAnomalies are API-driven and audit-backed. Read-only inventory, connection testing, and billing sync are supported for the configured providers through official APIs. Billing-readable provider accounts can ingest DigitalOcean billing history, OpenAI organization costs, and Anthropic organization cost reports into duplicate-safe CostEvents with confidence labels. Provider writes must be modeled as ChangeSets; Cloudflare DNS record upsert, Cloudflare R2 bucket create, Cloudflare Worker deployment, Git-connected Cloudflare Pages deployment, DigitalOcean droplet create/destroy, DigitalOcean App Platform deployment, Supabase project create/pause, and Supabase migration apply are the first supported write skills, spendy successful actuation creates receipt-linked CostEvents with confidence labels, threshold-crossing observed provider spend switches the provider account to read-only mode with a decision card, and unsupported skills or missing credentials fail closed with ResourceReceipts/deadletters.

The executive console now includes a ProviderOps dashboard covering provider connection registration/testing, read-only resource sync, resource inventory, Resource Janitor cleanup proposals, worker-pool/autoscaler review, ChangeSet review/approval/application gates, deployment target registration, DeploymentRun planning and health checks, provider health, drift, cost, reconciliation, and ResourceReceipt visibility.

## API authentication

`/health` remains public for liveness probes. `/v1/*` routes can require API-key authentication by setting `BOSS_REQUIRE_API_AUTH=true` and at least one comma-separated key in `BOSS_API_KEYS`.

Clients may send either:

- `Authorization: Bearer <key>`
- `X-BOSS-API-Key: <key>`

If API authentication is required but no keys are configured, BOSS fails closed with `503` rather than accepting unauthenticated requests. The web console sends `BOSS_API_KEY` or `NEXT_PUBLIC_BOSS_API_KEY` through the shared API client when configured. Workers also send `BOSS_API_KEY` or `BOSS_WORKER_API_KEY` when polling the control plane.

For multi-actor environments, set `BOSS_API_KEY_PRINCIPALS` to comma-separated `api-key:member_id` or `api-key=member_id` entries. When configured, a valid API key is bound to that project member principal, and mutation payloads cannot claim a different `actor`. `GET /v1/auth/session` reports whether API auth is required, whether principal binding is active, and the resolved member id.

BOSS can also accept real OIDC bearer JWTs for `/v1/*` transport authentication by setting `BOSS_OIDC_ENABLED=true`, `BOSS_OIDC_ISSUER`, `BOSS_OIDC_AUDIENCE`, and `BOSS_OIDC_JWKS_URL`. The token must resolve to an existing project member through `BOSS_OIDC_PRINCIPAL_CLAIM` or `BOSS_OIDC_SUBJECT_PRINCIPALS`; otherwise BOSS fails closed before any write action can claim an actor.

For delegated org-wide RBAC, `POST /v1/org-members` creates an org-level member with explicit permissions. Org members can act in any project under the same org through the same `require_project_actor` checks, while cross-org access remains blocked.

## Current control-plane endpoints

- `GET /health`
- `GET /v1/auth/session`
- `GET /v1/projects`
- `GET /v1/members`
- `GET /v1/members/{member_id}`
- `POST /v1/members`
- `GET /v1/break-glass/grants`
- `POST /v1/break-glass/grants`
- `POST /v1/break-glass/grants/{grant_id}/revoke`
- `GET /v1/status`
- `POST /v1/metatron/query`
- `GET /v1/health-snapshots`
- `POST /v1/health-snapshots/refresh`
- `POST /v1/commands/propose`
- `GET /v1/tasks`
- `POST /v1/tasks`
- `GET /v1/departments`
- `POST /v1/departments`
- `GET /v1/policies`
- `POST /v1/policies`
- `GET /v1/plans`
- `POST /v1/plans`
- `GET /v1/meetings`
- `POST /v1/meetings`
- `GET /v1/jobs/{job_id}`
- `GET /v1/tasks/{task_id}`
- `GET /v1/self-improvement/proposals`
- `POST /v1/self-improvement/proposals`
- `GET /v1/self-improvement/proposals/{proposal_id}`
- `POST /v1/self-improvement/proposals/{proposal_id}/canary`
- `POST /v1/self-improvement/proposals/{proposal_id}/rollout`
- `POST /v1/self-improvement/proposals/{proposal_id}/rollback`
- `GET /v1/artifacts`
- `GET /v1/artifacts/{artifact_id}`
- `GET /v1/roles`
- `GET /v1/roles/{role_key}`
- `GET /v1/roles/{role_key}/governance`
- `POST /v1/roles/{role_key}/reviews`
- `GET /v1/packs`
- `GET /v1/packs/{pack_id}`
- `POST /v1/packs/install`
- `GET /v1/agent-modules`
- `GET /v1/agent-modules/{module_id}`
- `POST /v1/agent-modules/install`
- `GET /v1/skills`
- `GET /v1/entitlements/sources`
- `POST /v1/entitlements/sources`
- `POST /v1/router/preview`
- `GET /v1/router/logs`
- `GET /v1/providers`
- `POST /v1/providers`
- `POST /v1/providers/{provider_account_id}/test`
- `GET /v1/resources`
- `POST /v1/resources/sync`
- `GET /v1/costs`
- `GET /v1/changesets`
- `POST /v1/changesets`
- `POST /v1/changesets/{changeset_id}/approve`
- `POST /v1/changesets/{changeset_id}/apply`
- `GET /v1/resource-receipts`
- `GET /v1/worker-pools`
- `POST /v1/worker-pools`
- `GET /v1/autoscaler/plans`
- `POST /v1/autoscaler/plans`
- `POST /v1/resource-janitor/run`
- `GET /v1/deployments`
- `POST /v1/deployments`
- `GET /v1/deployment-runs`
- `POST /v1/deployment-runs`
- `POST /v1/deployment-runs/{deployment_run_id}/health-check`
- `GET /v1/provider-health`
- `GET /v1/drift-reports`
- `GET /v1/reconciliation-runs`
- `GET /v1/comms/outbox`
- `POST /v1/comms/outbox`
- `POST /v1/comms/outbox/{message_id}/queue`
- `POST /v1/comms/outbox/{message_id}/deliver`
- `GET /v1/comms/inbox`
- `POST /v1/comms/inbox/ingest`
- `POST /v1/jobs`
- `POST /v1/worker/poll`
- `POST /v1/worker/heartbeat`
- `POST /v1/worker/complete`
- `POST /v1/worker/fail`
- `GET /v1/budget/status`
- `GET /v1/budget/allocations`
- `POST /v1/budget/allocations`
- `POST /v1/budget/quote`
- `POST /v1/budget/reserve`
- `POST /v1/budget/release`
- `GET /v1/decisions/inbox`
- `POST /v1/decisions/digests`
- `GET /v1/events`
- `GET /v1/deadletters`
- `POST /v1/openai/plans`
- `POST /v1/software-factory/proposals`
- `POST /v1/software-factory/merges`
- `POST /v1/software-factory/deployments`
- `GET /v1/software-factory/rollouts`
- `POST /v1/software-factory/proposals/queue`
- `POST /v1/software-factory/merges/queue`
- `POST /v1/software-factory/deployments/queue`
- `POST /v1/infra/droplets/requests`
- `POST /v1/infra/droplets`
- `POST /v1/infra/droplets/queue`
- `GET /v1/infra/resources`
- `POST /v1/infra/resources/{resource_id}/terminate`
- `POST /v1/infra/resources/{resource_id}/terminate/queue`
- `POST /v1/infra/janitor/run`
- `GET /v1/receipts`
- `GET /v1/receipts/{dedupe_key}`
- `GET /v1/audit/export`
- `POST /v1/audit/replay-bundles`
- `POST /v1/compliance/evidence-bundles`

## Budget-governor behavior

- Spendy work should follow `quote -> reserve -> execute -> consume/release`.
- Reused or blocked spendy effects release their reservation instead of leaving budget locked.
- Spend-gated jobs without an active reservation are not leased; they create a visible decision and are cancelled explicitly rather than running silently.
- Budget allocations support parent-child envelopes for project, department, initiative, or other scoped spend. Child allocations and allocation-backed reservations are denied when they exceed the available parent allocation.
- Lease-time budget states now affect execution behavior too: `orange` defers non-critical work, and `red` halts non-`p0` jobs while freezing infrastructure provisioning plus higher-risk merge, deploy, and outbound communication effects behind visible decisions.
- Real sandbox PR proposals can now be queued as leased jobs so GitHub/OpenAI execution runs through `reservation -> job -> lease -> receipt -> artifact -> decision`.
- Real sandbox droplet provisioning now follows `reservation -> decision -> job -> lease -> receipt -> artifact -> infra_resource`, with TTL janitor queueing for cleanup.

## Audit and replay

- `GET /v1/receipts` lists receipt-backed effects as first-class API objects for a project.
- `GET /v1/audit/export` reconstructs the current command/task/job/decision/artifact/receipt/event chain, with optional task/job/decision scoping.
- `POST /v1/audit/replay-bundles` persists that export as a durable `replay_bundle` artifact so the console and external clients can capture a point-in-time run packet.
- `POST /v1/compliance/evidence-bundles` persists a durable `compliance_evidence_bundle` artifact covering project members, roles, policy records, break-glass grants, decisions, receipts, budget reservations, cost receipts, deadletters, and compliance-relevant events.
- Tasks, jobs, artifacts, and receipts now carry a `data_boundary` classification (`public`, `internal`, `confidential`, `restricted`, or `local-only`) that is visible through object APIs and audit export. Outbox and inbox messages carry the same classification through comms APIs. Job and artifact creation inherit the task boundary unless an explicit boundary is supplied.
- GitHub PR, merge, and deployment effects are blocked before receipt creation when the task boundary is `restricted` or `local-only`, producing a decision and `policy.denied` event instead of sending data to an unapproved external provider.
- Slack and email delivery effects are blocked before receipt creation when the message boundary is `restricted` or `local-only`, producing a decision and `policy.denied` event instead of sending data to an unapproved external provider.
- DigitalOcean droplet provisioning is blocked before receipt creation when the task boundary is `restricted` or `local-only`, producing a decision and `policy.denied` event instead of sending data to an unapproved external provider.
- Queued OpenAI, PR proposal, and merge work now stamps both artifacts and receipts with `created_by_job_id`, and PR-proposal reservations are attached to their worker jobs for tighter lineage.
- Approving a plan-review decision now auto-reserves the configured sandbox proposal budget and queues the next `software_factory.propose_pr` job instead of stopping at decision state only.
- Sandbox PR proposals now wait for real GitHub check-run evidence and persist that evidence into the `test_report` and `review_packet` artifacts before the PR review decision is considered mergeable.
- When local sandbox validation is enabled, sandbox PR proposals also fetch/checkout the proposal branch in an approved local clone, run allowlisted real commands, capture output into the test report, and block merge approval if any command fails or times out.
- Approving the final sandbox PR review decision creates a receipt-linked merge gate by default, and can queue a real `github.pull_request.merge` job only when `BOSS_ENABLE_SANDBOX_MERGE=true`.
- Even when merge policy is enabled, BOSS now blocks merge execution unless the linked sandbox validation report is in a passed state.
- When sandbox deploy policy is enabled, a successful merge creates a deployment review decision instead of silently treating merge as the terminal step. Approving that decision queues a real `github.deployment.create` job against the sandbox repository, backed by a receipt and a durable `deployment_result` artifact.
- If `BOSS_SANDBOX_DEPLOY_HEALTHCHECK_URL` is configured, BOSS now runs a real post-deploy HTTP health check and only treats the deploy as complete when that check passes. Failed verification creates a `deployment_health_check` artifact and a blocking follow-on decision instead of silently completing the rollout.
- Every sandbox deployment now creates a durable `deployment_rollout` artifact and queryable rollout record with staged evidence for GitHub deployment, post-deploy health, release readiness, and rollback/health-decision linkage when blocked.

## Model entitlement broker

- BOSS treats AI capacity as governed entitlement sources, not generic free compute.
- The runtime inventory now distinguishes `official_cli`, `official_coding_agent`, `official_api`, and `approved_local_model` sources.
- Public subscription sources for Codex, Claude Code, and Grok Build are modeled as official OAuth/CLI session entitlements with `auth_method`, `connection_status`, `connection_ref`, OAuth scopes, and provider-rule notes. They can be used only for owner or single-tenant work when an official session is connected; API-key sources remain the production and multi-tenant path.
- Router previews and brokered model calls write durable routing logs with selected source, selected model, denial reasons, and rejected alternatives.
- Routing decisions now include `data_boundary`; restricted and local-only work is denied for unapproved external sources, while approved local model capacity can handle local-only work when configured.
- OpenAI planning and review-packet generation now pass through the broker before execution.
- The broker fails closed when no eligible runtime-backed source is available or when provider-policy constraints disallow the workload.
- The executive console no longer labels seeded provider inventory as generically “runtime ready.” It distinguishes active capacity, credential-gated capacity, adapter-ready capacity, and registry-only inventory so the visible surface matches real backend readiness.

## Communications plane

- The comms slice now uses the canonical `outbox_messages` and `inbox_messages` tables instead of burying communication intent in chat.
- Outbound communication follows `draft -> queued -> sending -> acked` when a real provider is enabled, or transitions to `blocked`, `retrying`, or `deadlettered` with visible events when it is not.
- `comms.outbox.deliver` is a leased worker job kind, so real sends still require a job lease, a reservation, and a receipt.
- Slack is the first provider surface in this slice, but it is fail-closed by default behind `BOSS_SLACK_REAL_SEND_ALLOWED=false`. Real sends require both that flag and `BOSS_SLACK_WEBHOOK_URL`.
- Email now shares the same outbox path through a real SMTP adapter, fail-closed by default behind `BOSS_EMAIL_REAL_SEND_ALLOWED=false`. Real sends require SMTP host/from-address config and optional auth credentials.
- Decision digests are API-created `decision_digest` artifacts that group urgent decisions separately from lower-risk batched decisions. An optional channel creates an outbox draft only; sending still requires explicit queueing, reservation, lease, and receipt.
- Inbox ingestion now deterministically parses decision replies. Valid replies transition the referenced decision through the same permissioned decision service as the UI; malformed or unresolvable replies become visible `inbox_message` deadletters.
- Outbox queueing honors quiet-hours and per-channel throttling for non-urgent messages through delayed job `run_after` values. `urgent` queue requests bypass those scheduling deferrals but still require reservation, lease, and receipt before any external send.
- Outbox job failures propagate into message status and deadletters so the comms plane honors the `no silent drops` invariant.

## Self-improvement lane

- BOSS now tracks self-improvement through a durable `self_improvement_proposals` workflow instead of burying product changes in ad hoc notes or chat.
- Creating a proposal requires a change reference, diff summary, test plan, canary scope, rollback plan, and success metrics. The control plane turns that into a task, proposal artifact, and canary approval decision.
- The self-improvement API now fails closed if proposal, canary, rollout, or rollback requests omit required change-control evidence such as non-empty test plans, canary scope, rollback steps, metrics, or evidence references.
- Decision outcomes are now connected to proposal state. Approval advances the proposal, revision requests reopen it explicitly, and rejection leaves a visible rejected state instead of silent drift.
- Successful canaries automatically create the next rollout approval decision. Rollout and rollback are both recorded as first-class artifacts so BOSS can prove how it changed itself.
- The executive console now submits canary, rollout, and rollback updates with explicit metrics and evidence references instead of rendering dead-end buttons that fail validation at the API boundary.

## Health snapshots

- BOSS now has a canonical `health_snapshots` object that captures queue state, workload state, incident state, budget state, and derived top risks for a project.
- `GET /v1/status` now returns live top risks plus latest snapshot metadata, while `POST /v1/health-snapshots/refresh` persists a durable snapshot for audit and operator review.
- Snapshot risk derivation currently covers deadletters, blocked or deadlettered outbox messages, budget pressure, high-risk pending decisions, retryable jobs, and missing provider credentials.

## Role governance

- Installed roles now carry runtime governance state instead of only static pack metadata: trust score, successful-run count, violation counters, and last review or violation timestamps.
- `POST /v1/roles/{role_key}/reviews` records a durable governance review with evidence refs and metrics, updates the role trust score, and emits audit events for trust changes and detected drift.
- Review metrics can now trigger automatic autonomy downgrades for violation conditions, or create explicit autonomy-upgrade decisions when a role meets the autonomy ladder policy.
- Approved autonomy decisions now update the live role record through the control plane instead of leaving autonomy changes as spec-only intent.

## Agent modules and marketplace

- Agent logic is now a contained product surface, separate from company packs. A packaged agent module can own a role charter, prompt bundle, runtime job kinds, skill defaults, and install-time configuration.
- `GET /v1/agent-modules` exposes the marketplace catalog for a project, `POST /v1/agent-modules/install` installs a module into a project, and installed roles retain `source_module_id` and `source_module_version` lineage.
- Approved plan follow-on jobs now carry `agent_module_id`, and installed module configuration can influence runtime behavior such as proposal branch prefixes.
- OpenAI plan generation and software-factory PR proposals now resolve through installed agent modules so prompt/system behavior and capability hints are driven by the packaged module, not only hardcoded service logic.

## GitHub bootstrap

The bootstrap script can use either an explicit GitHub token or the machine's stored macOS Git credentials:

```bash
scripts/bootstrap_github_repos.sh
```

That script creates:

- `seanheiney/boss`
- `seanheiney/boss-sandbox`

It also pushes the sandbox template and attempts to apply branch protection to `boss-sandbox/main`. If the GitHub account tier does not allow branch protection on private repositories, the script reports that entitlement limit explicitly and stops short of bypassing it.
