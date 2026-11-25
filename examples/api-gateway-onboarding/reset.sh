#!/usr/bin/env bash
# Reset the API gateway onboarding demo to a fresh state.
#
#   reset.sh --local <dir>                build the tenant repo at <dir> (replaced if it exists)
#   reset.sh --github <owner/repo> --yes  reset that GitHub repo and open the partner's pull request
#
# Both build the same history: main holds the gateway team's repo (rules, checker, contract
# tests, one passing tenant); branch onboard/lumen-pay adds the partner's first, broken file at
# config/tenants/lumen-pay.yaml. With --github, main is force-pushed, every other branch is
# deleted, open pull requests are closed, and the branch is pushed with a new pull request. That
# push and pull request are what BOSS's GitHub intake sees (filter: data.changed_files glob
# config/tenants/*.yaml), so the demo starts the way a real partner change does.
#
# Safety: it refuses a non-empty directory, or a GitHub repository with commits, that does not
# carry the .northgate-demo marker.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BRANCH="onboard/lumen-pay"
TARGET="config/tenants/lumen-pay.yaml"
GIT=(git -c user.name="Northgate demo" -c user.email="demo@northgate.example" -c init.defaultBranch=main)

usage() { sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

build() {  # build <dir>: a fresh repo with main and the partner's branch
  local dir="$1"
  if [ -e "$dir" ] && [ -n "$(ls -A "$dir" 2>/dev/null)" ] && [ ! -e "$dir/.northgate-demo" ]; then
    echo "Refusing: $dir is not empty and is not a demo tenant repo (no .northgate-demo)." >&2
    exit 1
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
}

case "${1:-}" in
  --local)
    [ -n "${2:-}" ] || usage
    build "$2"
    echo "Built $2: main + $BRANCH (the partner's file at $TARGET)."
    ;;
  --github)
    REPO="${2:-}"
    [ -n "$REPO" ] && [ "${3:-}" = "--yes" ] || usage
    command -v gh >/dev/null || { echo "gh (GitHub CLI) is needed for --github" >&2; exit 1; }
    if gh api "repos/$REPO/commits?per_page=1" >/dev/null 2>&1 \
       && ! gh api "repos/$REPO/contents/.northgate-demo" >/dev/null 2>&1; then
      echo "Refusing: $REPO has commits but no .northgate-demo marker on its default branch." >&2
      exit 1
    fi
    WORK="$(mktemp -d)"
    trap 'rm -rf "$WORK"' EXIT
    build "$WORK/repo"
    cd "$WORK/repo"
    "${GIT[@]}" remote add origin "https://github.com/$REPO.git"
    for n in $(gh pr list --repo "$REPO" --state open --json number --jq '.[].number'); do
      gh pr close "$n" --repo "$REPO" --comment "Demo reset." >/dev/null
    done
    "${GIT[@]}" push -q --force origin main
    for b in $(git ls-remote --heads origin | awk '{print $2}' | sed 's#refs/heads/##'); do
      [ "$b" = "main" ] || "${GIT[@]}" push -q origin --delete "$b"
    done
    "${GIT[@]}" push -q origin "$BRANCH"
    gh pr create --repo "$REPO" --base main --head "$BRANCH" \
      --title "Onboard Lumen Pay to the gateway" \
      --body "Adds Lumen Pay's tenant configuration ($TARGET)."
    ;;
  *) usage ;;
esac
