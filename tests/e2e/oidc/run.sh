#!/bin/bash
# F6 end-to-end proof (stream S6): an openid-client RP signs in through BOSS's workforce pool,
# then through a customer pool (hosted sign-up, TOTP, consent, account page).
#
# Needs a running BOSS: the API (BOSS_PUBLIC_URL = the console origin) and the console, which
# forwards /oidc/* to the API (apps/web/lib/oidc-proxy.ts), with this shell's DATABASE_URL,
# BOSS_MASTER_KEY and BOSS_IDENTITY_SERVER_KEY being the API's.
#   tests/e2e/oidc/run.sh <boss_origin e.g. http://localhost:13000> [rp_origin]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
boss="${1:?boss origin}"; rp="${2:-http://localhost:14000}"
world="$(mktemp -d)/world.json"
trap 'rm -rf "$(dirname "$world")"' EXIT
(cd "$here/../../.." && PYTHONPATH=.:engine python3 tests/e2e/oidc/setup_world.py "$world" "$rp")
(cd "$here" && [ -d node_modules ] || npm ci --no-audit --no-fund >/dev/null)
node "$here/rp.mjs" "$world" "$boss" "$rp"
(cd "$here/../../.." && PYTHONPATH=.:engine python3 tests/e2e/oidc/setup_customer.py "$world" "$rp")
node "$here/rp-customer.mjs" "$world" "$boss" "$rp"
