#!/usr/bin/env bash
# Install, verify and remove BOSS on ANOTHER machine, driven entirely from this one.
#
# WHY THIS EXISTS. Installing on a server meant copying the repository up by hand, SSH-ing in,
# and running the installer there — three steps, each easy to get subtly wrong, and none of them
# recorded anywhere. The machine ended up holding a tree nobody could identify: one box
# had /root/boss with no .git in it, so "which version is running" had no answer.
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
# SAFETY. Remote uninstall keeps data and credentials by default. Purge needs a
# separate explicit confirmation; the SSH session must not turn --yes into data loss.
set -euo pipefail

HOST=""
KEY=""
DIR="/opt/boss"
REF="HEAD"
API_PORT="8000"
WEB_PORT="3000"
PG_PORT="5432"
ASSUME_YES=0
PURGE_DATA=0
SKIP_BACKUP=0
CONFIRM_PURGE=""
WITH_CONSOLE=0
ALL_SERVICES=0
PROVIDER=""
MODEL_API_KEY=""
MODEL_NAME=""
PROJECT_ID="boss-dogfood"
SUB_TOKEN=""
SECRET_STORE=""
ACCEPT_DB_FALLBACK=0
EXTRA_INSTALL_ARGS=()
REMOTE_DOCKER="docker"

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
  update       ship a newer commit to an EXISTING install and rebuild it in place
  uninstall    stop/remove the host stack; keep data and credentials by default
  check        run BOSS's own health check on the host
  smoke        prove the install works: API, database, console, model wiring
  status       containers, ports and disk on the host
  model        connect an API KEY to the remote install — one command, no tunnel
  subscription connect a Claude/Codex SUBSCRIPTION token — reads stdin, or --token
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
  --yes                required for remote uninstall; also makes install non-interactive
  --purge-data         uninstall: remove this stack's volumes and .env after backup
  --skip-backup        uninstall purge: explicitly skip the pre-purge backup
  --confirm-purge DELETE-BOSS-DATA
                       uninstall purge: required even with --yes
  --service NAME       which service `logs` should tail
  --with-console       update: also rebuild the web console. OFF by default because the
                       console build is the memory peak, and a host with under 8GB and no
                       swap gets it OOM-killed — which would take a working install down to
                       fix something in the engine.
  --all-services       update: rebuild every service
  --provider NAME      model: openrouter | openai | anthropic | custom
  --api-key KEY        model: the provider key to store
  --model NAME         model: which model, e.g. anthropic/claude-sonnet-4.5
  --project ID         model/env: project id (default boss-dogfood)
  --secret-store WHICH install: database (default, 4 containers) or infisical (6 containers,
                       ~400MB more, a real secret store). Both stay on the server
  --token VALUE        subscription: the token. Omit it and the token is read from stdin,
                       which keeps it out of your shell history and argv
  --accept-database-fallback
                       subscription: store the credential in Postgres when the install has
                       no secret store. Say yes deliberately; it is a live credential
  --                   everything after this is passed through to install.sh

examples:
  scripts/install/remote.sh --host root@192.0.2.202 --key ~/.config/boss/deploy_key install --yes
  scripts/install/remote.sh --host root@192.0.2.202 --key ~/.config/boss/deploy_key smoke
  scripts/install/remote.sh --host root@192.0.2.202 --key ~/.config/boss/deploy_key tunnel
  scripts/install/remote.sh --host root@192.0.2.202 --key ~/.config/boss/deploy_key \
      model --provider openrouter --api-key sk-or-... --model anthropic/claude-sonnet-4.5
  claude setup-token | scripts/install/remote.sh --host root@192.0.2.202 \
      --key ~/.config/boss/deploy_key subscription --provider claude_code
  scripts/install/remote.sh --host root@192.0.2.202 --key ~/.config/boss/deploy_key uninstall --yes
USAGE
}

