# BOSS architecture and contracts

The rules the control plane holds itself to, and the API surface that enforces them. Written for
people changing BOSS, not people running it — see the [README](../README.md) to run it.

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
