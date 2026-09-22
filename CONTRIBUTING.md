# Contributing to BOSS

This is the engineering handbook: how to run BOSS from source, what the repository contains,
and the rules a change is expected to follow. If you are trying to *use* BOSS, start at the
[README](README.md) instead.

## Prerequisites

- Node `20.x`
- `pnpm` `9.x`
- Python `3.11+`
- Docker with Compose
- GitHub credentials available through `BOSS_GITHUB_TOKEN`, `GITHUB_TOKEN`, or the macOS keychain
- `OPENAI_API_KEY`
- Optional ProviderOps credentials for read-only connection tests, inventory sync, and billing sync: `BOSS_DIGITALOCEAN_TOKEN`, `BOSS_CLOUDFLARE_API_TOKEN`, `BOSS_SUPABASE_ACCESS_TOKEN`, `OPENAI_API_KEY` or `OPENAI_ADMIN_KEY`, and `ANTHROPIC_API_KEY` or `ANTHROPIC_ADMIN_KEY`. Supabase project creation ChangeSets should reference a local env-backed `SecretRef` such as `env:BOSS_SUPABASE_PROJECT_DB_PASS` for the database password.

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
- `go/tenant-gateway/`: optional S2 tenant gateway library/binary (not mounted; production edge is Traefik)
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

## API-first rule

- The control plane API is the primary product surface.
- `apps/web` is a client of that API, not a second source of business logic.
- New product capabilities should land in `apps/api` and `api/openapi.yaml` first, then be consumed by UI through the shared client in `packages/api-client`.
- This keeps UI swappable and makes customer/API access a first-class path rather than an afterthought.

## Design partners (private repository)

If you were invited onto this **private** repo, start here — do not assume a
public clone or Cloud tenant exists:

- Checkable week-1 path: [`docs/operations/DESIGN_PARTNER_ONBOARDING.md`](docs/operations/DESIGN_PARTNER_ONBOARDING.md)
- Engine-only Docker/MCP pack: [`engine/DESIGN_PARTNER_ONBOARDING.md`](engine/DESIGN_PARTNER_ONBOARDING.md)
- Pointer with no secret values: `make onboard`

Access is still a human GitHub invite. This is not the public OSS flip.

## GitHub bootstrap

The bootstrap script can use either an explicit GitHub token or the machine's stored macOS Git credentials:

```bash
scripts/bootstrap_github_repos.sh
```

That script creates:

- `seanheiney/boss`
- `seanheiney/boss-sandbox`

It also pushes the sandbox template and attempts to apply branch protection to `boss-sandbox/main`. If the GitHub account tier does not allow branch protection on private repositories, the script reports that entitlement limit explicitly and stops short of bypassing it.
