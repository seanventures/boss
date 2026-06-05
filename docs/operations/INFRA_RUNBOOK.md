# BOSS Infra Operations Runbook

Status: operational companion to `docs/product/PRD-v2.0.md` and `docs/product/TECHNICAL_SPEC-v2.0.md`

This runbook defines how BOSS should be scaled and operated without bypassing the core invariants:

- No provider write without a `ChangeSet`.
- No spendy action without a `BudgetReservation`.
- No external/provider action without a `Receipt` or `ResourceReceipt`.
- No worker execution without a lease.
- No production DNS/deploy change without approval and rollback evidence.
- Budget Governor always wins over Autoscaler.

The machine-readable environment contract is `deploy/environments/boss-infra-profiles.yaml`. Validate it with:

```sh
make infra-validate
```

## Environment Progression

| Environment | Purpose | Runtime | Scale Posture | External Claim |
| --- | --- | --- | --- | --- |
| `local_dogfood` | Development, smoke, owner dogfood | Docker Compose | Manual local workers | No external rollout claim |
| `staging_cluster` | Kubernetes rollout rehearsal | Helm or Kustomize | HPA plus BOSS worker pools | Requires live staging verification |
| `production_control_plane` | Customer-facing Cloud/Enterprise | Helm on managed Kubernetes | HPA plus budget-aware BOSS autoscaler | Requires production approval and evidence |
| `enterprise_private_cluster` | Customer-owned private deployment | Helm or Kustomize | Private worker pools | Requires customer infra validation |

## Control Plane Topology

Run API, web, and worker as separate workloads:

- API owns the control-plane routes and policy enforcement.
- Web is an API client only.
- Workers are stateless and pull jobs through leases.
- Postgres is the durable system of record for tasks, jobs, leases, receipts, events, budgets, decisions, and ProviderOps state.
- Artifacts should move from local filesystem/PVC to object storage before heavy production scale.

For production and private enterprise, use managed or customer-managed HA Postgres with backups and a restore drill. Do not treat the current RWX PVC package as the final artifact strategy for high-scale production.

## Scaling Rules

Kubernetes HPA can scale pods, but BOSS decides whether work may run.

Use three layers:

1. Kubernetes HPA for API/web/worker CPU pressure.
2. BOSS worker pools for job-class capacity and queue pressure.
3. Resource Plane / ProviderOps for cloud spillover, provider health, and cost-aware capacity.

Worker pools should be separated by risk and effect type:

- `general`: status, planning, task/artifact work.
- `providerops`: read-only inventory, health, reconciliation, drift.
- `release`: deployment planning, health checks, release evidence.
- `local_runner`: restricted/local-only data, local models, customer-held provider secrets.

Do not scale effectful provider workers just because queue depth is high. They require policy, budget, approval, provider health, and ResourceReceipt paths.

## Budget And Kill Switches

Every environment profile must keep these switches available:

- Freeze all provisioning.
- Freeze destructive actions.
- Freeze model API spend.
- Freeze public-cloud spillover.
- Enter read-only provider mode.
- Drain cloud workers.

When budget state is `red` or `frozen`, do not scale paid/provider capacity. Drain or defer noncritical work and create a decision card for the operator.

## ProviderOps Actuation

Provider changes must follow:

```text
Discover -> Model -> Quote -> Reserve -> Approve -> Actuate -> Receipt -> Reconcile -> Optimize
```

Operational requirements:

- Provider accounts are scoped to org/workspace/project.
- Agents never receive raw provider secrets.
- Provider adapters execute only through typed skills.
- Every write is represented by a `ChangeSet`.
- Spendy writes require a `BudgetReservation`.
- Destructive writes require explicit approval.
- Production DNS/deploy changes require rollback evidence.
- Every provider action writes a `ResourceReceipt`.
- Failed provider calls become events, deadletters, or decisions.

## Staging Rollout Checklist

Before staging:

1. `make lint`
2. `make test`
3. `make build`
4. `make smoke`
5. `make k8s-validate`
6. `make infra-validate`
7. `make release-blockers`
8. Build and publish API/web/worker images.
9. Create a real `boss-secrets` secret with API keys and `DATABASE_URL`.
10. Apply Helm or Kustomize to staging.
11. Run `/health`, API auth, worker lease, seed/bootstrap, and smoke checks.
12. Record staging health evidence in BOSS.

Staging provider writes remain disabled until an explicit `DeploymentEnvironmentPolicy`, `ChangeSet`, `BudgetReservation`, approval, and rollback plan exist.

## Production Rollout Checklist

Before production:

1. Complete all staging checks.
2. Verify HA Postgres and backup/restore drill evidence.
3. Confirm API auth plus OIDC or SAML runtime configuration.
4. Confirm `make release-blockers` has no unaccepted blockers for the selected rollout.
5. Create a production deployment decision card with risk, budget, rollback, and evidence links.
6. Confirm provider write switches are still disabled unless the rollout explicitly needs them.
7. Apply the reviewed Helm values or Kustomize overlay.
8. Verify `/health`, authenticated API, web, worker polling, job lease/heartbeat, and event feed.
9. Record production cluster validation evidence in BOSS.

Do not enable production DNS, deploy, provider write, or public-cloud spillover flags as part of a generic rollout. Each one needs its own ChangeSet and approval path.

## Rollback

Rollback must be explicit before any non-local rollout:

- Revert image tags or Helm release revision.
- Disable provider write flags.
- Drain cloud workers.
- Re-enter read-only provider mode.
- Restore database from tested backup only if data corruption is confirmed.
- Record rollback evidence and reason in BOSS.

Rollback is not a substitute for receipt/idempotency. Repeated provider effects must still dedupe through receipts.

## Validation Commands

Local release baseline:

```sh
make lint
make test
make build
make smoke
make k8s-validate
make infra-validate
make release-blockers
```

`make release-blockers` is expected to report blocked external rollout paths on an unconfigured workstation. That is correct; it prevents local readiness from being confused with live hosted/provider readiness.
