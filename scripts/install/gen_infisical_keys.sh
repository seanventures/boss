#!/usr/bin/env sh
# Generate the two keys a bundled Infisical refuses to start without, and append them to .env.
#
# They have no defaults on purpose: a shipped default encryption key is the same key on every
# installation that forgot to change it. Losing ENCRYPTION_KEY means losing every secret stored
# under it, so back up the .env this writes to.
set -eu

ENV_FILE="${1:-.env}"

if grep -qs '^INFISICAL_ENCRYPTION_KEY=' "$ENV_FILE"; then
  echo "$ENV_FILE already has INFISICAL_ENCRYPTION_KEY — leaving it alone." >&2
  echo "Rotating it would strand every secret already encrypted with the old one." >&2
  exit 0
fi

umask 077
touch "$ENV_FILE"
{
  echo "INFISICAL_ENCRYPTION_KEY=$(openssl rand -hex 16)"
  echo "INFISICAL_AUTH_SECRET=$(openssl rand -base64 32)"
} >> "$ENV_FILE"

echo "wrote INFISICAL_ENCRYPTION_KEY and INFISICAL_AUTH_SECRET to $ENV_FILE"
echo "back that file up: the encryption key is the only way to read what Infisical stores."
