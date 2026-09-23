#!/usr/bin/env bash
# Install, verify and remove BOSS on ANOTHER machine, driven entirely from this one.
#
# WHY THIS EXISTS. Installing on a server meant copying the repository up by hand, SSH-ing in,
# and running the installer there — three steps, each easy to get subtly wrong, and none of them
# recorded anywhere. The machine ended up holding a tree nobody could identify: the box at
# 2.29.2.202 had /root/boss with no .git in it, so "which version is running" had no answer.
#
# WHAT THIS DOES INSTEAD. One command from the laptop. The source is streamed over the SSH
# connection as a git archive of an exact commit, so:
#
#   * the host never needs git, a deploy key, or access to a private repository;
#   * what lands there is a named commit rather than whatever was in someone's working tree;
#   * nothing is left behind on the laptop and no credential crosses the wire.
#
# The installer on the far side is the SAME install.sh a local user runs. This does not
# reimplement it — a second installation path is a second thing to keep true.
#
# SAFETY. `uninstall` destroys the database and the secret-store key on the remote host, so it
# requires --yes explicitly; there is no interactive prompt over a non-interactive SSH session
# to catch a mistake.
set -euo pipefail

HOST=""
KEY=""
DIR="/opt/boss"
REF="HEAD"
API_PORT="8000"
WEB_PORT="3000"
PG_PORT="5432"
ASSUME_YES=0
PROVIDER=""
MODEL_API_KEY=""
MODEL_NAME=""
PROJECT_ID="boss-dogfood"
EXTRA_INSTALL_ARGS=()

G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; B=$'\e[1m'; D=$'\e[2m'; N=$'\e[0m'
say()  { printf "  %s\n" "$*"; }
ok()   { printf "  ${G}✓${N} %s\n" "$*"; }
warn() { printf "  ${Y}!${N} %s\n" "$*"; }
die()  { printf "\n  ${R}✗ %s${N}\n\n" "$*" >&2; exit 1; }
head_() { printf "\n${B}%s${N}\n\n" "$*"; }

usage() {
  cat <<'USAGE'
BOSS remote installer — run the installer on a server, from your laptop.

usage:
  scripts/install/remote.sh --host USER@HOST [--key PATH] COMMAND [options]

commands:
  install      stream this commit to the host and run the installer there
  uninstall    remove the install on the host (needs --yes; destroys its database)
  check        run BOSS's own health check on the host
  smoke        prove the install works: API, database, console, model wiring
  status       containers, ports and disk on the host
  model        connect a model to the remote install — one command, no tunnel
  env          print export lines for BOSS_API / BOSS_API_KEY / PROJECT
  tunnel       forward the host's API and console to this laptop over SSH
  logs         tail a service's logs (--service api|worker|web|db)

options:
  --host USER@HOST     required; the machine to install on
  --key PATH           SSH PRIVATE key — the file WITHOUT .pub, e.g. ~/.ssh/id_ed25519
                       (passed to ssh -i; the .pub half belongs in the server's
                       authorized_keys). Omit to use ssh's own resolution.
  --dir PATH           install directory on the host (default /opt/boss)
  --ref REF            commit/tag/branch to ship (default HEAD)
  --api-port N         host port for the API (default 8000)
  --web-port N         host port for the console (default 3000)
  --postgres-port N    host port for Postgres (default 5432)
  --yes                required by uninstall; also makes install non-interactive
  --service NAME       which service `logs` should tail
  --provider NAME      model: openrouter | openai | anthropic | custom
  --api-key KEY        model: the provider key to store
  --model NAME         model: which model, e.g. anthropic/claude-sonnet-4.5
  --project ID         model/env: project id (default boss-dogfood)
  --                   everything after this is passed through to install.sh

examples:
  scripts/install/remote.sh --host root@2.29.2.202 --key ~/.config/boss/deploy_key install --yes
  scripts/install/remote.sh --host root@2.29.2.202 --key ~/.config/boss/deploy_key smoke
  scripts/install/remote.sh --host root@2.29.2.202 --key ~/.config/boss/deploy_key tunnel
  scripts/install/remote.sh --host root@2.29.2.202 --key ~/.config/boss/deploy_key \
      model --provider openrouter --api-key sk-or-... --model anthropic/claude-sonnet-4.5
  scripts/install/remote.sh --host root@2.29.2.202 --key ~/.config/boss/deploy_key uninstall --yes
USAGE
}

