# BOSS

BOSS, the Business Operating System Sentinel, is a financially controlled autonomous company operating system. This repository is the real control plane and dogfood environment for BOSS itself.

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

If a required credential is missing, the API must fail closed and create visible decisions and events rather than silently degrading to a mock path.

## API-first rule

- The control plane API is the primary product surface.
- `apps/web` is a client of that API, not a second source of business logic.
- New product capabilities should land in `apps/api` and `api/openapi.yaml` first, then be consumed by UI through the shared client in `packages/api-client`.
- This keeps UI swappable and makes customer/API access a first-class path rather than an afterthought.

## Repo layout

- `AGENTS.md`: active repository rules
- `docs/product/PRD-v1.0.md`: governing product intent
- `api/openapi.yaml`: active API contract
- `db/schema.sql`: active starter schema
- `apps/web`: Next.js executive console shell
- `apps/api`: FastAPI control plane
- `apps/worker`: Python worker shell
- `packages/api-client`: shared TypeScript client for the control-plane API
- `packages/specpack`: active spec objects, workflows, and policies
- `packages/specpack/packs`: spec-backed company pack definitions installable into projects
- `templates/boss-sandbox`: starter contents for the real sandbox target repository
- `reference/boss_build_spec_pack`: original imported build pack

## Prerequisites

- Node `20.x`
- `pnpm` `9.x`
- Python `3.9+`
- Docker with Compose
- GitHub credentials available through `BOSS_GITHUB_TOKEN`, `GITHUB_TOKEN`, or the macOS keychain
- `OPENAI_API_KEY`

## Setup

1. Copy `.env.example` to `.env` and fill in the required secrets.
   If `localhost:5432` is already occupied on your machine, set `BOSS_POSTGRES_PORT` and make `DATABASE_URL` match that port before starting BOSS.
2. Run `make install`.
3. Run `make seed`.
4. Run `make dev`.

## Commands

- `make dev`: start Postgres, web, API, and worker
- `make test`: run web and Python tests
- `make lint`: run TypeScript and Python checks
- `make seed`: apply schema and seed the dogfood org/workspace/project
- `make smoke`: run a local schema/bootstrap smoke check
- `make real-smoke-plan`: run a real reservation -> OpenAI plan flow against local Postgres
- `make real-smoke-pr`: run a real reservation -> queued PR job -> leased worker -> sandbox PR proposal flow
- `make real-smoke-merge`: run the full sandbox PR proposal flow and, when merge policy is enabled, approve and execute the merge job
- `make worker-drain-once`: poll the control plane once and execute at most one leased job

The database-backed commands now wait for Postgres readiness before seeding, testing, or smoke execution, which avoids race conditions during local startup.

The executive console now supports API-driven CEO command intake plus live visibility into recent tasks, artifacts, events, decisions, deadletters, installed packs, and installed roles.

The dogfood seed now installs the spec-backed `core_v1` company pack into `boss-dogfood`, which provisions the default operating roles from `packages/specpack/roles`.

The dogfood seed also registers the initial entitlement-source inventory from `packages/specpack/models/providers.yaml`, and brokered OpenAI planning/review calls now write routing logs before execution.

## Current control-plane endpoints

- `GET /health`
- `GET /v1/status`
- `POST /v1/commands/propose`
- `GET /v1/tasks`
- `POST /v1/tasks`
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
- `GET /v1/packs`
- `GET /v1/packs/{pack_id}`
- `POST /v1/packs/install`
- `GET /v1/skills`
- `GET /v1/entitlements/sources`
- `POST /v1/entitlements/sources`
- `POST /v1/router/preview`
- `GET /v1/router/logs`
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
- `POST /v1/budget/quote`
- `POST /v1/budget/reserve`
- `POST /v1/budget/release`
- `GET /v1/decisions/inbox`
- `GET /v1/events`
- `GET /v1/deadletters`
- `POST /v1/openai/plans`
- `POST /v1/software-factory/proposals`
- `POST /v1/software-factory/merges`
- `POST /v1/software-factory/proposals/queue`
- `POST /v1/software-factory/merges/queue`
- `GET /v1/receipts`
- `GET /v1/receipts/{dedupe_key}`
- `GET /v1/audit/export`
- `POST /v1/audit/replay-bundles`

