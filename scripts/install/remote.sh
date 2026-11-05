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
MODEL_BASE_URL=""
PROJECT_ID="boss-dogfood"
SECRET_STORE=""
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
  prune        free disk BOSS used on the host (old images, stopped previews, build cache)
  check        run BOSS's own health check on the host
  smoke        prove the install works: API, database, console, model wiring
  status       containers, ports and disk on the host
  model        connect an API KEY to the remote install — one command, no tunnel
  subscription sign a Codex / Claude Code / Grok SUBSCRIPTION in inside BOSS on the host,
               through the provider's own login (open the link it prints), then prove it
  env          print export lines for BOSS_API / BOSS_API_KEY (the host tools' key) / PROJECT
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
  --provider NAME      model: openrouter | openai | anthropic | gemini | xai | custom
                       subscription: codex | claude_code | grok
  --model NAME         model: which model, e.g. anthropic/claude-sonnet-4.5
  --base-url URL       model: required for a custom OpenAI-compatible provider
  --project ID         model/env: project id (default boss-dogfood)
  --secret-store WHICH install: database (default, 4 containers) or infisical (6 containers,
                       ~400MB more, a real secret store). Both stay on the server
  --                   everything after this is passed through to install.sh

Model keys are read from a hidden prompt or stdin. Do not pass them as arguments; the
shell saves those in history. A subscription is never pasted: it signs in inside BOSS.

examples:
  scripts/install/remote.sh --host root@192.0.2.202 --key ~/.config/boss/deploy_key install --yes
  scripts/install/remote.sh --host root@192.0.2.202 --key ~/.config/boss/deploy_key smoke
  scripts/install/remote.sh --host root@192.0.2.202 --key ~/.config/boss/deploy_key tunnel
  scripts/install/remote.sh --host root@192.0.2.202 --key ~/.config/boss/deploy_key \
      model --provider openrouter --model anthropic/claude-sonnet-4.5
  scripts/install/remote.sh --host root@192.0.2.202 --key ~/.config/boss/deploy_key \
      subscription --provider codex
  scripts/install/remote.sh --host root@192.0.2.202 --key ~/.config/boss/deploy_key uninstall --yes
USAGE
}

CMD=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    install|update|uninstall|check|smoke|status|tunnel|logs|model|env|subscription|prune) CMD="$1"; shift ;;
    --token|--token=*|--api-key|--api-key=*)
      die "Refusing a secret on the command line. It would be saved in shell history and process listings. Use the hidden prompt or pipe one line on stdin." ;;
    --secret-store) SECRET_STORE="${2:-}"; shift 2 ;;
    --accept-database-fallback)
      die "--accept-database-fallback is gone: a subscription signs in inside BOSS (subscription --provider codex|claude_code|grok); nothing is stored from this laptop." ;;
    --purge-data) PURGE_DATA=1; shift ;;
    --skip-backup) SKIP_BACKUP=1; shift ;;
    --confirm-purge) CONFIRM_PURGE="${2:-}"; shift 2 ;;
    --provider) PROVIDER="${2:-}"; shift 2 ;;
    --model) MODEL_NAME="${2:-}"; shift 2 ;;
    --base-url) MODEL_BASE_URL="${2:-}"; shift 2 ;;
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
# THE PREVIOUS TREE GOES, OR MOVES ASIDE. Docker creates the read-only mount point
# .boss/selfsrc as root, so a plain `rm -rf $DIR.prev` failed after every good update and the
# NEXT update stopped at "previous source remains" (boss-1, 2026-11-05: eight such leftovers).
# An empty root-owned mount point holds nothing: rm, else sudo -n rm, else rename with a stamp.
clear_prev() {
  rsh "rm -rf '$DIR.prev' 2>/dev/null; [ ! -e '$DIR.prev' ] && exit 0
       sudo -n rm -rf '$DIR.prev' 2>/dev/null; [ ! -e '$DIR.prev' ] && exit 0
       mv '$DIR.prev' '$DIR.prev.'\$(date +%s)"
}
rshq() { ssh "${SSH_OPTS[@]}" "$HOST" "$@" 2>/dev/null; }

