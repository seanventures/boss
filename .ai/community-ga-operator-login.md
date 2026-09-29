# Community operator login for v1.0-rc1

A human still has to complete a live Claude, Codex, or Grok login on a machine
that holds those accounts. Automated checks use fake tokens such as
`test-claude-token` and do not call Claude, Codex, or Grok.

## Claude setup token

The operator supplies a token from `claude setup-token`. BOSS stores that value
only in the local secret store (`local_vault`). It is not written to git, logs,
Postgres, or an env file by this path. If the local store is unavailable, or
the token is missing, setup stops with an error and stores nothing.

```bash
export CLAUDE_CODE_OAUTH_TOKEN='…'   # this shell only; do not commit it
./install.sh --dir "$PWD" --yes --skip-model-key --connect-claude
```

In a terminal, omit the variable and paste the token at the hidden prompt.
`scripts/store_subscription_token.py claude_code --stdin` and the setup screen
use the same store. `--accept-database-fallback` does not move this token into
Postgres.

## CLI-owned logins

These record connection status. They do not copy OAuth bytes and they do not
start a model call. A refused or missing login is an error.

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env \
  exec -u boss api claude auth login
./install.sh --dir "$PWD" --yes --skip-model-key --connect-claude-local

codex login --device-auth
./install.sh --dir "$PWD" --yes --skip-model-key --connect-codex

docker compose -f scripts/install/community-compose.yml --env-file .env \
  exec -u boss api grok login --device-auth
./install.sh --dir "$PWD" --yes --skip-model-key --connect-grok-local
```

Codex auth-file import is unchanged. Grok keeps its CLI session in `GROK_HOME`;
BOSS does not read `auth.json`. The first governed job is what checks that a
recorded login can actually call the model.

## Backups

`scripts/install/backup.sh` blanks `CLAUDE_CODE_OAUTH_TOKEN` and
`CODEX_AUTH_JSON` in the archive copy of `.env`. If either value still appears
in the archive, the archive is discarded. The live `.env` is not rewritten.
