#!/usr/bin/env bash
# THE NEXT STEP AFTER AN INSTALL, in one place: the installer's final card, `boss open` and
# `boss doctor` all print it from here, so they can never disagree.
#
# A fresh BOSS has no Owner. Until someone claims it, the console only says "run boss identity
# setup-link", so the first step is ALWAYS the claim; Setup (repository, model, first pull
# request) comes after. The stranger test (rc10.27) found the card leading with "Finish setup"
# and the password, and only `boss doctor` mentioning the claim.
#
#   next_step.sh --claim STATUS --setup-url URL [--boss CMD] [--origin URL]
#
#   --claim      `unclaimed`, `claimed …` (boss identity claim-status), or empty when unknown
#                (an older image): unknown is treated as unclaimed, with "if nobody owns it yet".
#   --setup-url  where Setup opens (http://localhost:3000/?area=setup)
#   --boss       the boss command to name (default: boss)
#   --origin     the console's https origin, when it has one: from the server's own network,
#                opening it claims BOSS on the site (BOSSAPP-813)
set -euo pipefail

claim="" setup_url="" boss="boss" origin=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --claim) claim="${2-}"; shift 2 ;;
    --setup-url) setup_url="${2-}"; shift 2 ;;
    --boss) boss="${2:-boss}"; shift 2 ;;
    --origin) origin="${2-}"; shift 2 ;;
    *) printf 'next_step.sh: unknown option %s\n' "$1" >&2; exit 2 ;;
  esac
done

B=$'\e[1m'; N=$'\e[0m'
[[ -t 1 ]] || { B=""; N=""; }

case "$claim" in
  claimed*)
    printf '  %sFinish setup%s     %s\n' "$B" "$N" "$setup_url"
    printf '                   (repository, model, first pull request)\n'
    printf '  Sign in with your own account. Teammates without one yet: %s password, until it stops.\n' "$boss"
    ;;
  *)
    if [[ "$claim" == unclaimed ]]; then
      printf '  %sNext: claim this BOSS%s (nobody owns it yet)\n' "$B" "$N"
    else
      printf '  %sNext: claim this BOSS%s (if nobody owns it yet)\n' "$B" "$N"
    fi
    printf '    1. On this server run:  %s identity setup-link\n' "$boss"
    printf '    2. Open the one-hour link it prints, in the browser you will sign in with.\n'
    printf '    3. Create the Owner account.\n'
    [[ -z "$origin" ]] ||
      printf '    From the server'"'"'s own network, opening %s claims it too.\n' "${origin%/}"
    printf '  %sThen finish setup%s  %s\n' "$B" "$N" "$setup_url"
    printf '                     (repository, model, first pull request)\n'
    printf '  %sGuided first minute%s python3 scripts/install/guided_first_minute.py\n' "$B" "$N"
    ;;
esac
