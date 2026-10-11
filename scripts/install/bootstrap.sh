#!/usr/bin/env bash
# BOSS Community bootstrap.
#
#   curl -fsSL https://boss.sean.network/install.sh | bash
#   curl -fsSL https://boss.sean.network/install.sh | bash -s -- --yes      # never ask
#
# The separate website imports this product bootstrap for /install.sh.
#
# Finds the newest published BOSS release, downloads that release's install.sh from the
# public GitHub repository over HTTPS, and runs it with the release pinned. Everything after
# `bash -s --` goes to the installer (see its --help). BOSS_REF=v1.2 or --ref v1.2 installs a
# specific release; BOSS_REF=main installs the development branch.
#
# This file only chooses and downloads. It holds no credentials, sends nothing about this
# machine, and never downloads from another host. A failed download runs nothing.
#
# Everything is inside main() so a piped shell has read the whole file before it runs.

main() {
  set -euo pipefail
  umask 077
  local repo="seanventures/boss"
  local repo_url="https://github.com/${repo}.git"
  local ref="${BOSS_REF:-}"
  local args=()

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --ref)
        [[ $# -ge 2 ]] || { echo "BOSS: --ref needs a release tag, branch, or commit" >&2; return 2; }
        ref="$2"; shift 2 ;;
      --ref=*) ref="${1#--ref=}"; shift ;;
      *) args+=("$1"); shift ;;
    esac
  done

  command -v curl >/dev/null 2>&1 || { echo "BOSS: curl is required to download the installer." >&2; return 1; }

  if [[ -z "$ref" ]]; then
    local tags=""
    if command -v git >/dev/null 2>&1; then
      tags="$(git ls-remote --tags --refs "$repo_url" 'v*' </dev/null 2>/dev/null | sed 's#.*refs/tags/##' || true)"
    fi
    if [[ -z "$tags" ]]; then
      # No git yet on a fresh machine: the public tag list over HTTPS.
      tags="$(curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 \
                "https://api.github.com/repos/${repo}/tags?per_page=100" 2>/dev/null |
              grep -o '"name": *"v[^"]*"' | sed 's/.*"\(v[^"]*\)"$/\1/' || true)"
    fi
    ref="$(printf '%s\n' "$tags" | pick_release 0)"
    [[ -n "$ref" ]] || ref="$(printf '%s\n' "$tags" | pick_release 1)"
    if [[ -z "$ref" ]]; then
      echo "BOSS: could not find a published release. Check the network, or set BOSS_REF=main." >&2
      return 1
    fi
  fi
  if ! [[ "$ref" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ && "$ref" != *..* ]]; then
    echo "BOSS: --ref must be a release tag, branch name, or full commit SHA." >&2
    return 2
  fi

  echo "BOSS: installing release ${ref} from github.com/${repo}"
  local installer
  installer="$(mktemp)"
  # shellcheck disable=SC2064
  trap "rm -f '$installer'" EXIT
  if ! curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 \
       "https://raw.githubusercontent.com/${repo}/${ref}/install.sh" -o "$installer"; then
    echo "BOSS: could not download install.sh for ${ref}; nothing was installed." >&2
    return 1
  fi
  if ! head -n 1 "$installer" | grep -q '^#!/usr/bin/env bash' || ! grep -q 'BOSS Community' "$installer"; then
    echo "BOSS: the download for ${ref} is not the BOSS installer; nothing was run." >&2
    return 1
  fi
  # BOSS_REPO_REF pins the checkout to the same ref the installer came from. The installer
  # reads its questions from the terminal itself, so stdin (this pipe) is not needed.
  BOSS_REPO_REF="$ref" bash "$installer" ${args[@]+"${args[@]}"} </dev/null
}

# stdin: tag names. $1: 1 to consider prereleases. stdout: the newest tag or nothing.
# The same program as pick_release in scripts/install/upgrade.sh; a test keeps them identical.
pick_release() {
  # >>> boss_pick_release
  LC_ALL=C awk -v pre="${1:-0}" '
    /^v[0-9]+(\.[0-9]+)*(-[0-9A-Za-z.]+)?$/ {
      core = substr($0, 2); label = ""
      cut = index(core, "-")
      if (cut > 0) { label = substr(core, cut + 1); core = substr(core, 1, cut - 1) }
      if (label != "" && pre != 1) next
      n = split(core, part, ".")
      if (n > 3) next
      printf "%09d.%09d.%09d.%s\t%s\n", part[1], (n >= 2 ? part[2] : 0), (n >= 3 ? part[3] : 0), (label == "" ? "~" : label), $0
    }' | LC_ALL=C sort | tail -n 1 | cut -f 2
  # <<< boss_pick_release
}

main "$@"
