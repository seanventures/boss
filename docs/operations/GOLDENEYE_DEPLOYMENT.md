# Goldeneye Deployment

BOSS is prepared for goldeneye as a multi-service app: API, worker, and web
Mission Control. The production compose package uses goldeneye-injected
Postgres and platform services; it does not run its own Postgres, Redis,
Supabase, MinIO, or mail infrastructure.

## Files

- `Dockerfile`: multi-target linux/amd64 build for `api`, `worker`, `web`, and optional `account-connect`.
- `docker-compose.yaml`: goldeneye production compose file with Traefik labels.
- `deploy/goldeneye.compose.yaml`: image-backed compose file for the goldeneye `deploy --compose` CLI.
- `app.env.tmpl`: env template for goldeneye to fill and operators to review.

## Single Command

For a source-tree deployment on goldeneye, after goldeneye fills `app.env`,
deploy from the repo root:

```sh
docker compose --env-file app.env up -d --build
```

For the platform-native path, push the images first and deploy with the
image-backed compose file:

```sh
# Use one shared tag for api/worker/web so tags cannot drift (BOSS-342: worker
# lagged on `norebuild` while api/web were on `view2`, and worker lacked git).
TAG=$(git rev-parse --short HEAD)
REG=goldeneye.tail95d980.ts.net:5000

docker buildx build --platform linux/amd64 --target api \
  -t $REG/boss-api:$TAG --push .
docker buildx build --platform linux/amd64 --target worker \
  -t $REG/boss-worker:$TAG --push .
docker buildx build --platform linux/amd64 --target web \
  --build-arg NEXT_PUBLIC_BOSS_API_URL=https://boss-api.local.sean.network \
  --build-arg NEXT_PUBLIC_BOSS_PROJECT_ID=boss-dogfood \
  -t $REG/boss-web:$TAG --push .
docker buildx build --platform linux/amd64 --target account-connect \
  -t $REG/boss-account-connect:$TAG --push .

# Optional contract probe against published tags (Dockerfile-level checks always run):
#   BOSS_IMAGE=$REG/boss-api:$TAG BOSS_WORKER_IMAGE=$REG/boss-worker:$TAG \
#     python tests/test_runtime_image_contract.py

ssh root@goldeneye deploy boss --compose /path/to/goldeneye.compose.yaml --env-template /path/to/app.env.tmpl
```

**Worker image contract (BOSS-342):** the multi-target `Dockerfile` `worker` stage
must install `git`, `nodejs`, and `npm` (same packages as the api coding
toolchain minus Playwright/Claude). Without `git`, `code.task` jobs
deadletter with `No such file or directory: 'git'`. After rebuilding worker,
point compose at the new shared tag — do not leave worker on a stale tag while
api/web advance.

`NEXT_PUBLIC_BOSS_API_URL` is a Next.js public value and is compiled into the
browser bundle at build time. Setting it only in the runtime compose
environment is not sufficient for production web images. If it is omitted, the
web client falls back to same-origin relative API requests rather than baking a
developer localhost endpoint into the browser bundle.

Use the optional Account Connect service only for hosted/attached Cloud flows:

```sh
docker compose --env-file app.env --profile account-connect up -d --build
```

## Routing

BOSS is primarily an admin/control-plane product, so the default compose labels
publish the web console on the tailnet-only hostname:

- Web/admin: `https://boss.local.sean.network`
- API: `https://boss-api.local.sean.network`

No direct public TCP/UDP ports are required.

## Platform Services

Goldeneye injects the source-of-truth datastores and services:

- `DATABASE_URL` for Postgres.
- `REDIS_URL`, `SUPABASE_*`, `S3_*`, `OPENAI_*`, `SMTP_*`, `MAIL_FROM`, OIDC, and other platform endpoints.
- `OPENAI_BASE_URL` is supported for the Groq-backed OpenAI-compatible gateway. When set, BOSS uses chat completions rather than assuming the OpenAI Responses API.
- `SMTP_HOST`/`SMTP_PORT`/`MAIL_FROM` and `OIDC_ISSUER`/`OIDC_CLIENT_ID` are accepted directly, while legacy `BOSS_*` aliases remain supported.

## Operator-Provided Keys

Goldeneye provides the platform values listed in `app.env.tmpl`. Before deploy,
the operator must replace app-owned placeholders:

- Required for production auth: `BOSS_API_KEYS`, `BOSS_API_KEY_PRINCIPALS`, `BOSS_API_KEY`, and `BOSS_WORKER_API_KEY`.
- Required for real software-factory GitHub work: `BOSS_GITHUB_TOKEN` or the `BOSS_GITHUB_APP_*` values.
- Required only when Account Connect is enabled: `AC_SERVICE_TOKEN` and, if not sharing `DATABASE_URL`, `AC_DATABASE_URL`.
- Required only when provider writes are explicitly enabled: the relevant provider credentials such as `BOSS_DIGITALOCEAN_TOKEN`, `BOSS_CLOUDFLARE_API_TOKEN`, `BOSS_SUPABASE_ACCESS_TOKEN`, or `BOSS_SSH_HOST_PRIVATE_KEY`.
- Optional third-party model/provider keys remain blank unless that adapter is enabled: `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `XAI_API_KEY`, and similar adapter-specific keys.

Do not enable `BOSS_INFRA_REAL_PROVISION_ALLOWED`,
`BOSS_EMAIL_REAL_SEND_ALLOWED`, `BOSS_SLACK_REAL_SEND_ALLOWED`,
`BOSS_ENABLE_SANDBOX_MERGE`, or `BOSS_ENABLE_SANDBOX_DEPLOY` until the
corresponding policy, receipt, budget, approval, and rollback controls are
configured for that environment.

## Direct Ports

`DEPLOY_PORTS=` stays empty. BOSS needs no direct public TCP or UDP ports;
Traefik routes HTTP(S) by hostname to the internal service ports.

## Production Rules

- Do not enable provider writes unless the relevant BOSS ChangeSet, BudgetReservation, approval, and ResourceReceipt path is configured.
- Do not expose Mission Control on the public hostname by default.
- Do not publish database or platform-service ports.
- Do not commit `app.env`; commit only `app.env.tmpl`.
- Build amd64 images with `docker buildx build --platform linux/amd64 ...` or use the compose `platform: linux/amd64` setting.
