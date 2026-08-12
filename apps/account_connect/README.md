# BOSS Account Connect

Hosted auth, entitlement, marketplace, team invite, and billing portal service consumed by the BOSS control plane through `BOSS_ACCOUNT_CONNECT_URL`.

## Run locally

```bash
export AC_DATABASE_URL=postgresql+psycopg://boss:boss@localhost:5433/boss_account_connect
export AC_SERVICE_TOKEN=dev-account-connect-token
export AC_PUBLIC_BASE_URL=http://localhost:8010
python -m uvicorn apps.account_connect.app.main:app --host 0.0.0.0 --port 8010 --reload
```

Point BOSS at the service:

```bash
export BOSS_ACCOUNT_CONNECT_URL=http://localhost:8010
export BOSS_ACCOUNT_CONNECT_TOKEN=dev-account-connect-token
export BOSS_EDITION=cloud
```

## Easy Connect (operator-facing)

Self-hosted Community Mission Control links here without pasting long-lived secrets:

| Path | Who | Purpose |
|---|---|---|
| `GET/POST /device` | Operator browser | Enter short device code from Mission Control |
| `GET/POST /v1/demo/authorize` | Operator browser | One-click browser approve; redirects to `return_url` with `code` + `state` |
| `POST /v1/device/authorize` | Control plane (service token) | Programmatic authorize |
| `GET /v1/device/authorizations/{user_code}` | Control plane (service token) | Poll authorization |

## Contract

Implements the HTTP surface expected by `apps/api/app/services/commercial.py`:

- `GET /v1/health`
- `GET /v1/orgs/{org_id}/health`
- `GET /v1/orgs/{org_id}/entitlements`
- `GET /v1/orgs/{org_id}/marketplace/packs`
- `POST /v1/orgs/{org_id}/auth-sessions` and `POST /v1/auth-sessions`
- `POST /v1/orgs/{org_id}/auth-sessions/{id}/complete` and `POST /v1/auth-sessions/{id}/complete`
- `POST /v1/orgs/{org_id}/team-invites`
- `POST /v1/orgs/{org_id}/team-invites/{id}/revoke`
- `POST /v1/orgs/{org_id}/billing-portal-sessions`

Bearer auth is enforced on service APIs when `AC_SERVICE_TOKEN` is set. Operator Easy Connect pages (`/device`, `/v1/demo/authorize`) are public — the short device code / session state is the secret.
