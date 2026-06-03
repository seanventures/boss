# BOSS Technical Specification v0.1

## Architecture

BOSS is a SaaS control plane with a stateless execution plane.

Control plane responsibilities:

- Tenant boundaries: org/workspace/project.
- Canonical objects: task, decision, policy, job, receipt, artifact, event, budget.
- Policy and budget enforcement.
- Router and skills registry.
- Decision inbox and executive console APIs.

Execution plane responsibilities:

- Stateless workers lease jobs.
- Workers run bounded workflows.
- Workers create artifacts and receipts.
- Workers never bypass policy/budget/receipt boundaries.

Comms plane responsibilities:

- Outbox/inbox messages.
- ACK, escalation, deadletter.
- Slack/email later; mock outbox in v0.

## v0 deployment shape

Local development:

- `apps/web`: Next.js frontend.
- `apps/api`: FastAPI API.
- `apps/worker`: Python worker.
- `packages/specpack`: YAML specs.
- `db/schema.sql`: starter schema.
- Postgres via Docker Compose.

Production later:

- API service.
- Web service.
- Worker pools by job kind.
- Postgres.
- Object store.
- LiteLLM gateway.
- OpenCost/KEDA/Karpenter if Kubernetes is adopted.

## Data model overview

Core tables:

- `orgs`
- `workspaces`
- `projects`
- `roles`
- `agent_module_installs`
- `tasks`
- `decisions`
- `jobs`
- `job_leases`
- `job_attempts`
- `receipts`
- `artifacts`
- `events`
- `budget_policies`
- `budget_allocations`
- `budget_reservations`
- `cost_receipts`
- `outbox_messages`
- `inbox_messages`
- `deadletters`

## Job state machine

States:

- `queued`
- `leased`
- `running`
- `succeeded`
- `failed_retryable`
- `failed_final`
- `deadlettered`
- `cancelled`

Transitions:

- `queued -> leased` when worker claims job.
- `leased -> running` when worker starts.
- `running -> succeeded` on success.
- `running -> failed_retryable` on transient failure with attempts left.
- `running -> failed_final` on terminal failure or exhausted attempts.
- `failed_final -> deadlettered` when isolated for human review.
- Any non-terminal state -> `cancelled` by policy/operator.

Lease fields:

- `lease_owner`
- `lease_token_hash`
- `lease_expires_at`
- `heartbeat_at`
- `attempt`

Implementation guidance:

- Use `SELECT ... FOR UPDATE SKIP LOCKED` for v0 Postgres leasing.
- Workers must heartbeat before `lease_expires_at`.
- Expired leases may be reclaimed.
- Attempts are bounded.

## Exactly-once side-effect strategy

BOSS provides practical exactly-once effects through idempotency:

1. Compute a stable `dedupe_key` for intended effect.
2. Insert receipt with unique `(scope, dedupe_key)` before the effect.
3. If receipt exists and succeeded, return previous result.
4. If receipt exists and in progress, follow recovery rules.
5. Execute side effect only through adapter.
6. Update receipt with provider reference or failure.

In v0, external effects are mocked, but the receipt path is real.

## Budget governor

Every spendy action follows:

quote → reserve → execute → consume/release → cost receipt.

Budget states:

- `green`: normal.
- `yellow`: prefer cheap models/tools.
- `orange`: defer non-critical jobs; freeze expensive tiers.
- `red`: halt non-P0 jobs; freeze provisioning and Tier-2+ actions.

Enforcement points:

- job leasing
- model/tool invocation
- infra provisioning
- outbound comms
- merge/deploy later

## Skills and routing

A SkillSpec defines:

- id
- kind
- input/output schema
- cost estimator
- risk tier
- retry policy
- permissions
- mock mode

Model calls are skills too. The router chooses a model/tool based on:

- task kind
- budget state
- risk tier
- data boundary
- capability score
- fallback health

Every route decision creates a router log event.

## Agent modules and marketplace

BOSS packages worker/agent logic as installable `AgentModule` units rather than scattering prompts and runtime defaults across unrelated services.

An AgentModule may contain:

- role identity and charter
- prompt bundle
- runtime job kinds
- default skill bindings
- install-time configuration schema
- marketplace metadata

Implementation guidance:

- Store module installation state separately from pack installation state.
- Keep role lineage back to `source_module_id` and `source_module_version`.
- Allow company packs to provision baseline roles, but let installed modules override or enrich the runtime role spec for governed job kinds.
- Resolve runtime behavior through installed modules when a job kind is claimed by a module.
- Keep module packaging deterministic and spec-backed so modules can be sold, versioned, and installed through a marketplace later without changing core execution rules.
- Module selection must not bypass budget, receipt, approval, entitlement, or lease enforcement.

## Self-improvement safety

BOSS may observe failures and propose improvements. It cannot silently apply them.

Self-improvement workflow:

observe → propose → test → canary → approval → rollout → monitor → rollback if needed.

Required artifacts:

- improvement proposal
- diff or PR
- test plan
- rollback plan
- canary result

## API design

Use typed JSON APIs. Every mutation should emit an event. Every effect should emit or reference a receipt.

Core endpoints are in `api/openapi.yaml`.

## Testing requirements

Minimum automated tests:

- task creation from command
- decision approve/reject transitions
- job lease claim/heartbeat/complete
- expired lease reclaim
- dedupe receipt prevents duplicate effect
- budget reservation denies over-cap action
- outbox failure becomes deadletter
- smoke flow from command to decision card
