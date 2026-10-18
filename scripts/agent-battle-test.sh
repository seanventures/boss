#!/usr/bin/env bash
# The agent battle test (tests/agent-battle/battle.yaml, BOSSAPP-578): BOSS's real agents on
# the model this install has configured, in a TEST database. See scripts/agent_battle.py.
#
#   BATTLE_DATABASE_URL   the test database (required; its name must contain "test").
#                         db/schema.sql is applied and BOSS's defaults seeded if it is empty.
#   BATTLE_MODEL_FROM     the install's database to READ the model settings from (read-only:
#                         the project's chat/decision engine and model knobs, never a secret).
#                         Default: the same server's "boss" database.
#   BATTLE_PROJECT        which project's settings to read (default boss-dogfood)
#   BATTLE_CODE           the BOSS tree to test (default: this checkout) — point it at another
#                         worktree to score a base commit with the same set and runner.
#
#   scripts/agent-battle-test.sh [--only id,id] [--per-scenario N] [--workers N] [--label x]
#                                [--judge-model opus] [--no-judge]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CODE="${BATTLE_CODE:-$HERE}"
: "${BATTLE_DATABASE_URL:?set BATTLE_DATABASE_URL to a test database}"
case "${BATTLE_DATABASE_URL##*/}" in *test*) ;; *) echo "refusing: not a test database" >&2; exit 2;; esac
FROM="${BATTLE_MODEL_FROM:-${BATTLE_DATABASE_URL%/*}/boss}"
PROJECT="${BATTLE_PROJECT:-boss-dogfood}"

# The model settings the install's agents run on — the knobs only, never a credential.
KNOBS="chat_llm_engine chat_model claude_cli_model codex_cli_model decision_backend decision_model agent_decision_confidence"
MODEL_SETTINGS="$(python3 - "$FROM" "$PROJECT" $KNOBS <<'PY'
import json, sys
from sqlalchemy import create_engine, text
url, project, knobs = sys.argv[1], sys.argv[2], sys.argv[3:]
url = url.replace("postgresql://", "postgresql+psycopg://", 1) if url.startswith("postgresql://") else url
try:
    eng = create_engine(url, pool_pre_ping=True)
    with eng.connect() as c:
        c.execute(text("SET TRANSACTION READ ONLY"))
        rows = c.execute(text("SELECT key, value FROM project_settings WHERE project_id = :p "
                              "AND key = ANY(:k)"), {"p": project, "k": knobs}).all()
    def value(v):
        try:
            return json.loads(v) if isinstance(v, str) else v
        except ValueError:
            return v
    print(json.dumps({k: value(v) for k, v in rows}))
except Exception as exc:  # noqa: BLE001
    print(f"could not read the install's model settings: {type(exc).__name__}", file=sys.stderr)
    print("{}")
PY
)"
echo "model settings (from $PROJECT): $MODEL_SETTINGS"

export DATABASE_URL="$BATTLE_DATABASE_URL"
# A CLI engine runs on this machine's own login (no credential is copied from the install).
case "$MODEL_SETTINGS" in
  *'"claude_code"'*) export BOSS_ANTHROPIC_CLAUDE_OAUTH_ENABLED=true BOSS_ANTHROPIC_CLAUDE_OAUTH_SESSION_REF=host-cli-login ;;
  *'"codex"'*) export BOSS_OPENAI_CODEX_OAUTH_ENABLED=true BOSS_OPENAI_CODEX_OAUTH_SESSION_REF=host-cli-login ;;
esac
export PYTHONPATH="$CODE:$CODE/engine"
export BOSS_AUTHZ_MODE="${BOSS_AUTHZ_MODE:-shadow}"
export BOSS_BATTLE_CODE="$CODE"
python3 - <<'PY'
from sqlalchemy import text
from boss_engine.config import get_settings
from boss_engine.db import get_engine
with get_engine(get_settings()).connect() as c:
    seeded = c.execute(text("SELECT to_regclass('public.projects') IS NOT NULL")).scalar()
    seeded = seeded and c.execute(text("SELECT count(*) FROM projects")).scalar()
if not seeded:
    from boss_engine import bootstrap
    bootstrap.seed()
PY
exec python3 "$HERE/scripts/agent_battle.py" --model-settings "$MODEL_SETTINGS" "$@"
