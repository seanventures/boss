# BOSS Kubernetes Base

This directory contains the first Enterprise self-hosted Kubernetes package for BOSS.

The base is intentionally conservative:

- BOSS API, web, and worker run as separate workloads.
- Schema/bootstrap runs through a Job before rollout.
- API and worker share an artifacts PVC; production clusters should back this with RWX storage or replace it with an external artifact store before scaling heavily.
- Secrets are referenced through `boss-secrets`, but the template is not included in `kustomization.yaml` so fake credentials are never applied accidentally.
- API authentication is enabled by default; workers and web need configured API keys.
- Provider and model secrets remain optional. Missing credentials make the relevant adapters fail closed through normal BOSS decisions/events.

Build images from the repository root:

```sh
docker build -f infra/containers/api.Dockerfile -t ghcr.io/seanheiney/boss-api:0.4.0 .
docker build -f infra/containers/web.Dockerfile -t ghcr.io/seanheiney/boss-web:0.4.0 .
docker build -f infra/containers/worker.Dockerfile -t ghcr.io/seanheiney/boss-worker:0.4.0 .
```

Before applying:

1. Create a real Postgres database and set `DATABASE_URL` in a Secret named `boss-secrets`.
2. Copy `base/secret-template.yaml`, replace every placeholder with real secret refs/values, and apply it yourself.
3. Confirm the API keys in `BOSS_API_KEYS`, `BOSS_API_KEY`, and `BOSS_WORKER_API_KEY` line up with `BOSS_API_KEY_PRINCIPALS`.
4. Run `make k8s-validate`.
5. Apply with your chosen overlay: `kubectl apply -k deploy/kubernetes/base`.

Do not apply production DNS, cloud provisioning credentials, model credentials, or provider write switches until the relevant BOSS policies, ChangeSets, approvals, BudgetReservations, and rollback plans are configured.