CMD=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    install|update|uninstall|check|smoke|status|tunnel|logs|model|env|subscription) CMD="$1"; shift ;;
    --token) SUB_TOKEN="${2:-}"; shift 2 ;;
    --secret-store) SECRET_STORE="${2:-}"; shift 2 ;;
    --accept-database-fallback) ACCEPT_DB_FALLBACK=1; shift ;;
    --purge-data) PURGE_DATA=1; shift ;;
    --skip-backup) SKIP_BACKUP=1; shift ;;
    --confirm-purge) CONFIRM_PURGE="${2:-}"; shift 2 ;;
    --provider) PROVIDER="${2:-}"; shift 2 ;;
    --api-key) MODEL_API_KEY="${2:-}"; shift 2 ;;
    --model) MODEL_NAME="${2:-}"; shift 2 ;;
    --project) PROJECT_ID="${2:-}"; shift 2 ;;
    --host) HOST="${2:-}"; shift 2 ;;
    --key) KEY="${2:-}"; shift 2 ;;
    --with-console) WITH_CONSOLE=1; shift ;;
    --all-services) ALL_SERVICES=1; shift ;;
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
  if rshq 'docker info >/dev/null 2>&1'; then
    REMOTE_DOCKER="docker"
  elif rshq 'command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1'; then
    REMOTE_DOCKER="sudo -n docker"
  else
    die "Docker is unavailable on the host; start it or allow this user to run sudo docker"
  fi
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
         if [ -f '$DIR'/\$f ]; then cp -p '$DIR'/\$f '$stage'/\$f || exit 1; fi
       done" || die "could not carry private configuration into staging; live source unchanged"
  # Retain the previous tree until install/recreation succeeds. If the stage
  # cannot be moved into place, put the previous tree back immediately.
  rsh "[ ! -e '$DIR.prev' ]" ||
    die "previous source remains at $DIR.prev; recover or move it before another source swap"
  rsh "if [ -d '$DIR' ]; then mv '$DIR' '$DIR.prev' || exit 1; fi
       if ! mv '$stage' '$DIR'; then
         [ -d '$DIR.prev' ] && mv '$DIR.prev' '$DIR'
         exit 1
       fi" || die "could not swap the new tree into place on $HOST; previous source restored if possible"
  rsh "printf '%s\n' '$sha' > '$DIR/.boss-shipped-commit'"
  ok "source on host at $sha (tree replaced, not merged)"
}