CMD=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    install|uninstall|check|smoke|status|tunnel|logs|model|env) CMD="$1"; shift ;;
    --provider) PROVIDER="${2:-}"; shift 2 ;;
    --api-key) MODEL_API_KEY="${2:-}"; shift 2 ;;
    --model) MODEL_NAME="${2:-}"; shift 2 ;;
    --project) PROJECT_ID="${2:-}"; shift 2 ;;
    --host) HOST="${2:-}"; shift 2 ;;
    --key) KEY="${2:-}"; shift 2 ;;
    --dir) DIR="${2:-}"; shift 2 ;;
    --ref) REF="${2:-}"; shift 2 ;;
    --api-port) API_PORT="${2:-}"; shift 2 ;;
    --web-port) WEB_PORT="${2:-}"; shift 2 ;;
    --postgres-port) PG_PORT="${2:-}"; shift 2 ;;
    --service) SERVICE="${2:-}"; shift 2 ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --) shift; EXTRA_INSTALL_ARGS=("$@"); break ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1 (try --help)" ;;
  esac
done

[[ -n "$CMD" ]] || { usage; exit 1; }
[[ -n "$HOST" ]] || die "--host is required"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new)
[[ -n "$KEY" ]] && SSH_OPTS+=(-i "$KEY")

# -t ONLY WHEN THERE IS A TERMINAL TO ATTACH IT TO. Asking for a pseudo-terminal from a script
# or a CI job prints "Pseudo-terminal will not be allocated" into the middle of the output,
# which reads like a failure in a log somebody is scanning for one.
TTY_OPT=()
[[ -t 1 ]] && TTY_OPT=(-t)

rsh()  { ssh "${SSH_OPTS[@]}" "$HOST" "$@"; }
rshq() { ssh "${SSH_OPTS[@]}" "$HOST" "$@" 2>/dev/null; }

# ---------------------------------------------------------------------------- preflight
preflight() {
  rsh 'echo ok' >/dev/null 2>&1 || die "cannot SSH to $HOST (try: ssh ${KEY:+-i $KEY} $HOST)"
  local missing
  missing="$(rshq 'for c in docker tar; do command -v $c >/dev/null 2>&1 || echo $c; done')"
  [[ -z "$missing" ]] || die "missing on the host: $(echo "$missing" | tr '\n' ' ')"
  rshq 'docker info >/dev/null 2>&1' || die "docker is installed on the host but not usable by this user"
}

# The host's own numbers, because an install that cannot fit is better refused than
# half-finished. Building the console image is the memory-hungry step and the one that dies
# first on a small box.
report_capacity() {
  local mem swap disk
  mem="$(rshq "free -m | awk '/Mem:/{print \$2}'")"
  swap="$(rshq "free -m | awk '/Swap:/{print \$2}'")"
  disk="$(rshq "df -BG --output=avail / | tail -1 | tr -dc '0-9'")"
  say "host: ${mem}MB RAM, ${swap}MB swap, ${disk}GB free disk"
  [[ "${disk:-0}" -lt 15 ]] && warn "under 15GB free — the images alone are ~13GB"
  # NO SWAP IS THE RISK, not the RAM figure on its own. The console build is the peak, and on a
  # box with nothing to page out to the kernel kills it rather than slowing down — which reads
  # as a mysterious installer failure rather than as "this machine is too small".
  if [[ "${swap:-0}" -lt 1 && "${mem:-0}" -lt 8000 ]]; then
    warn "${mem}MB RAM and NO swap — the console build is the peak and may be OOM-killed"
    say "  ${D}add temporary swap:  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile${N}"
  fi
  return 0
}

