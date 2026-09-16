# Run BOSS locally

Everything on one machine: the control plane, a console, and a real secret store.

You need Docker, and a clone of this repository. All commands run from its root.

## 1. Settings

```bash
printf 'BOSS_API_KEY=%s\n' "$(openssl rand -hex 16)" >> .env
sh scripts/install/gen_infisical_keys.sh .env
```

The second command writes the two keys the secret store refuses to start without. **Back up
`.env`** — `INFISICAL_ENCRYPTION_KEY` is the only way to read what it stores.

If 5432, 8080, 8000 or 3000 are already in use on your machine, choose your own:

```bash
printf 'BOSS_POSTGRES_PORT=55700\nINFISICAL_PORT=55701\nBOSS_API_PORT=55702\nBOSS_WEB_PORT=55703\n' >> .env
```

## 2. Start it

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env \
  --profile secrets --profile web up -d --build
```

First run builds the API, worker and console, which takes a few minutes. `--profile secrets`
adds the bundled Infisical; `--profile web` adds the console. Leave either out and you get
BOSS without it.

## 3. Create the database contents

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env \
  exec -T api python -m boss_engine bootstrap
```

Applies the schema and seeds the default org, workspace and project. Until you run it, the API
answers requests about a project that does not exist.

## 4. Provision the secret store

```bash
sh scripts/install/bootstrap_infisical.sh .env
```

Creates the admin account, an organization, a project and a machine identity, then writes
`BOSS_INFISICAL_URL`, `BOSS_INFISICAL_TOKEN` and `BOSS_INFISICAL_PROJECT_ID` into `.env`. It
prints the admin login; the password is in `.env` as `INFISICAL_ADMIN_PASSWORD`.

Then restart BOSS so it reads them:

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env \
  --profile secrets --profile web up -d api worker
```

## 5. Check it

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env \
  exec -T api python -c "from boss_engine.config import Settings; from boss_engine.services.secret_backends import default_secret_backend; print(default_secret_backend(Settings()))"
```

Prints `infisical`. If it prints `database`, step 4 did not take — check `.env` holds the three
`BOSS_INFISICAL_*` values.

| What | Where |
|---|---|
| Console | http://127.0.0.1:3000 |
| API | http://127.0.0.1:8000 |
| Secret store | http://127.0.0.1:8080 |

Substitute your own ports if you set them in step 1. Open the console at
`/?org=org_boss` — the organization picker's Enter button is currently unreliable after a
restart, so the direct URL is the dependable way in.

## 6. Connect a model

BOSS ships with no model. See [connecting a model](MODEL_AND_SUBSCRIPTION_SETUP.md) — on this
install use **Path B**, because BOSS is running on the machine you are signed in on.

For a Codex subscription specifically, `BOSS_API` and `BOSS_API_KEY` for that page are your
local ones:

```bash
export BOSS_API=http://127.0.0.1:8000
export BOSS_API_KEY=$(grep '^BOSS_API_KEY=' .env | cut -d= -f2)
```

Credentials land in Infisical, not in BOSS's database.

## Stopping and starting

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env --profile secrets --profile web stop
docker compose -f scripts/install/community-compose.yml --env-file .env --profile secrets --profile web start
```

To throw everything away, including every stored secret:

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env --profile secrets --profile web down -v
```

## When something is wrong

| What you see | What it means |
|---|---|
| `required variable INFISICAL_ENCRYPTION_KEY is missing` | Step 1's second command was not run, or `--env-file .env` was left off. |
| `port is already allocated` | Set your own ports — step 1. |
| `violates foreign key constraint … projects` | Step 3 was not run. |
| `infisical is not configured` | Step 4 did not write to the `.env` you are passing to compose. |
| `bootstrap returned no token` | The store was already provisioned. Infisical issues that token once; the existing one is in `.env`. |
