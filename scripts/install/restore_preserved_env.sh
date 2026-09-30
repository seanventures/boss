#!/bin/sh
# Put back the credentials a wholesale .env rewrite would destroy.
#
# install.sh writes .env with a heredoc. That is right for a first install and, left alone,
# fatal for a second: the file also holds INFISICAL_ENCRYPTION_KEY, which is the only key that
# can read the secret-store database in the Postgres volume, and BOSS_INFISICAL_TOKEN, which
# Infisical issues exactly once. The rewrite deletes both, then the installer sees the
# encryption key missing and generates a new one. The next start of Infisical cannot decrypt
# a single stored credential, and bootstrap cannot fetch the token again.
#
# Usage: restore_preserved_env.sh OLD_FILE NEW_FILE
# Mutates NEW_FILE. OLD_FILE is left alone.
#
# BOSS_PUBLIC_URL_EXPLICIT=1 keeps the public URL the new file already has. Otherwise the
# previous URL wins: the installer writes http://127.0.0.1 when the flag was not passed, and
# that default must not rebuild a server's console against the operator's laptop.
# BOSS_CONSOLE_ORIGIN_EXPLICIT=1 and BOSS_API_BIND_EXPLICIT=1 are the same idea for the
# console hostname and the API's published address. Unset, the previous value wins.
set -eu

OLD="${1:?old env file}"
NEW="${2:?new env file}"
[ -f "$OLD" ] && [ -f "$NEW" ]

# A blank in the new file must not erase a credential the previous file held. Remote updates
# run `install.sh -y --skip-model-key`, which writes OPENAI_API_KEY= with nothing after it.
FILL_IF_BLANK="
OPENAI_API_KEY
OPENAI_BASE_URL
ANTHROPIC_API_KEY
GOOGLE_API_KEY
XAI_API_KEY
OPENROUTER_API_KEY
BOSS_GITHUB_TOKEN
BOSS_ACCOUNT_CONNECT_URL
CODEX_AUTH_JSON
CLAUDE_CODE_OAUTH_TOKEN
"

# The previous value wins even when the new file already has a different one. The template
# writes a single API key and gpt-4o-mini; a re-run must not drop a second key or a model the
# operator changed. The encryption key is here for a sharper reason: if anything generated a
# fresh INFISICAL_ENCRYPTION_KEY before this script ran, that new key would make the existing
# secret-store database unreadable. The key from the previous file is the one the volume was
# encrypted with, so it wins, and a duplicate line is collapsed to that one value.
KEEP_PREVIOUS="
OPENAI_MODEL
BOSS_API_KEYS
BOSS_API_KEY_PRINCIPALS
BOSS_MCP_API_KEY
BOSS_MCP_PROJECT_ID
BOSS_WORKER_API_KEY
BOSS_CONSOLE_PASSWORD
BOSS_CONSOLE_SESSION_SECRET
BOSS_GITHUB_OWNER
BOSS_GITHUB_REPO
INFISICAL_ENCRYPTION_KEY
INFISICAL_AUTH_SECRET
INFISICAL_ADMIN_EMAIL
INFISICAL_ADMIN_PASSWORD
INFISICAL_PORT
INFISICAL_SITE_URL
BOSS_INFISICAL_URL
BOSS_INFISICAL_TOKEN
BOSS_INFISICAL_PROJECT_ID
BOSS_INFISICAL_CLIENT_ID
BOSS_INFISICAL_CLIENT_SECRET
BOSS_INFISICAL_ENVIRONMENT
"

old_val() {
  # First matching line, value may contain '='. Absent is empty, not an error.
  grep -E "^${1}=" "$OLD" 2>/dev/null | head -1 | cut -d= -f2- || true
}

new_val() {
  grep -E "^${1}=" "$NEW" 2>/dev/null | head -1 | cut -d= -f2- || true
}

# Replace every existing line for this key, or append one. Values are written with %s so a
# token that contains backslashes, quotes, or '=' is stored as itself.
set_val() {
  key="$1"
  val="$2"
  tmp="${NEW}.setting.$$"
  found=0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      "$key"=*)
        printf '%s=%s\n' "$key" "$val"
        found=1
        ;;
      *)
        printf '%s\n' "$line"
        ;;
    esac
  done < "$NEW" > "$tmp"
  if [ "$found" -eq 0 ]; then
    printf '%s=%s\n' "$key" "$val" >> "$tmp"
  fi
  mv "$tmp" "$NEW"
  chmod 600 "$NEW" 2>/dev/null || true
}

for key in $FILL_IF_BLANK; do
  old="$(old_val "$key")"
  [ -n "$old" ] || continue
  new="$(new_val "$key")"
  if [ -z "$new" ]; then
    set_val "$key" "$old"
  fi
done

for key in $KEEP_PREVIOUS; do
  old="$(old_val "$key")"
  [ -n "$old" ] || continue
  set_val "$key" "$old"
done

if [ "${BOSS_PUBLIC_URL_EXPLICIT:-0}" != "1" ]; then
  old="$(old_val NEXT_PUBLIC_BOSS_API_URL)"
  if [ -n "$old" ]; then
    set_val NEXT_PUBLIC_BOSS_API_URL "$old"
  fi
fi

if [ "${BOSS_CONSOLE_ORIGIN_EXPLICIT:-0}" != "1" ]; then
  old="$(old_val BOSS_CONSOLE_PUBLIC_ORIGIN)"
  if [ -n "$old" ]; then
    set_val BOSS_CONSOLE_PUBLIC_ORIGIN "$old"
  fi
fi

if [ "${BOSS_API_BIND_EXPLICIT:-0}" != "1" ]; then
  old="$(old_val BOSS_API_BIND)"
  if [ -n "$old" ]; then
    set_val BOSS_API_BIND "$old"
  fi
fi

for key in BOSS_API_PORT BOSS_WEB_PORT BOSS_POSTGRES_PORT; do
  case "$key" in
    BOSS_API_PORT) explicit="${BOSS_API_PORT_EXPLICIT:-0}" ;;
    BOSS_WEB_PORT) explicit="${BOSS_WEB_PORT_EXPLICIT:-0}" ;;
    BOSS_POSTGRES_PORT) explicit="${BOSS_POSTGRES_PORT_EXPLICIT:-0}" ;;
  esac
  if [ "$explicit" != "1" ]; then
    old="$(old_val "$key")"
    [ -z "$old" ] || set_val "$key" "$old"
  fi
done

# Keep operator-added settings that the installer template does not know about:
# model routing, worker tuning, provider options, etc. Only literal KEY=VALUE
# lines are copied. Never source/eval .env or expand a value; a credential may
# legitimately contain shell metacharacters and must stay data.
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    *=*) key="${line%%=*}" ;;
    *) continue ;;
  esac
  case "$key" in
    ""|[0-9]*|*[!A-Za-z0-9_]*) continue ;;
  esac
  if ! grep -q "^${key}=" "$NEW"; then
    printf '%s\n' "$line" >> "$NEW"
  fi
done < "$OLD"