# ---------------------------------------------------------------------------- ship source
# A COMMIT, NOT A WORKING TREE. `git archive` takes what is committed at $REF, so the host
# receives something that can be named later. A dirty tree is reported rather than shipped.
ship_source() {
  local sha
  sha="$(git -C "$REPO_ROOT" rev-parse --short "$REF")" || die "no such ref: $REF"
  if [[ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]]; then
    warn "working tree is dirty — shipping committed $REF ($sha), not your edits"
  fi
  say "shipping $REF ($sha) → $HOST:$DIR"
  # STAGED AND SWAPPED, NOT EXTRACTED OVER THE TOP.
  #
  # `tar -x` into the existing directory overwrites what the archive contains and leaves
  # everything it does not. Renaming doctor.sh to checkup.sh therefore shipped checkup.sh and
  # left doctor.sh sitting beside it, so the host held a tree that matched no commit — exactly
  # the "which version is running" problem this script exists to end. Deleted files have to
  # actually delete.
  #
  # .env is carried across by hand because it is NOT in the archive and must survive an update:
  # it holds the secret-store encryption key, and losing it strands every stored secret.
  local stage="$DIR.staging"
  rsh "rm -rf '$stage' && mkdir -p '$stage'" || die "could not prepare staging dir on $HOST"
  git -C "$REPO_ROOT" archive --format=tar "$REF" \
    | rsh "tar -x -C '$stage'" \
    || die "could not copy the source to $HOST"
  rsh "for f in .env boss-community.env.sh; do
         [ -f '$DIR'/\$f ] && cp -p '$DIR'/\$f '$stage'/\$f || true
       done" || true
  # The swap itself: old tree aside, new tree in, old tree gone. Ordered so a failure at any
  # step leaves a complete tree at \$DIR rather than a half-written one.
  rsh "rm -rf '$DIR.prev' \
       && { [ -d '$DIR' ] && mv '$DIR' '$DIR.prev' || true; } \
       && mv '$stage' '$DIR' \
       && rm -rf '$DIR.prev'" || die "could not swap the new tree into place on $HOST"
  rsh "printf '%s\n' '$sha' > '$DIR/.boss-shipped-commit'"
  ok "source on host at $sha (tree replaced, not merged)"
}