# Shell text, run ON THE SERVER, that sets variable $1 to the host tools' key: BOSS_HOST_API_KEY
# from the install's .env (issued there by `boss identity host-key` when it is missing), else the
# shared BOSS_API_KEY while that still works. The key never crosses the laptop's command line.
remote_key_sh() {
  local get="grep '^BOSS_HOST_API_KEY=' '$DIR/.env' 2>/dev/null | head -1 | cut -d= -f2-"
  printf '%s' "$1=\$($get); [ -n \"\$$1\" ] || { bash '$DIR/scripts/install/boss.sh' --dir '$DIR' identity host-key >/dev/null 2>&1 || true; $1=\$($get); }; [ -n \"\$$1\" ] || $1=\$(grep '^BOSS_API_KEY=' '$DIR/.env' 2>/dev/null | head -1 | cut -d= -f2-)"
}

# ---------------------------------------------------------------------------- preflight
preflight() {
  rsh 'echo ok' >/dev/null 2>&1 || die "cannot SSH to $HOST (try: ssh ${KEY:+-i $KEY} $HOST)"
  # `~` MEANS THE SSH USER'S HOME ON THE HOST. Every command below single-quotes "$DIR" into
  # the remote shell, so `--dir '~/boss'` reached the host as a literal "~/boss": smoke looked
  # for "~/boss/.env", found nothing, and quietly skipped the authenticated check — and install
  # or uninstall would have acted on the wrong path. Resolved once, here, before any use.
  if [[ "$DIR" == "~" || "$DIR" == "~/"* ]]; then
    local remote_home
    remote_home="$(rshq 'printf %s "$HOME"')"
    [[ -n "$remote_home" ]] || die "could not read the home directory of $HOST to resolve $DIR"
    DIR="${remote_home}${DIR#\~}"
  fi
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
  # WHAT THIS MACHINE CAN RUN AT ONCE, measured the way admission measures it (admission.py):
  # (RAM - max(1 GB, 15%) for the control plane) / 1.2 GB per build, clamped to [1, cores/2].
  local cores slots reserve
  cores="$(rshq "nproc" || echo 2)"
  reserve=$(( mem * 15 / 100 )); [[ "$reserve" -lt 1024 ]] && reserve=1024
  slots=$(( (mem - reserve) / 1200 ))
  [[ "$slots" -gt $(( cores / 2 )) ]] && slots=$(( cores / 2 ))
  [[ "$slots" -lt 1 ]] && slots=1
  say "Builds at once: ${slots} (auto) — more are queued, not run into swap"
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
  local sha sha_short
  # THE FULL 40-HEX COMMIT, never a short sha and never a tag object: `^{commit}` dereferences
  # an annotated tag to the commit it points at, so what is written here is always something
  # `git rev-parse <sha>^{commit}` on the host resolves on its own, unambiguously.
  sha="$(git -C "$REPO_ROOT" rev-parse "$REF^{commit}")" || die "no such ref: $REF"
  sha_short="$(git -C "$REPO_ROOT" rev-parse --short "$sha")"
  if [[ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]]; then
    warn "working tree is dirty — shipping committed $REF ($sha_short), not your edits"
  fi
  say "shipping $REF ($sha_short) → $HOST:$DIR"
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
  ok "source on host at $sha_short (tree replaced, not merged)"
}

# The commands printed for the operator to paste next. They carry --dir: without it a pasted
# command goes to the default /opt/boss and reports "no BOSS_API_KEY" against a healthy install.
again() {
  local cmd="remote.sh --host $HOST${KEY:+ --key $KEY}"
  [[ "$DIR" != "/opt/boss" ]] && cmd+=" --dir '$DIR'"
  printf '%s' "$cmd"
}

