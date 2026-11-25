#!/usr/bin/env bash
# Reset the API gateway onboarding demo to a fresh state.
#
#   reset.sh --local <dir>                build the tenant repo at <dir> (replaced if it exists)
#   reset.sh --github <owner/repo> --yes  reset that github.com repo and open the partner's pull request
#
# Both build the same history: main holds the gateway team's repo (rules, checker, contract
# tests, one passing tenant); branch onboard/lumen-pay adds the partner's first, broken file at
# config/tenants/lumen-pay.yaml. With --github, open pull requests are closed, main is
# force-pushed, every other branch is deleted, and the branch is pushed with a new pull request.
# That push and pull request are what BOSS's GitHub intake sees (filter: data.changed_files glob
# config/tenants/*.yaml), so the demo starts the way a real partner change does.
#
# Safety (fails closed):
#   --local  refuses a non-empty directory without the .northgate-demo marker, and any directory
#            inside this example (it would wipe the shipped tenant-repo).
#   --github talks to github.com only (GH_HOST is ignored; every gh call and the push name the
#            same host). It proceeds only when main has the .northgate-demo marker, or when GitHub
#            says the repository is empty. Any other answer (an error, no main, no login, a rate
#            limit) stops it before anything is changed.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd -P)"
BRANCH="onboard/lumen-pay"
TARGET="config/tenants/lumen-pay.yaml"
HOST="github.com"
GIT=(git -c user.name="Northgate demo" -c user.email="demo@northgate.example" -c init.defaultBranch=main)

usage() { sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
die() { echo "Refusing: $*" >&2; exit 1; }

absolute() {  # the absolute, symlink-free path of $1 (its parent must exist or is created)
  local parent base
  parent="$(dirname "$1")"; base="$(basename "$1")"
  mkdir -p "$parent"
  if [ -d "$1" ]; then (cd "$1" && pwd -P); else echo "$(cd "$parent" && pwd -P)/$base"; fi
}

build() {  # build <dir>: a fresh repo with main and the partner's branch
  local dir
  dir="$(absolute "$1")"
  case "$dir/" in
    "$HERE/"*) die "$dir is inside the example itself ($HERE)." ;;
  esac
  [ "$dir" != "/" ] || die "the root directory."
  if [ -e "$dir" ] && [ -n "$(ls -A "$dir" 2>/dev/null)" ] && [ ! -e "$dir/.northgate-demo" ]; then
    die "$dir is not empty and is not a demo tenant repo (no .northgate-demo)."
  fi
  rm -rf "$dir"
  mkdir -p "$dir"
  cp -R "$HERE/tenant-repo/." "$dir/"
  (
    cd "$dir"
    "${GIT[@]}" init -q
    "${GIT[@]}" add -A
    "${GIT[@]}" commit -q -m "Northgate tenant repo: rules, checker, contract tests"
    "${GIT[@]}" checkout -q -b "$BRANCH"
    cp "$HERE/partner/lumen-pay.bad.yaml" "$TARGET"
    "${GIT[@]}" add "$TARGET"
    "${GIT[@]}" commit -q -m "Onboard Lumen Pay to the gateway"
    "${GIT[@]}" checkout -q main
  )
  BUILT="$dir"
}

gh_api() { gh api --hostname "$HOST" "$@"; }

github_safe() {  # github_safe <owner/repo>: succeed only for a demo repo's main or an empty repo
  local repo="$1" out
  if out="$(gh_api "repos/$repo/commits?sha=main&per_page=1" 2>&1)"; then
    gh_api "repos/$repo/contents/.northgate-demo?ref=main" >/dev/null 2>&1 \
      || die "$HOST/$repo has a main branch without the .northgate-demo marker."
    return 0
  fi
  case "$out" in
    *"Git Repository is empty"*) return 0 ;;
  esac
  die "could not confirm $HOST/$repo is a demo repo (GitHub said: $(printf '%s' "$out" | head -1))."
}

case "${1:-}" in
  --local)
    [ -n "${2:-}" ] || usage
    build "$2"
    echo "Built $BUILT: main + $BRANCH (the partner's file at $TARGET)."
    ;;
  --github)
    REPO="${2:-}"
    [ -n "$REPO" ] && [ "${3:-}" = "--yes" ] || usage
    [[ "$REPO" =~ ^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$ ]] || die "'$REPO' is not owner/repo."
    command -v gh >/dev/null || die "gh (GitHub CLI) is needed for --github."
    unset GH_HOST GH_REPO
    github_safe "$REPO"
    WORK="$(mktemp -d)"
    trap 'rm -rf "$WORK"' EXIT
    build "$WORK/repo"
    cd "$BUILT"
    "${GIT[@]}" remote add origin "https://$HOST/$REPO.git"
    OPEN="$(gh pr list --repo "$HOST/$REPO" --state open --json number --jq '.[].number')"
    for n in $OPEN; do
      gh pr close "$n" --repo "$HOST/$REPO" --comment "Demo reset." >/dev/null
    done
    "${GIT[@]}" push -q --force origin main
    HEADS="$("${GIT[@]}" ls-remote --heads origin | awk '{print $2}' | sed 's#refs/heads/##')"
    for b in $HEADS; do
      [ "$b" = "main" ] || "${GIT[@]}" push -q origin --delete "$b"
    done
    "${GIT[@]}" push -q origin "$BRANCH"
    gh pr create --repo "$HOST/$REPO" --base main --head "$BRANCH" \
      --title "Onboard Lumen Pay to the gateway" \
      --body "Adds Lumen Pay's tenant configuration ($TARGET)."
    ;;
  *) usage ;;
esac
