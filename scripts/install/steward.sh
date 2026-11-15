#!/usr/bin/env bash
# boss steward — install, run or remove the Infra Agent's host helper (BOSSAPP-1157, LLD §7.2).
#
#   steward.sh run [--sample-only]   one pass: post a reading, take at most one signed job
#   steward.sh install               launchd agent (macOS) / systemd user timer (Linux), every 30 s
#   steward.sh uninstall             remove it
#   steward.sh status                is it installed, when did it last run, last log line
#
# Pull only: it calls the local API with the host key (BOSS_HOST_API_KEY in .env, issued by
# `boss identity host-key`). No listener. It never prints the key. Needs python3 and docker on the
# PATH of the install user (the one who owns Docker Desktop / is in the docker group).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
INSTALL_DIR="${BOSS_INSTALL_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd -P)}"
HELPER="$SCRIPT_DIR/steward.py"
INTERVAL="${BOSS_STEWARD_INTERVAL:-30}"
LABEL="com.boss.steward"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
LOG_DIR="${BOSS_STEWARD_STATE:-$HOME/.boss}"

usage() { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; }

python_bin() { command -v python3 || { echo "steward: python3 is required" >&2; exit 2; }; }

cmd_run() {
  exec "$(python_bin)" -I "$HELPER" --install-dir "$INSTALL_DIR" "$@"
}

install_mac() {
  mkdir -p "$(dirname "$PLIST")" "$LOG_DIR"
  # PATH is given explicitly: launchd agents do not read the shell profile, and `docker`
  # lives under /usr/local/bin or /opt/homebrew/bin.
  sed -e "s|@PYTHON@|$(python_bin)|g" -e "s|@HELPER@|$HELPER|g" -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
      -e "s|@INTERVAL@|$INTERVAL|g" -e "s|@LOG_DIR@|$LOG_DIR|g" -e "s|@HOME@|$HOME|g" \
      "$SCRIPT_DIR/steward/com.boss.steward.plist.tmpl" > "$PLIST"
  launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  echo "boss steward installed (launchd $LABEL, every ${INTERVAL}s); log: $LOG_DIR/steward.log"
}

install_linux() {
  command -v systemctl >/dev/null 2>&1 || { echo "steward: systemd is required on Linux" >&2; exit 2; }
  mkdir -p "$UNIT_DIR" "$LOG_DIR"
  sed -e "s|@PYTHON@|$(python_bin)|g" -e "s|@HELPER@|$HELPER|g" -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
      "$SCRIPT_DIR/steward/boss-steward.service.tmpl" > "$UNIT_DIR/boss-steward.service"
  sed -e "s|@INTERVAL@|$INTERVAL|g" "$SCRIPT_DIR/steward/boss-steward.timer.tmpl" > "$UNIT_DIR/boss-steward.timer"
  systemctl --user daemon-reload
  systemctl --user enable --now boss-steward.timer
  # Survive logout on a server: lingering keeps the user manager alive.
  loginctl enable-linger "$USER" >/dev/null 2>&1 || true
  echo "boss steward installed (systemd --user boss-steward.timer, every ${INTERVAL}s); log: $LOG_DIR/steward.log"
}

cmd_install() {
  [[ -f "$INSTALL_DIR/.env" ]] || { echo "steward: $INSTALL_DIR has no .env (is BOSS installed there?)" >&2; exit 2; }
  grep -q '^BOSS_HOST_API_KEY=' "$INSTALL_DIR/.env" || echo "steward: no BOSS_HOST_API_KEY in .env yet — run 'boss identity host-key' once; the helper reports when it has a key" >&2
  case "$(uname -s)" in
    Darwin) install_mac ;;
    Linux) install_linux ;;
    *) echo "steward: unsupported OS $(uname -s)" >&2; exit 2 ;;
  esac
}

cmd_uninstall() {
  case "$(uname -s)" in
    Darwin) launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true; rm -f "$PLIST" ;;
    Linux) systemctl --user disable --now boss-steward.timer >/dev/null 2>&1 || true
           rm -f "$UNIT_DIR/boss-steward.service" "$UNIT_DIR/boss-steward.timer"; systemctl --user daemon-reload || true ;;
  esac
  echo "boss steward removed"
}

cmd_status() {
  case "$(uname -s)" in
    Darwin) if [[ -f "$PLIST" ]]; then echo "installed: launchd $LABEL"; launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -E 'state|last exit' | sed 's/^/  /' || true; else echo "not installed"; fi ;;
    Linux) systemctl --user is-active boss-steward.timer >/dev/null 2>&1 && echo "installed: systemd boss-steward.timer (active)" || echo "not installed" ;;
  esac
  if [[ -f "$LOG_DIR/steward.log" ]]; then echo "last run:"; tail -n 1 "$LOG_DIR/steward.log" | sed 's/^/  /'; fi
}

case "${1:-}" in
  run) shift; cmd_run "$@" ;;
  install) cmd_install ;;
  uninstall) cmd_uninstall ;;
  status) cmd_status ;;
  -h|--help|help|"") usage ;;
  *) echo "steward: unknown command $1" >&2; usage; exit 2 ;;
esac
