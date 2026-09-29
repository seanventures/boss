#!/usr/bin/env bash
# BOSS node agent. One program for every machine Community BOSS can use.
#
#   bash scripts/install/node_agent.sh --local
#   BOSS_CONTROL_PLANE='https://boss.example' BOSS_ENROLL_TOKEN='<one-time>' \
#     bash scripts/install/node_agent.sh
#
# --local is the computer where the community installer ran. It does not use an
# enroll token. Without --local, the same script calls home with a one-time token.
# The control plane does not SSH out to this machine.
#
# Declared runtime packages (the distro package manager runs only as root):
#   curl
#   ca-certificates
#   python3
set -euo pipefail

NODE_AGENT_PACKAGES="curl ca-certificates python3"

node_agent_have() {
  case " ${BOSS_NODE_AGENT_FORCE_MISSING:-} " in
    *" $1 "*) return 1 ;;
  esac
  case "$1" in
    curl) command -v curl >/dev/null 2>&1 ;;
    python3) command -v python3 >/dev/null 2>&1 ;;
    ca-certificates)
      [[ -f /etc/ssl/cert.pem || -f /etc/ssl/certs/ca-certificates.crt || -d /etc/ssl/certs ]]
      ;;
    *) return 1 ;;
  esac
}

node_agent_missing() {
  local name missing
  missing=""
  for name in $NODE_AGENT_PACKAGES; do
    if ! node_agent_have "$name"; then
      missing="${missing} ${name}"
    fi
  done
  printf '%s' "${missing# }"
}

node_agent_package_manager() {
  if [[ "$(uname -s)" == "Darwin" ]]; then
    printf '%s\n' none
    return
  fi
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-}${ID_LIKE:-}" in
      *debian*|*ubuntu*) printf '%s\n' apt; return ;;
      *fedora*|*rhel*|*centos*) printf '%s\n' dnf; return ;;
      *alpine*) printf '%s\n' apk; return ;;
    esac
  fi
  if command -v apt-get >/dev/null 2>&1; then printf '%s\n' apt; return; fi
  if command -v dnf >/dev/null 2>&1; then printf '%s\n' dnf; return; fi
  if command -v yum >/dev/null 2>&1; then printf '%s\n' yum; return; fi
  if command -v apk >/dev/null 2>&1; then printf '%s\n' apk; return; fi
  printf '%s\n' none
}

node_agent_install_command() {
  case "$1" in
    apt)
      printf '%s\n' "DEBIAN_FRONTEND=noninteractive apt-get install -y ${NODE_AGENT_PACKAGES}"
      ;;
    dnf) printf '%s\n' "dnf install -y ${NODE_AGENT_PACKAGES}" ;;
    yum) printf '%s\n' "yum install -y ${NODE_AGENT_PACKAGES}" ;;
    apk) printf '%s\n' "apk add --no-cache ${NODE_AGENT_PACKAGES}" ;;
    *) printf '%s\n' "install ${NODE_AGENT_PACKAGES}" ;;
  esac
}

node_agent_install_as_root() {
  local manager
  manager="$1"
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "node agent: not root; refusing to run the distro package manager" >&2
    echo "node agent: declared runtime packages: ${NODE_AGENT_PACKAGES}" >&2
    echo "node agent: install command: $(node_agent_install_command "$manager")" >&2
    return 1
  fi
  case "$manager" in
    apt)
      DEBIAN_FRONTEND=noninteractive apt-get update
      # shellcheck disable=SC2086
      DEBIAN_FRONTEND=noninteractive apt-get install -y $NODE_AGENT_PACKAGES
      ;;
    dnf)
      # shellcheck disable=SC2086
      dnf install -y $NODE_AGENT_PACKAGES
      ;;
    yum)
      # shellcheck disable=SC2086
      yum install -y $NODE_AGENT_PACKAGES
      ;;
    apk)
      # shellcheck disable=SC2086
      apk add --no-cache $NODE_AGENT_PACKAGES
      ;;
    *)
      echo "node agent: no supported distro package manager; install ${NODE_AGENT_PACKAGES} and re-run" >&2
      return 1
      ;;
  esac
}