# ---------------------------------------------------------------------------- commands
cmd_install() {
  head_ "Install BOSS on $HOST"
  preflight
  report_capacity
  ship_source
  local args=(--dir "$DIR" --api-port "$API_PORT" --web-port "$WEB_PORT" --postgres-port "$PG_PORT")
  # 127.0.0.1 IS ALWAYS WRONG FOR A REMOTE INSTALL, so this does not wait to be asked.
  #
  # NEXT_PUBLIC_BOSS_API_URL is compiled into the console bundle at image BUILD time. The
  # installer defaults it to 127.0.0.1, which is right on a laptop and means "the operator's
  # own machine" on a server — the console then serves a perfect 200 while every link inside it
  # points somewhere with nothing behind it. Installing over SSH we already know the address,
  # so it is passed unless the caller said otherwise.
  local supplied_public=0
  for a in ${EXTRA_INSTALL_ARGS[@]+"${EXTRA_INSTALL_ARGS[@]}"}; do
    [[ "$a" == "--public-url" ]] && supplied_public=1
  done
  if [[ $supplied_public -eq 0 ]]; then
    local addr="${HOST##*@}"
    args+=(--public-url "http://$addr:$API_PORT")
    say "console will be built for http://$addr:$API_PORT"
  fi
  [[ "$ASSUME_YES" -eq 1 ]] && args+=(-y --skip-model-key)
  [[ ${#EXTRA_INSTALL_ARGS[@]} -gt 0 ]] && args+=("${EXTRA_INSTALL_ARGS[@]}")
  say "running the installer on the host — this builds images and takes a while"
  # -t so the installer's progress reaches this terminal as it happens rather than in one
  # silent block at the end.
  ssh "${SSH_OPTS[@]}" ${TTY_OPT[@]+"${TTY_OPT[@]}"} "$HOST" "cd '$DIR' && bash install.sh ${args[*]}" \
    || die "the installer failed on $HOST (see output above)"
  ok "installer finished"
}

cmd_uninstall() {
  [[ "$ASSUME_YES" -eq 1 ]] || die "uninstall destroys the host's database and secret key — pass --yes"
  head_ "Remove BOSS from $HOST"
  preflight
  rshq "[ -f '$DIR/scripts/install/uninstall.sh' ]" \
    || die "no BOSS install found at $DIR (nothing to remove)"
  ssh "${SSH_OPTS[@]}" ${TTY_OPT[@]+"${TTY_OPT[@]}"} "$HOST" "cd '$DIR' && bash scripts/install/uninstall.sh --yes --dir '$DIR'" \
    || die "the uninstaller reported an error"
  ok "uninstaller finished"
  verify_clean
}

# WHAT "CLEAN" HAS TO MEAN. Not "the script said it worked" — no containers, no volumes, no
# images, no .env. Each one has been left behind by a failed removal at some point.
verify_clean() {
  head_ "Verifying the host is clean"
  local c v i e rc=0
  c="$(rshq "docker ps -aq --filter name=boss-community | wc -l" || echo 0)"
  v="$(rshq "docker volume ls -q | grep -cE 'boss[-_]community' || true")"
  i="$(rshq "docker images -q 'boss-community-*' | wc -l" || echo 0)"
  e="$(rshq "[ -f '$DIR/.env' ] && echo yes || echo no")"
  [[ "${c:-0}" -eq 0 ]] && ok "no containers" || { warn "$c container(s) left"; rc=1; }
  [[ "${v:-0}" -eq 0 ]] && ok "no volumes"    || { warn "$v volume(s) left";   rc=1; }
  [[ "${i:-0}" -eq 0 ]] && ok "no images"     || { warn "$i image(s) left";    rc=1; }
  [[ "$e" == "no" ]]    && ok "no .env"       || { warn ".env still present";  rc=1; }
  [[ $rc -eq 0 ]] && printf "\n  ${G}${B}Host is clean.${N}\n\n" \
                  || printf "\n  ${Y}${B}Host is NOT clean — see above.${N}\n\n"
  return $rc
}

cmd_status() {
  head_ "BOSS on $HOST"
  preflight
  report_capacity
  say ""
  rsh "docker ps --filter name=boss-community --format '  {{.Names}}\t{{.Status}}\t{{.Ports}}'" || true
  say ""
  say "shipped commit: $(rshq "cat '$DIR/.boss-shipped-commit' 2>/dev/null" || echo unknown)"
}

cmd_check() {
  head_ "Health check on $HOST"
  preflight
  local script
  script="$(rshq "ls '$DIR/scripts/install/checkup.sh' '$DIR/scripts/install/doctor.sh' 2>/dev/null | head -1")"
  [[ -n "$script" ]] || die "no health-check script found under $DIR"
  ssh "${SSH_OPTS[@]}" ${TTY_OPT[@]+"${TTY_OPT[@]}"} "$HOST" "cd '$DIR' && bash '$script'"
}

# THE SMOKE TEST RUNS ON THE HOST, against 127.0.0.1. The install binds to the host's own
# loopback, so testing from the laptop would prove the firewall is open rather than that BOSS
# works — a different question, and one `tunnel` answers.
cmd_smoke() {
  head_ "Smoke test on $HOST"
  preflight
  local fails=0

  local up
  up="$(rshq "docker ps --filter name=boss-community --filter status=running -q | wc -l")"
  [[ "${up:-0}" -ge 3 ]] && ok "containers running ($up)" || { warn "only ${up:-0} running"; fails=1; }

  local health
  health="$(rshq "curl -s -o /dev/null -w '%{http_code}' -m 20 http://127.0.0.1:$API_PORT/health")"
  [[ "$health" == "200" ]] && ok "API /health 200" || { warn "API /health returned $health"; fails=1; }

  # THE API MUST REACH ITS DATABASE, and that is not what /health proves.
  #
  # This read the BODY of an unauthenticated /v1/projects and passed if it contained a quote
  # character. A 401 is JSON and contains quotes. So did the 500 that was actually happening:
  # the API could not authenticate to Postgres, and this check called it "API returns project
  # data" three cycles running. Status code, with the key, against a route that touches the DB.
  local key code
  key="$(rshq "grep '^BOSS_API_KEY=' '$DIR/.env' 2>/dev/null | head -1 | cut -d= -f2-")"
  if [[ -z "$key" ]]; then
    warn "no BOSS_API_KEY in $DIR/.env — cannot test an authenticated route"; fails=1
  else
    code="$(rshq "curl -s -o /dev/null -w '%{http_code}' -m 25 -H 'Authorization: Bearer $key' http://127.0.0.1:$API_PORT/v1/projects")"
    if [[ "$code" == "200" ]]; then ok "API reaches its database (authenticated /v1/projects 200)"
    else
      warn "authenticated /v1/projects returned $code"
      rshq "curl -s -m 25 -H 'Authorization: Bearer $key' http://127.0.0.1:$API_PORT/v1/projects | head -c 300" | sed 's/^/      /'
      printf "\n"
      fails=1
    fi
  fi

  local tables
  tables="$(rshq "docker exec \$(docker ps -q --filter name=boss-community-db) psql -U boss -d boss -tAc \"select count(*) from information_schema.tables where table_schema='public'\" 2>/dev/null")"
  [[ "${tables:-0}" -gt 50 ]] && ok "database schema applied ($tables tables)" \
                              || { warn "only ${tables:-0} tables — schema may not have applied"; fails=1; }

  local web
  web="$(rshq "curl -s -o /dev/null -w '%{http_code}' -m 30 http://127.0.0.1:$WEB_PORT/")"
  if [[ "$web" == "200" ]]; then ok "console 200"
  elif [[ "$web" == "000" ]]; then say "  ${D}console not installed (--no-web)${N}"
  else warn "console returned $web"; fails=1; fi

  # A CONSOLE BUILT FOR THE WRONG URL IS THE FAILURE THAT LOOKS LIKE SUCCESS: it serves 200 and
  # every link inside points at a port on the operator's laptop.
  local baked host
  baked="$(rshq "docker exec \$(docker ps -q --filter name=boss-community-web) printenv NEXT_PUBLIC_BOSS_API_URL 2>/dev/null")"
  if [[ -n "$baked" ]]; then
    # SAY WHERE TO GO, not just what was compiled in. Reported as "console: <api url>" this
    # sent an operator to the API port, where `/` is a 404 and the install looks broken.
    host="${HOST##*@}"
    say ""
    say "  ${B}Open the console:${N} http://${host}:${WEB_PORT}"
    say "  ${D}it calls the API at: $baked${N}"
  fi

  printf "\n"
  if [[ $fails -eq 0 ]]; then printf "  ${G}${B}Smoke test passed.${N}\n\n"; return 0
  else printf "  ${R}${B}Smoke test FAILED.${N}\n\n"; return 1; fi
}

# ONE COMMAND, NO TUNNEL, NO EXPORTS.
#
# Connecting a model to a server used to be: open a tunnel in one terminal, export BOSS_API in
# another, ssh in to read the server's API key out of its .env, then hand-write a curl. Four
# steps across two terminals, and the documented ssh call omitted -i, so it failed with
# "Permission denied (publickey)" for anyone whose key is not ssh's default.
#
# The server can already reach its own API and already holds its own key. So it does the work.
cmd_model() {
  [[ -n "$PROVIDER" ]] || die "--provider is required (openrouter | openai | anthropic | custom)"
  [[ -n "$MODEL_API_KEY" ]] || die "--api-key is required"
  head_ "Connecting a model on $HOST"
  preflight

  local payload
  payload="$(PROJ="$PROJECT_ID" PROV="$PROVIDER" AKEY="$MODEL_API_KEY" MNAME="$MODEL_NAME" \
    python3 -c 'import json,os
p={"project_id":os.environ["PROJ"],"actor":"ceo","provider":os.environ["PROV"],
   "api_key":os.environ["AKEY"]}
m=os.environ.get("MNAME")
if m: p["model"]=m
print(json.dumps(p))')" || die "could not build the request"

  say "provider: $PROVIDER${MODEL_NAME:+   model: $MODEL_NAME}   project: $PROJECT_ID"
  local out
  # THE KEY GOES OVER STDIN, not in argv — an argument is visible in the host's process list.
  out="$(printf '%s' "$payload" | ssh "${SSH_OPTS[@]}" "$HOST" "
      set -e
      K=\$(grep '^BOSS_API_KEY=' '$DIR/.env' 2>/dev/null | head -1 | cut -d= -f2-)
      [ -n \"\$K\" ] || { echo 'NOKEY'; exit 1; }
      curl -sS -X POST 'http://127.0.0.1:$API_PORT/v1/setup/model' \
        -H \"x-boss-api-key: \$K\" -H 'Content-Type: application/json' --data-binary @-
  " 2>&1)" || true

  if [[ "$out" == *NOKEY* ]]; then die "no BOSS_API_KEY in $DIR/.env on $HOST — is BOSS installed there?"; fi
  # The API verifies the key against the provider before storing it, so its reply is the answer.
  if printf '%s' "$out" | grep -q '"ok": *true\|"status": *"connected"\|"provider"'; then
    ok "stored and verified"
    printf '%s\n' "$out" | head -c 400 | sed 's/^/      /'
    printf "\n"
  else
    warn "the API refused it:"
    printf '%s\n' "$out" | head -c 600 | sed 's/^/      /'
    printf "\n"
    return 1
  fi
}

# For when you do want the shell variables — with the right key, which is the server's.
cmd_env() {
  preflight
  local key host
  key="$(rshq "grep '^BOSS_API_KEY=' '$DIR/.env' 2>/dev/null | head -1 | cut -d= -f2-")"
  [[ -n "$key" ]] || die "no BOSS_API_KEY in $DIR/.env on $HOST"
  host="${HOST##*@}"
  # Printed for `eval`, so it goes to stdout bare. It contains a secret; do not paste it about.
  echo "export BOSS_API=http://${host}:${API_PORT}"
  echo "export BOSS_API_KEY=${key}"
  echo "export PROJECT=${PROJECT_ID}"
}

cmd_tunnel() {
  head_ "Forwarding $HOST to this laptop"
  say "API      http://127.0.0.1:$API_PORT"
  say "Console  http://127.0.0.1:$WEB_PORT"
  say ""
  say "Leave this running. Ctrl-C closes the tunnel."
  say ""
  # -N: no remote command, this is a tunnel and nothing else.
  ssh "${SSH_OPTS[@]}" -N \
    -L "$API_PORT:127.0.0.1:$API_PORT" \
    -L "$WEB_PORT:127.0.0.1:$WEB_PORT" "$HOST"
}

cmd_logs() {
  preflight
  local svc="${SERVICE:-api}"
  ssh "${SSH_OPTS[@]}" ${TTY_OPT[@]+"${TTY_OPT[@]}"} "$HOST" \
    "docker logs -f --tail 100 \$(docker ps -q --filter name=boss-community-$svc)"
}

case "$CMD" in
  install)   cmd_install ;;
  uninstall) cmd_uninstall ;;
  check)     cmd_check ;;
  smoke)     cmd_smoke ;;
  status)    cmd_status ;;
  model)     cmd_model ;;
  env)       cmd_env ;;
  tunnel)    cmd_tunnel ;;
  logs)      cmd_logs ;;
esac