## Budget-governor behavior

- Spendy work should follow `quote -> reserve -> execute -> consume/release`.
- Reused or blocked spendy effects release their reservation instead of leaving budget locked.
- Spend-gated jobs without an active reservation are not leased; they create a visible decision and are cancelled explicitly rather than running silently.
- Real sandbox PR proposals can now be queued as leased jobs so GitHub/OpenAI execution runs through `reservation -> job -> lease -> receipt -> artifact -> decision`.

## Audit and replay

- `GET /v1/receipts` lists receipt-backed effects as first-class API objects for a project.
- `GET /v1/audit/export` reconstructs the current command/task/job/decision/artifact/receipt/event chain, with optional task/job/decision scoping.
- `POST /v1/audit/replay-bundles` persists that export as a durable `replay_bundle` artifact so the console and external clients can capture a point-in-time run packet.
- Queued OpenAI, PR proposal, and merge work now stamps both artifacts and receipts with `created_by_job_id`, and PR-proposal reservations are attached to their worker jobs for tighter lineage.
- Approving a plan-review decision now auto-reserves the configured sandbox proposal budget and queues the next `software_factory.propose_pr` job instead of stopping at decision state only.
- Approving the final sandbox PR review decision creates a receipt-linked merge gate by default, and can queue a real `github.pull_request.merge` job only when `BOSS_ENABLE_SANDBOX_MERGE=true`.

## Model entitlement broker

- BOSS treats AI capacity as governed entitlement sources, not generic free compute.
- The runtime inventory now distinguishes `official_cli`, `official_coding_agent`, `official_api`, and `approved_local_model` sources.
- Router previews and brokered model calls write durable routing logs with selected source, selected model, denial reasons, and rejected alternatives.
- OpenAI planning and review-packet generation now pass through the broker before execution.
- The broker fails closed when no eligible runtime-backed source is available or when provider-policy constraints disallow the workload.

## Communications plane

- The comms slice now uses the canonical `outbox_messages` and `inbox_messages` tables instead of burying communication intent in chat.
- Outbound communication follows `draft -> queued -> sending -> acked` when a real provider is enabled, or transitions to `blocked`, `retrying`, or `deadlettered` with visible events when it is not.
- `comms.outbox.deliver` is a leased worker job kind, so real sends still require a job lease, a reservation, and a receipt.
- Slack is the first provider surface in this slice, but it is fail-closed by default behind `BOSS_SLACK_REAL_SEND_ALLOWED=false`. Real sends require both that flag and `BOSS_SLACK_WEBHOOK_URL`.
- Outbox job failures propagate into message status and deadletters so the comms plane honors the `no silent drops` invariant.

## Self-improvement lane

- BOSS now tracks self-improvement through a durable `self_improvement_proposals` workflow instead of burying product changes in ad hoc notes or chat.
- Creating a proposal requires a change reference, diff summary, test plan, canary scope, rollback plan, and success metrics. The control plane turns that into a task, proposal artifact, and canary approval decision.
- Decision outcomes are now connected to proposal state. Approval advances the proposal, revision requests reopen it explicitly, and rejection leaves a visible rejected state instead of silent drift.
- Successful canaries automatically create the next rollout approval decision. Rollout and rollback are both recorded as first-class artifacts so BOSS can prove how it changed itself.

## GitHub bootstrap

The bootstrap script can use either an explicit GitHub token or the machine's stored macOS Git credentials:

```bash
scripts/bootstrap_github_repos.sh
```

That script creates:

- `seanheiney/boss`
- `seanheiney/boss-sandbox`

It also pushes the sandbox template and attempts to apply branch protection to `boss-sandbox/main`. If the GitHub account tier does not allow branch protection on private repositories, the script reports that entitlement limit explicitly and stops short of bypassing it.