# ---------------------------------------------------------------------------- commands
# NOT CLAIMED YET (BOSSAPP-813): from the server's own network (LAN, tailnet) the console's https
# address is the claim itself; from anywhere else, the setup link the host prints.
claim_next() {
  local state origin
  state="$(rshq "bash '$DIR/scripts/install/boss.sh' --dir '$DIR' identity claim-status 2>/dev/null | tail -n 1" || true)"
  [[ "$state" == unclaimed ]] || return 0
  origin="$(rshq "grep '^BOSS_CONSOLE_PUBLIC_ORIGIN=' '$DIR/.env' 2>/dev/null | tail -n 1 | cut -d= -f2-" || true)"
  if [[ -n "$origin" ]]; then
    say "Claim it:        open ${origin%/} to claim this BOSS (from your own network); from elsewhere run on the host: ${D}boss identity setup-link${N}"
  else
    say "Claim it:        on the host run ${D}boss identity setup-link${N}, then open the link it prints"
  fi
}

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
  clear_prev || warn "previous source remains at $DIR.prev; remove it after verifying this install"
  prune_remote
  ok "installer finished"

  # The tunnel makes the VM's loopback ports available on the operator laptop;
  # do not suggest opening unencrypted public HTTP endpoints.
  printf "\n${B}Where it is${N}\n\n"
  say "Tunnel    ${D}$(again) tunnel${N}"
  say "Console   http://127.0.0.1:${WEB_PORT} after connecting the tunnel"
  say "API       http://127.0.0.1:${API_PORT} after connecting the tunnel"
  printf "\n${B}What next${N}\n\n"
  claim_next
  say "Check it:        ${D}$(again) smoke${N}"
  say "An API key:      ${D}$(again) model --provider openrouter${N} (hidden prompt)"
  say "A subscription:  ${D}$(again) subscription --provider codex${N} (or claude_code, grok): signs in inside BOSS through the provider's own login"
  say "Shell variables: ${D}eval \"\$($(again) env)\"${N}"
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

# A HOST THIS SCRIPT INSTALLS RUNS ONLY BOSS, so its Docker build cache is BOSS's: every
# deploy adds ~1.3 GB and nothing removed it (boss-1: 80% disk, 46.5 GB of cache). Best effort
# after every install and update; never volumes, never another project's containers.
prune_remote() {
  rshq "[ -f '$DIR/scripts/install/prune.sh' ]" || return 0
  local out
  out="$(rsh "cd '$DIR' && bash scripts/install/prune.sh --dedicated" 2>&1)" \
    || { warn "disk prune skipped: ${out##*$'\n'}"; return 0; }
  say "disk: $(printf '%s\n' "$out" | grep -E '^disk (before|after)' | tr '\n' ' ')"
}

# UPDATES RESTARTED BUILDS (stability spec C6): every recreate killed the API mid-turn and the
# build redid clone and install, four times in an hour on boss-1. The API now drains on SIGTERM
# (builds resume after), and when the operator chose "Hold updates while builds run" on the
# system-health card this waits for running builds first — up to BOSS_DEPLOY_WAIT_MINUTES
# (default 30), then updates anyway. The key is read on the host, never shipped.
wait_for_builds() {
  local limit="${BOSS_DEPLOY_WAIT_MINUTES:-30}" waited=0 state
  while :; do
    state="$(rshq "$(remote_key_sh k)
      [ -n \"\$k\" ] && curl -s -m 10 -H \"Authorization: Bearer \$k\" 'http://127.0.0.1:$API_PORT/v1/system/deploy-readiness'" || true)"
    case "$state" in
      *'"safe_to_deploy":false'*|*'"safe_to_deploy": false'*) ;;
      *) return 0 ;;
    esac
    if [[ "$waited" -ge "$limit" ]]; then
      warn "builds still running after ${limit} min; updating anyway (they resume afterwards)"
      return 0
    fi
    [[ "$waited" -eq 0 ]] && say "builds are running and updates are held until they finish — waiting (up to ${limit} min)"
    sleep 60
    waited=$((waited + 1))
  done
}

