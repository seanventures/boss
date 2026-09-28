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
docker build -f infra/containers/api.Dockerfile -t ghcr.io/seanheiney/boss-api:0.4.1 .
docker build -f infra/containers/web.Dockerfile -t ghcr.io/seanheiney/boss-web:0.4.1 .
docker build -f infra/containers/worker.Dockerfile -t ghcr.io/seanheiney/boss-worker:0.4.1 .
```

Before applying:

1. Create a real Postgres database and set `DATABASE_URL` in a Secret named `boss-secrets`.
2. Copy `base/secret-template.yaml`, replace every placeholder with real secret refs/values, and apply it yourself.
3. Confirm the API keys in `BOSS_API_KEYS`, `BOSS_API_KEY`, and `BOSS_WORKER_API_KEY` line up with `BOSS_API_KEY_PRINCIPALS`. Add independent, random `BOSS_CONSOLE_PASSWORD` and `BOSS_CONSOLE_SESSION_SECRET` values of at least 32 characters each to `boss-secrets`.
4. If exposing Mission Control remotely, set `BOSS_CONSOLE_PUBLIC_ORIGIN` in `base/configmap.yaml` to the exact browser-facing HTTPS origin. Configure a trusted TLS ingress to overwrite `Host`, `X-Forwarded-Host`, and `X-Forwarded-Proto` with that origin's host, host, and `https`. Keep the web and API services private; do not expose their HTTP ports directly.
   The base NetworkPolicy allows BOSS pods only, so add a narrow overlay
   permitting the trusted ingress controller to reach the web pod.
5. Run `make k8s-validate` and `make infra-validate`.
6. Apply with your chosen overlay: `kubectl apply -k deploy/kubernetes/base`.

The empty default origin rejects remote requests. The web readiness probe expects
an unauthenticated `/v1/projects` request to return 401; 503 keeps the pod out
of service when the console credentials are missing or too short. Verify the
deployed proxy and sign-in as described in `docs/community/CONSOLE_AUTH.md`.

Do not apply production DNS, cloud provisioning credentials, model credentials, or provider write switches until the relevant BOSS policies, ChangeSets, approvals, BudgetReservations, and rollback plans are configured.

Use `docs/operations/INFRA_RUNBOOK.md` and `deploy/environments/boss-infra-profiles.yaml` to choose the correct local, staging, production, or private-enterprise operating profile before scaling worker pools or enabling provider writes.