# ---------------------------------------------------------------------------- commands
cmd_install() {
  head_ "Install BOSS on $HOST"
  preflight
  report_capacity
  if rshq "[ -f '$DIR/.env' ]"; then
    backup_remote
  fi
  ship_source
  local args=(--dir "$DIR" --api-port "$API_PORT" --web-port "$WEB_PORT" --postgres-port "$PG_PORT")
  # Community ports remain on the VM's loopback. A laptop connects through
  # the tunnel command, which makes 127.0.0.1 the correct browser API address.
  [[ -n "$SECRET_STORE" ]] && args+=(--secret-store "$SECRET_STORE")
  [[ "$ASSUME_YES" -eq 1 ]] && args+=(-y --skip-model-key)
  [[ ${#EXTRA_INSTALL_ARGS[@]} -gt 0 ]] && args+=("${EXTRA_INSTALL_ARGS[@]}")
  say "running the installer on the host — this builds images and takes a while"
  # -t so the installer's progress reaches this terminal as it happens rather than in one
  # silent block at the end.
  ssh "${SSH_OPTS[@]}" ${TTY_OPT[@]+"${TTY_OPT[@]}"} "$HOST" "cd '$DIR' && bash install.sh ${args[*]}" \
    || die "the installer failed on $HOST; previous source remains at $DIR.prev and private backup was printed above"
  rsh "rm -rf '$DIR.prev'" || warn "previous source remains at $DIR.prev; remove it after verifying this install"
  ok "installer finished"

  # The tunnel makes the VM's loopback ports available on the operator laptop;
  # do not suggest opening unencrypted public HTTP endpoints.
  printf "\n${B}Where it is${N}\n\n"
  say "Tunnel    ${D}remote.sh --host $HOST${KEY:+ --key $KEY} tunnel${N}"
  say "Console   http://127.0.0.1:${WEB_PORT} after connecting the tunnel"
  say "API       http://127.0.0.1:${API_PORT} after connecting the tunnel"
  printf "\n${B}What next${N}\n\n"
  say "Check it:        ${D}remote.sh --host $HOST${KEY:+ --key $KEY} smoke${N}"
  say "An API key:      ${D}remote.sh --host $HOST${KEY:+ --key $KEY} model --provider openrouter --api-key KEY${N}"
  say "A subscription:  ${D}claude setup-token | remote.sh --host $HOST${KEY:+ --key $KEY} subscription --provider claude_code${N}"
  say "Shell variables: ${D}eval \"\$(remote.sh --host $HOST${KEY:+ --key $KEY} env)\"${N}"
  printf "\n"
}

# UPDATING IS NOT REINSTALLING, and until now the only way to ship a fix was `install`.
#
# That works and it is the wrong tool: `install` rebuilds every image including the web console,
# which is the memory peak. On the host this was written for — 3.8GB RAM, no swap, two cores —
# rebuilding the console to deploy a Python fix in the engine risks OOM-killing the build and
# taking a working install down with it.
#
# So this ships the tree and rebuilds only what needs it. Default is api and worker, because
# that is where the engine lives and where nearly every fix lands. The console is opt-in.
backup_remote() {
  rshq "[ -f '$DIR/scripts/install/backup.sh' ]" ||
    die "this remote checkout predates backup.sh; make a private database/artifact backup before updating"
  local backup
  backup="$(rsh "cd '$DIR' && bash scripts/install/backup.sh --dir '$DIR'")" ||
    die "remote backup failed; source and containers were not changed"
  say "private recovery backup on host: $backup"
}

cmd_update() {
  head_ "Update BOSS on $HOST"
  preflight
  rshq "[ -f '$DIR/.env' ]" || die "no install at $DIR on $HOST — use \`install\`, not \`update\`"

  local was
  was="$(rshq "cat '$DIR/.boss-shipped-commit' 2>/dev/null")"
  say "host is at ${was:-unknown}"

  # The source swap must never be the first mutation of a long-lived install.
  # This helper also snapshots the database and artifacts while writers pause.
  backup_remote

  ship_source

  # Archive updates do not run install.sh. Prepare a private copy of .env with
  # the dedicated worker key, then build against that copy. A failed build
  # leaves the live credentials unchanged; only a successful build promotes it.
  local next_env=".env.worker-next.$$"
  rsh "cd '$DIR' && cp -p .env '$next_env' && chmod 600 '$next_env' && python3 scripts/install/reconcile_api_credentials.py --ensure-worker '$next_env'" \
    || { rsh "rm -f '$DIR/$next_env'"; die "could not bind separate owner and worker keys on $HOST; previous source remains at $DIR.prev"; }

  local services="api worker"
  [[ "$WITH_CONSOLE" -eq 1 ]] && services="api worker web"
  [[ "$ALL_SERVICES"  -eq 1 ]] && services=""
  if [[ -n "$services" ]]; then
    say "rebuilding: $services  ${D}(console left alone; --with-console to include it)${N}"
  else
    say "rebuilding every service"
  fi

  # PROFILES MUST MATCH WHAT IS RUNNING or compose treats the bundled secret store as unwanted
  # and takes it down mid-update, stranding every secret behind it.
  local profiles=""
  rshq "$REMOTE_DOCKER ps --filter name=boss-community-infisical- -q | grep -q ." \
    && profiles="--profile secrets"
  local compose_build="$REMOTE_DOCKER compose -f scripts/install/community-compose.yml --env-file $next_env $profiles"
  local compose="$REMOTE_DOCKER compose -f scripts/install/community-compose.yml --env-file .env $profiles"

  say "building — this takes a few minutes"
  ssh "${SSH_OPTS[@]}" ${TTY_OPT[@]+"${TTY_OPT[@]}"} "$HOST" \
    "cd '$DIR' && $compose_build build $services" \
    || { rsh "rm -f '$DIR/$next_env'"; die "the build failed on $HOST. Old containers still run; previous source is at $DIR.prev."; }

  rsh "cd '$DIR' && mv '$next_env' .env && chmod 600 .env" \
    || die "could not promote the worker credentials on $HOST; old containers still run and previous source remains at $DIR.prev"

  # BUILD FIRST, THEN SWAP. A failed build above leaves the running install untouched, which is
  # the whole reason these are two steps rather than `up -d --build`.
  say "recreating: ${services:-all}"
  ssh "${SSH_OPTS[@]}" ${TTY_OPT[@]+"${TTY_OPT[@]}"} "$HOST" \
    "cd '$DIR' && $compose up -d --no-deps --force-recreate $services" \
    || die "could not recreate containers on $HOST; previous source remains at $DIR.prev"
  rsh "rm -rf '$DIR.prev'" || warn "previous source remains at $DIR.prev; remove it after verifying this update"

  ok "updated $was → $(rshq "cat '$DIR/.boss-shipped-commit'")"
  say ""
  say "now run:  ${D}scripts/install/remote.sh --host $HOST${KEY:+ --key $KEY} smoke${N}"
}

cmd_uninstall() {
  [[ "$ASSUME_YES" -eq 1 ]] || die "remote uninstall requires --yes (data is kept by default)"
  [[ "$PURGE_DATA" -eq 0 || "$CONFIRM_PURGE" == 'DELETE-BOSS-DATA' ]] ||
    die "data purge also requires --confirm-purge DELETE-BOSS-DATA"
  head_ "Remove BOSS from $HOST"
  preflight
  rshq "[ -f '$DIR/scripts/install/uninstall.sh' ]" \
    || die "no BOSS install found at $DIR (nothing to remove)"
  rshq "grep -q -- '--confirm-purge' '$DIR/scripts/install/uninstall.sh'" \
    || die "remote uninstaller predates safe data retention; update the remote source before uninstalling"
  local flags=""
  [[ "$PURGE_DATA" -eq 1 ]] && flags="--purge-data --confirm-purge DELETE-BOSS-DATA"
  [[ "$SKIP_BACKUP" -eq 1 ]] && flags="$flags --skip-backup"
  ssh "${SSH_OPTS[@]}" ${TTY_OPT[@]+"${TTY_OPT[@]}"} "$HOST" "cd '$DIR' && bash scripts/install/uninstall.sh --yes --dir '$DIR' $flags" \
    || die "the uninstaller reported an error"
  ok "uninstaller finished"
  verify_clean
}

# Verify the selected safety level. Default removal deliberately leaves volumes,
# images and .env in place so a rerun can recover the same database and keys.
verify_clean() {
  head_ "Verifying the host"
  local c e rc=0
  c="$(rshq "$REMOTE_DOCKER ps -aq --filter label=com.docker.compose.project=boss-community | wc -l" || echo 0)"
  e="$(rshq "[ -f '$DIR/.env' ] && echo yes || echo no")"
  [[ "${c:-0}" -eq 0 ]] && ok "no containers" || { warn "$c container(s) left"; rc=1; }
  if [[ "$PURGE_DATA" -eq 1 ]]; then
    [[ "$e" == "no" ]] && ok ".env removed" || { warn ".env still present"; rc=1; }
  else
    [[ "$e" == "yes" ]] && ok ".env retained" || { warn ".env missing"; rc=1; }
  fi
  return $rc
}

cmd_status() {
  head_ "BOSS on $HOST"
  preflight
  report_capacity
  say ""
  rsh "$REMOTE_DOCKER ps --filter name=boss-community --format '  {{.Names}}\t{{.Status}}\t{{.Ports}}'" || true
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
  up="$(rshq "$REMOTE_DOCKER ps --filter name=boss-community --filter status=running -q | wc -l")"
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
  tables="$(rshq "$REMOTE_DOCKER exec \$($REMOTE_DOCKER ps -q --filter name=boss-community-db) psql -U boss -d boss -tAc \"select count(*) from information_schema.tables where table_schema='public'\" 2>/dev/null")"
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
  baked="$(rshq "$REMOTE_DOCKER exec \$($REMOTE_DOCKER ps -q --filter name=boss-community-web) printenv NEXT_PUBLIC_BOSS_API_URL 2>/dev/null")"
  if [[ -n "$baked" ]]; then
    # SAY WHERE TO GO, not just what was compiled in. Reported as "console: <api url>" this
    # sent an operator to the API port, where `/` is a 404 and the install looks broken.
    say ""
    say "  ${B}Open the console through the SSH tunnel:${N} http://127.0.0.1:${WEB_PORT}"
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

# THE SUBSCRIPTION EQUIVALENT OF `model`.
#
# The documented route was: run store_subscription_token.py on your laptop, then curl
# /v1/setup/coding-agents/connect with $BOSS_API and $BOSS_API_KEY — two variables nobody had
# been told how to set for a remote install, and a script that stores the token in the WRONG
# install because it writes to whichever BOSS is local. Both calls belong on the server.
cmd_subscription() {
  [[ -n "$PROVIDER" ]] || die "--provider is required (claude_code | codex)"
  local token="$SUB_TOKEN"
  if [[ -z "$token" ]]; then
    [[ ! -t 0 ]] || die "no token: pass --token, or pipe one in (claude setup-token | … subscription --provider claude_code)"
    token="$(cat)"
  fi
  token="${token//$'\n'/}"
  [[ -n "$token" ]] || die "the token was empty"
  head_ "Connecting a $PROVIDER subscription on $HOST"
  preflight

  local payload
  payload="$(PROJ="$PROJECT_ID" PROV="$PROVIDER" TOK="$token" ACC="$ACCEPT_DB_FALLBACK" \
    python3 -c 'import json,os
print(json.dumps({"project_id":os.environ["PROJ"],"actor":"ceo",
  "provider":os.environ["PROV"],"credential":os.environ["TOK"],
  "accept_database_fallback":os.environ["ACC"]=="1"}))')" || die "could not build the request"

  say "provider: $PROVIDER   project: $PROJECT_ID   token: ${#token} chars (not shown)"

  # THE SCRIPT GOES OVER ARGV, THE CREDENTIAL OVER STDIN.
  #
  # Written as one quoted ssh string first, and three levels of shell quoting later `$K` was
  # expanding on the wrong machine. The script is base64'd instead — it holds no secret, so
  # argv is fine for it — while the payload, which does, stays on stdin.
  local remote_script encoded out
  IFS= read -r -d '' remote_script <<'EOS' || true
set -eu
DIR="$1"; PORT="$2"; PROJ="$3"; PROV="$4"
K=$(grep '^BOSS_API_KEY=' "$DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- || true)
[ -n "${K:-}" ] || { echo NOKEY; exit 1; }
B="http://127.0.0.1:$PORT"
STORE=$(curl -sS -X POST "$B/v1/setup/coding-agents/credential" \
  -H "x-boss-api-key: $K" -H 'Content-Type: application/json' --data-binary @- || true)
echo "STORE:$STORE"
case "$STORE" in *secret_store_fallback_not_accepted*) exit 0 ;; esac
CONNECT=$(curl -sS -X POST "$B/v1/setup/coding-agents/connect" \
  -H "x-boss-api-key: $K" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$PROJ\",\"actor\":\"ceo\",\"provider\":\"$PROV\"}" || true)
echo "CONNECT:$CONNECT"
EOS
  encoded=$(printf '%s' "$remote_script" | base64 | tr -d '\n')
  out="$(printf '%s' "$payload" | ssh "${SSH_OPTS[@]}" "$HOST" \
      "printf %s '$encoded' | base64 -d > /tmp/boss-sub.\$\$ && \
       bash /tmp/boss-sub.\$\$ '$DIR' '$API_PORT' '$PROJECT_ID' '$PROVIDER'; \
       rc=\$?; rm -f /tmp/boss-sub.\$\$; exit \$rc" 2>&1)" || true

  [[ "$out" == *NOKEY* ]] && die "no BOSS_API_KEY in $DIR/.env on $HOST — is BOSS installed there?"
  if printf '%s' "$out" | grep -q 'secret_store_fallback_not_accepted'; then
    warn "this install has no secret store, so BOSS stopped rather than putting a live"
    say  "  credential in its database. Re-run with --accept-database-fallback to allow it."
    return 1
  fi
  # `connect` proves the credential with a real turn before saying yes, so its reply is the
  # answer — not the store call, which only says the bytes were written somewhere.
  if printf '%s' "$out" | sed -n 's/^CONNECT://p' | grep -q '"ok": *true'; then
    ok "stored and proved with a real turn"
    printf '%s\n' "$out" | sed -n 's/^CONNECT://p' | head -c 400 | sed 's/^/      /'; printf "\n"
  else
    warn "it did not connect:"
    printf '%s\n' "$out" | head -c 700 | sed 's/^/      /'; printf "\n"
    return 1
  fi
}

# For when you do want the shell variables — with the right key, which is the server's.
cmd_env() {
  preflight
  local key
  key="$(rshq "grep '^BOSS_API_KEY=' '$DIR/.env' 2>/dev/null | head -1 | cut -d= -f2-")"
  [[ -n "$key" ]] || die "no BOSS_API_KEY in $DIR/.env on $HOST"
  # The install binds its API to VM loopback. The laptop reaches it through `tunnel`,
  # not over an unencrypted public port on the host.
  echo "export BOSS_API=http://127.0.0.1:${API_PORT}"
  echo "export BOSS_API_KEY=${key}"
  # BOSS_PROJECT, NOT PROJECT. The example scripts read BOSS_PROJECT, so a bare `PROJECT`
  # exported nothing they look at — the output was correct and useless, which is worse than
  # wrong: everything appears set and the default project is used anyway.
  echo "export BOSS_PROJECT=${PROJECT_ID}"
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
    "$REMOTE_DOCKER logs -f --tail 100 \$($REMOTE_DOCKER ps -q --filter name=boss-community-$svc)"
}

case "$CMD" in
  install)   cmd_install ;;
  update)    cmd_update ;;
  uninstall) cmd_uninstall ;;
  check)     cmd_check ;;
  smoke)     cmd_smoke ;;
  status)    cmd_status ;;
  model)     cmd_model ;;
  subscription) cmd_subscription ;;
  env)       cmd_env ;;
  tunnel)    cmd_tunnel ;;
  logs)      cmd_logs ;;
esac