node_agent_require_packages() {
  local missing manager
  missing="$(node_agent_missing)"
  manager="$(node_agent_package_manager)"
  if [[ -z "$missing" ]]; then
    printf '%s\n' "$manager"
    return
  fi
  if [[ "$(id -u)" -ne 0 || "$manager" == none ]]; then
    echo "node agent: missing runtime packages: ${missing}" >&2
    echo "node agent: declared runtime packages: ${NODE_AGENT_PACKAGES}" >&2
    if [[ "$(id -u)" -ne 0 ]]; then
      echo "node agent: not root; refusing to run the distro package manager" >&2
    fi
    if [[ "$manager" == none ]]; then
      echo "node agent: no supported distro package manager; install ${NODE_AGENT_PACKAGES} and re-run" >&2
    else
      echo "node agent: install command: $(node_agent_install_command "$manager")" >&2
    fi
    return 1
  fi
  node_agent_install_as_root "$manager"
  missing="$(node_agent_missing)"
  if [[ -n "$missing" ]]; then
    echo "node agent: missing runtime packages: ${missing}" >&2
    return 1
  fi
  printf '%s\n' "$manager"
}

node_agent_emit_report() {
  python3 - "$@" <<'PY'
import json
import os
from pathlib import Path

mode = os.environ["BOSS_AGENT_MODE"]
packages = os.environ["BOSS_DEP_PACKAGES"].split()
missing = [item for item in os.environ.get("BOSS_DEP_MISSING", "").split() if item]
report = {
    "packages": packages,
    "missing": missing,
    "installed_by_agent": os.environ.get("BOSS_DEP_INSTALLED_BY_AGENT") == "1",
    "package_manager": os.environ.get("BOSS_DEP_MANAGER") or "none",
}
payload = {
    "mode": mode,
    "project_id": os.environ.get("BOSS_PROJECT_ID", ""),
    "hostname": os.environ["BOSS_AGENT_HOSTNAME"],
    "arch": os.environ["BOSS_AGENT_ARCH"],
    "os": os.environ["BOSS_AGENT_OS"],
    "endpoint": os.environ.get("BOSS_AGENT_ENDPOINT", ""),
    "dependency_report": report,
    "labels": {
        "hostname": os.environ["BOSS_AGENT_HOSTNAME"],
        "arch": os.environ["BOSS_AGENT_ARCH"],
        "os": os.environ["BOSS_AGENT_OS"],
        "source": "node-agent",
    },
}
token = os.environ.get("BOSS_ENROLL_TOKEN", "")
if mode == "local":
    payload.pop("token", None)
else:
    if not token:
        raise SystemExit("enroll token rejected")
    payload["token"] = token
text = json.dumps(payload, sort_keys=True)
if mode == "local" and token and token in text:
    raise SystemExit("node agent: refusing to store the enroll token")
path = Path(os.environ["BOSS_REPORT_OUT"])
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(text + "\n")
path.chmod(0o600)
PY
}

node_agent_write_config() {
  python3 - <<'PY'
import os
import shlex
from pathlib import Path

path = Path(os.environ["BOSS_AGENT_CONFIG"])
path.parent.mkdir(parents=True, exist_ok=True)
fields = {
    "BOSS_CONTROL_PLANE": os.environ.get("BOSS_CONTROL_PLANE", ""),
    "BOSS_PROJECT_ID": os.environ.get("BOSS_PROJECT_ID", ""),
    "BOSS_AGENT_HOSTNAME": os.environ["BOSS_AGENT_HOSTNAME"],
    "BOSS_AGENT_ARCH": os.environ["BOSS_AGENT_ARCH"],
    "BOSS_AGENT_OS": os.environ["BOSS_AGENT_OS"],
    "BOSS_AGENT_ENDPOINT": os.environ.get("BOSS_AGENT_ENDPOINT", ""),
    "BOSS_AGENT_MODE": os.environ["BOSS_AGENT_MODE"],
}
lines = ["# BOSS node agent. The enroll token is not stored here."]
for key, value in fields.items():
    lines.append(f"{key}={shlex.quote(value)}")
text = "\n".join(lines) + "\n"
token = os.environ.get("BOSS_ENROLL_TOKEN", "")
if token and token in text:
    raise SystemExit("node agent: refusing to store the enroll token")
path.write_text(text)
path.chmod(0o600)
PY
}

