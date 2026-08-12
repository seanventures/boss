# BOSS Account Connect

Hosted auth, entitlement, marketplace, team invite, and billing portal service
consumed by the BOSS control plane through `BOSS_ACCOUNT_CONNECT_URL`.

This is the **Cloud/hosted side** of Easy Connect: self-hosted Community Mission
Control never pastes long-lived Cloud secrets; it polls short device codes or
completes a one-time browser auth code against this service.

## Environment

| Variable | Side | Purpose |
|---|---|---|
| `AC_DATABASE_URL` | Account Connect | Postgres DSN (defaults to `boss_account_connect` DB). May share the main `boss` DB; tables are `ac_*` with `IF NOT EXISTS`. |
| `AC_SERVICE_TOKEN` | Account Connect | Bearer token required on service APIs. Leave empty only for open local demos. |
| `AC_PUBLIC_BASE_URL` | Account Connect | Public origin used in authorization / portal URLs (e.g. `http://localhost:8010`). |
| `AC_DEFAULT_ORG_ID` | Account Connect | Seeded hosted org id (default `org_boss_cloud`). |
| `AC_DEFAULT_ORG_NAME` | Account Connect | Display name for Easy Connect pages. |
| `BOSS_ACCOUNT_CONNECT_URL` | Control plane (API/worker) | Base URL of this service (e.g. `http://localhost:8010` or `http://account-connect:8010` in compose). |
| `BOSS_ACCOUNT_CONNECT_TOKEN` | Control plane | Must match `AC_SERVICE_TOKEN`. |
| `BOSS_EDITION` | Control plane | Set `cloud` when testing hosted features end-to-end. |

Never commit real tokens. Prefer ops secret store / env files that are gitignored.

## Run locally (uvicorn)

```bash
export AC_DATABASE_URL=postgresql+psycopg://boss:boss@127.0.0.1:5432/boss
export AC_SERVICE_TOKEN=dev-account-connect-token
export AC_PUBLIC_BASE_URL=http://localhost:8010
export AC_DEFAULT_ORG_ID=org_boss_cloud
PYTHONPATH=. python -m uvicorn apps.account_connect.app.main:app --host 0.0.0.0 --port 8010 --reload
```

Point the BOSS control plane at Account Connect:

```bash
export BOSS_ACCOUNT_CONNECT_URL=http://localhost:8010
export BOSS_ACCOUNT_CONNECT_TOKEN=dev-account-connect-token
export BOSS_EDITION=cloud
```

`infra/docker-compose.yml` also defines a lightweight `account-connect` service
(port `8010`) that uses the local Postgres `boss_account_connect` database
created by `infra/postgres-init/01-create-account-connect-db.sql`.

## Docker Compose (optional cloud profile)

Root `docker-compose.yaml` includes an `account-connect` service under profiles
`account-connect` and `cloud` so the main boss stack stays unchanged unless you
opt in:

```bash
export AC_SERVICE_TOKEN=dev-account-connect-token
export AC_DATABASE_URL=postgresql+psycopg://boss:boss@db:5432/boss
export AC_PUBLIC_BASE_URL=http://account-connect:8010
export BOSS_ACCOUNT_CONNECT_URL=http://account-connect:8010
export BOSS_ACCOUNT_CONNECT_TOKEN=dev-account-connect-token

docker compose --profile cloud up -d account-connect
# or:
docker compose --profile account-connect up -d account-connect
```

Also set on the API/worker (via `app.env` / compose env):

```bash
BOSS_ACCOUNT_CONNECT_URL=http://account-connect:8010
BOSS_ACCOUNT_CONNECT_TOKEN=<same as AC_SERVICE_TOKEN>
```

## Easy Connect (operator-facing)

| Path | Who | Purpose |
|---|---|---|
| `GET/POST /device` | Operator browser | Enter short device code from Mission Control |
| `GET/POST /v1/demo/authorize` | Operator browser | One-click browser approve; redirects to `return_url` with `code` + `state` |
| `POST /v1/device/authorize` | Control plane (service token) | Programmatic authorize |
| `GET /v1/device/authorizations/{user_code}` | Control plane (service token) | Poll authorization |

User codes are normalized to uppercase with single hyphens (e.g. `ABCD-EFGH`).
`ac_device_authorizations.user_code` is unique so re-authorization upserts.

Browser approve stores a one-time `auth_code` on the session; complete validates
that code when present.

## Contract

Implements the HTTP surface expected by `apps/api/app/services/commercial.py`
/ `engine/boss_engine/services/commercial.py`:

- `GET /v1/health` (no auth)
- `GET /v1/orgs/{org_id}/health` (no auth)
- `GET /v1/orgs/{org_id}/entitlements`
- `GET /v1/orgs/{org_id}/marketplace/packs`
- `POST /v1/orgs/{org_id}/auth-sessions` and `POST /v1/auth-sessions`
- `POST /v1/orgs/{org_id}/auth-sessions/{id}/complete` and `POST /v1/auth-sessions/{id}/complete`
- `POST /v1/orgs/{org_id}/team-invites`
- `POST /v1/orgs/{org_id}/team-invites/{id}/revoke`
- `POST /v1/orgs/{org_id}/billing-portal-sessions`

Bearer auth is enforced on service APIs when `AC_SERVICE_TOKEN` is set. Operator
Easy Connect pages (`/device`, `/v1/demo/authorize`) are public — the short
device code / session state is the secret.

Startup seeds:

- default org (`AC_DEFAULT_ORG_ID`)
- entitlement `cloud_control_plane`
- marketplace pack `core_v1`

## Tests

```bash
# From repo root; shares boss DB or a dedicated AC DB.
export DATABASE_URL=postgresql+psycopg://boss:boss@127.0.0.1:5432/boss
PYTHONPATH=. .venv/bin/python -m pytest apps/account_connect/tests -q
```

Tests skip cleanly when Postgres is unreachable or neither `DATABASE_URL` nor
`AC_DATABASE_URL` is set.
