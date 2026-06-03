#!/usr/bin/env bash
set -euo pipefail

OWNER="${BOSS_GITHUB_OWNER:-seanheiney}"
BOSS_REPO="${BOSS_GITHUB_REPO:-boss}"
SANDBOX_REPO="${BOSS_TARGET_GITHUB_REPO:-boss-sandbox}"
DEFAULT_BRANCH="main"

if ! command -v gh >/dev/null 2>&1; then
  echo "gh CLI is required." >&2
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "GitHub CLI is not authenticated. Run: gh auth login" >&2
  exit 1
fi

ensure_repo() {
  local repo="$1"
  if gh repo view "${OWNER}/${repo}" >/dev/null 2>&1; then
    echo "Repo ${OWNER}/${repo} already exists."
    if ! git remote get-url origin >/dev/null 2>&1; then
      git remote add origin "git@github.com:${OWNER}/${repo}.git"
    fi
  else
    gh repo create "${OWNER}/${repo}" --private
    if ! git remote get-url origin >/dev/null 2>&1; then
      git remote add origin "git@github.com:${OWNER}/${repo}.git"
    fi
    if git rev-parse HEAD >/dev/null 2>&1; then
      git push -u origin main
    else
      echo "Local repo has no commits yet; create the first commit before pushing origin/main."
    fi
  fi
}

ensure_sandbox_repo() {
  if gh repo view "${OWNER}/${SANDBOX_REPO}" >/dev/null 2>&1; then
    echo "Repo ${OWNER}/${SANDBOX_REPO} already exists."
    return
  fi

  local temp_dir
  temp_dir="$(mktemp -d)"
  rsync -a templates/boss-sandbox/ "${temp_dir}/"

  (
    cd "${temp_dir}"
    git init -b "${DEFAULT_BRANCH}"
    git add .
    git commit -m "Bootstrap sandbox repository"
    gh repo create "${OWNER}/${SANDBOX_REPO}" --private --source=. --push --remote=origin
  )

  rm -rf "${temp_dir}"
}

protect_sandbox_main() {
  gh api \
    --method PUT \
    -H "Accept: application/vnd.github+json" \
    "/repos/${OWNER}/${SANDBOX_REPO}/branches/${DEFAULT_BRANCH}/protection" \
    -f required_status_checks.strict=true \
    -f enforce_admins=true \
    -f required_pull_request_reviews.required_approving_review_count=1 \
    -f restrictions= \
    -F required_status_checks.contexts[]="CI"
}

set_secret_if_present() {
  local repo="$1"
  local secret_name="$2"
  local secret_value="${!secret_name:-}"

  if [[ -n "${secret_value}" ]]; then
    gh secret set "${secret_name}" --repo "${OWNER}/${repo}" --body "${secret_value}"
  else
    echo "Skipping ${secret_name} for ${repo}; env var not set."
  fi
}

ensure_repo "${BOSS_REPO}"
ensure_sandbox_repo
protect_sandbox_main

set_secret_if_present "${BOSS_REPO}" OPENAI_API_KEY
set_secret_if_present "${BOSS_REPO}" BOSS_GITHUB_TOKEN

echo "Bootstrap complete for ${OWNER}/${BOSS_REPO} and ${OWNER}/${SANDBOX_REPO}."