node_agent_deliver() {
  local out status
  if [[ -n "${BOSS_CALL_HOME_SINK:-}" ]]; then
    BOSS_REPORT_OUT="$BOSS_CALL_HOME_SINK" node_agent_emit_report
    return
  fi
  if [[ "$BOSS_AGENT_MODE" == local && -z "${BOSS_CONTROL_PLANE:-}" ]]; then
    BOSS_REPORT_OUT="${BOSS_AGENT_CONFIG}.report.json" node_agent_emit_report
    return
  fi
  if [[ -z "${BOSS_CONTROL_PLANE:-}" ]]; then
    echo "node agent: BOSS_CONTROL_PLANE is required" >&2
    return 1
  fi
  out="$(mktemp)"
  chmod 600 "$out"
  BOSS_REPORT_OUT="$out" node_agent_emit_report
  curl --fail-with-body -sS -X POST \
    -H "content-type: application/json" \
    --data-binary @"$out" \
    "${BOSS_CONTROL_PLANE%/}/v1/hardware/agents/call-home"
  status=$?
  rm -f "$out"
  return "$status"
}

main() {
  local mode manager missing_before installed_by os_id
  umask 077
  mode="remote"
  if [[ "${1:-}" == "--local" ]]; then
    mode="local"
    shift
  fi
  if [[ "$#" -gt 0 ]]; then
    echo "node agent: unknown argument" >&2
    return 2
  fi
  export BOSS_AGENT_MODE="$mode"
  export BOSS_AGENT_HOSTNAME="${BOSS_AGENT_HOSTNAME:-$(uname -n)}"
  export BOSS_AGENT_ARCH="${BOSS_AGENT_ARCH:-$(uname -m)}"
  os_id="$(uname -s | tr '[:upper:]' '[:lower:]')"
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    if [[ -n "${ID:-}" ]]; then
      os_id="${os_id}/${ID}"
    fi
  fi
  export BOSS_AGENT_OS="${BOSS_AGENT_OS:-$os_id}"
  export BOSS_AGENT_ENDPOINT="${BOSS_AGENT_ENDPOINT:-agent://${BOSS_AGENT_HOSTNAME}}"
  if [[ -z "${BOSS_AGENT_CONFIG:-}" ]]; then
    if [[ "$(id -u)" -eq 0 ]]; then
      BOSS_AGENT_CONFIG="/etc/boss/node-agent.env"
    else
      BOSS_AGENT_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/boss/node-agent.env"
    fi
  fi
  export BOSS_AGENT_CONFIG
  missing_before="$(node_agent_missing)"
  manager="$(node_agent_require_packages)"
  installed_by=0
  if [[ -n "$missing_before" ]]; then
    installed_by=1
  fi
  export BOSS_DEP_PACKAGES="$NODE_AGENT_PACKAGES"
  export BOSS_DEP_MISSING=""
  export BOSS_DEP_INSTALLED_BY_AGENT="$installed_by"
  export BOSS_DEP_MANAGER="$manager"
  if [[ "$mode" == remote && -z "${BOSS_ENROLL_TOKEN:-}" ]]; then
    echo "enroll token rejected" >&2
    return 1
  fi
  if [[ "$mode" == local && -z "${BOSS_PROJECT_ID:-}" ]]; then
    echo "node agent: BOSS_PROJECT_ID is required" >&2
    return 1
  fi
  node_agent_write_config
  node_agent_deliver
}

if [[ "${BASH_SOURCE[0]}" == "$0" || -z "${BASH_SOURCE[0]}" ]]; then
  main "$@"
fi