cmd_prune() {
  head_ "Free BOSS's disk on $HOST"
  preflight
  prune_remote
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
  #
  # The console is profiled too (`web`), and only `secrets` used to be carried over: every
  # --all-services update rebuilt api and worker, reported success, and left boss-1's console on
  # a week-old image (2026-10-03) — so console fixes were "deployed" and never seen. Each profile
  # is carried when any of its services is running.
  local profiles=""
  rshq "$REMOTE_DOCKER ps --filter name=boss-community-infisical- -q | grep -q ." \
    && profiles="--profile secrets"
  rshq "$REMOTE_DOCKER ps --filter name=boss-community-web- -q | grep -q ." \
    && profiles="$profiles --profile web"
  rshq "$REMOTE_DOCKER ps --filter name=boss-community-mailpit- -q | grep -q ." \
    && profiles="$profiles --profile mail"
  local update_started
  update_started=$(rshq "date -u +%s")
  local compose_build="$REMOTE_DOCKER compose -f scripts/install/community-compose.yml --env-file $next_env $profiles"
  local compose="$REMOTE_DOCKER compose -f scripts/install/community-compose.yml --env-file .env $profiles"

  say "building — this takes a few minutes"
  ssh "${SSH_OPTS[@]}" ${TTY_OPT[@]+"${TTY_OPT[@]}"} "$HOST" \
    "cd '$DIR' && $compose_build build $services" \
    || { rsh "rm -f '$DIR/$next_env'"; die "the build failed on $HOST. Old containers still run; previous source is at $DIR.prev."; }

  rsh "cd '$DIR' && mv '$next_env' .env && chmod 600 .env" \
    || die "could not promote the worker credentials on $HOST; old containers still run and previous source remains at $DIR.prev"

  wait_for_builds

  # BUILD FIRST, THEN SWAP. A failed build above leaves the running install untouched, which is
  # the whole reason these are two steps rather than `up -d --build`.
  say "recreating: ${services:-all}"
  ssh "${SSH_OPTS[@]}" ${TTY_OPT[@]+"${TTY_OPT[@]}"} "$HOST" \
    "cd '$DIR' && $compose up -d --no-deps --force-recreate $services" \
    || die "could not recreate containers on $HOST; previous source remains at $DIR.prev"
  # PROVE IT, rather than trusting the build's exit code: every running BOSS container must have
  # been created by this update. A service the update skipped is a stale build still serving.
  if [[ "$ALL_SERVICES" -eq 1 ]]; then
    local stale
    stale=$(rshq "$REMOTE_DOCKER ps --filter name=boss-community- --format '{{.Names}}' | while read -r c; do \
      t=\$($REMOTE_DOCKER inspect -f '{{.Created}}' \"\$c\" | xargs -I{} date -u -d {} +%s); \
      [ \"\$t\" -lt '$update_started' ] && echo \"\$c\"; done | grep -v infisical || true")
    [[ -z "$stale" ]] || die "these services were not rebuilt and still run the old build: $stale"
  fi
  clear_prev || warn "previous source remains at $DIR.prev; remove it after verifying this update"

  prune_remote
  ok "updated $was → $(rshq "cat '$DIR/.boss-shipped-commit'")"
  say ""
  say "now run:  ${D}scripts/install/$(again) smoke${N}"
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
  #
  # The key is read on the host. Copying it into this SSH command would put a generated
  # credential in the laptop's process list and in the remote shell's command string.
  local code
  code="$(rshq "$(remote_key_sh k)
    if [ -z \"\$k\" ]; then printf '%s' NOKEY
    else curl -s -o /dev/null -w '%{http_code}' -m 25 -H \"Authorization: Bearer \$k\" 'http://127.0.0.1:$API_PORT/v1/projects'
    fi")"
  if [[ "$code" == "NOKEY" || -z "$code" ]]; then
    warn "no API key in $DIR/.env — cannot test an authenticated route"; fails=1
  elif [[ "$code" == "200" ]]; then
    ok "API reaches its database (authenticated /v1/projects 200)"
  else
    warn "authenticated /v1/projects returned $code"
    rshq "$(remote_key_sh k)
      curl -s -m 25 -H \"Authorization: Bearer \$k\" 'http://127.0.0.1:$API_PORT/v1/projects' | head -c 300" | sed 's/^/      /'
    printf "\n"
    fails=1
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
  [[ -n "$PROVIDER" ]] || die "--provider is required (openrouter | openai | anthropic | gemini | xai | custom)"
  if [[ -z "$MODEL_API_KEY" ]]; then
    if [[ -t 0 && -r /dev/tty ]]; then
      read -r -s -p 'Provider API key (hidden): ' MODEL_API_KEY </dev/tty || true
      printf '\n' >/dev/tty
    else
      IFS= read -r MODEL_API_KEY || true
    fi
  fi
  [[ -n "$MODEL_API_KEY" ]] || die "provider API key is required; enter it at the hidden prompt or pipe one line on stdin"
  [[ "$PROVIDER" != custom || -n "$MODEL_BASE_URL" ]] || die "--base-url is required for a custom provider"
  head_ "Connecting a model on $HOST"
  preflight

  local payload
  payload="$(PROJ="$PROJECT_ID" PROV="$PROVIDER" AKEY="$MODEL_API_KEY" MNAME="$MODEL_NAME" MBASE="$MODEL_BASE_URL" \
    python3 -c 'import json,os
p={"project_id":os.environ["PROJ"],"actor":"ceo","provider":os.environ["PROV"],
   "api_key":os.environ["AKEY"]}
m=os.environ.get("MNAME")
if m: p["model"]=m
base=os.environ.get("MBASE")
if base: p["base_url"]=base
print(json.dumps(p))')" || die "could not build the request"

  say "provider: $PROVIDER${MODEL_NAME:+   model: $MODEL_NAME}   project: $PROJECT_ID"
  local out
  # THE KEY GOES OVER STDIN, not in argv — an argument is visible in the host's process list.
  out="$(printf '%s' "$payload" | ssh "${SSH_OPTS[@]}" "$HOST" "
      set -e
      $(remote_key_sh K)
      [ -n \"\$K\" ] || { echo 'NOKEY'; exit 1; }
      curl -sS -X POST 'http://127.0.0.1:$API_PORT/v1/setup/model' \
        -H \"x-boss-api-key: \$K\" -H 'Content-Type: application/json' --data-binary @-
  " 2>&1)" || true

  if [[ "$out" == *NOKEY* ]]; then die "no API key in $DIR/.env on $HOST — is BOSS installed there?"; fi
  # Community records a key without a live paid model call. A provider name
  # in an error is not evidence of a successful connection.
  local result
  result="$(printf '%s' "$out" | python3 -c 'import json,sys
try:
    data=json.load(sys.stdin)
    print((data.get("verification_state") or "stored") if data.get("ok") is True else "")
except (ValueError, TypeError):
    pass' 2>/dev/null)"
  if [[ -n "$result" ]]; then
    if [[ "$result" == verified ]]; then
      ok "model key stored and provider verified"
    else
      ok "model key stored; live provider call has not been verified"
    fi
  else
    # BOSS's own refusal, "headline — fix", and nothing added to it (boss_refusal.py).
    warn "$(printf '%s' "$out" | python3 "$REPO_ROOT/scripts/install/boss_refusal.py" \
      "the API did not accept the model connection (no reason was given)")"
    return 1
  fi
}

# THE SUBSCRIPTION EQUIVALENT OF `model` (BOSSAPP-769).
#
# A subscription is never carried from this laptop: no token, no auth file. It signs in INSIDE
# the BOSS server through the provider's own login — the host's `boss connect PROVIDER`, which
# asks BOSS to start that login, prints the link and code for you to open here, waits, then
# connects and proves it with one governed turn. This runs exactly that, over `ssh -t`.
cmd_subscription() {
  local word
  case "$PROVIDER" in
    codex) word=codex ;;
    claude|claude_code) word=claude ;;
    grok) word=grok ;;
    *) die "--provider is required (codex | claude_code | grok)" ;;
  esac
  [[ -t 0 && -t 1 ]] || die "a subscription signs in inside BOSS through its own login, which needs you at a terminal (you open the link it prints). Run this from a terminal, or on the host: boss connect $word"
  head_ "Signing $PROVIDER in inside BOSS on $HOST"
  preflight
  ssh "${SSH_OPTS[@]}" -t "$HOST" "bash '$DIR/scripts/install/boss.sh' --dir '$DIR' connect $word"
}

# For when you do want the shell variables — with the right key, which is the server's.
cmd_env() {
  preflight
  local key
  key="$(rshq "$(remote_key_sh k); printf '%s' \"\$k\"")"
  [[ -n "$key" ]] || die "no API key in $DIR/.env on $HOST — is BOSS installed there?"
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
  prune)     cmd_prune ;;
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
