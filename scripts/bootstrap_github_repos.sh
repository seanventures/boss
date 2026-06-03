#!/usr/bin/env bash
set -euo pipefail

OWNER="${BOSS_GITHUB_OWNER:-seanheiney}"
BOSS_REPO="${BOSS_GITHUB_REPO:-boss}"
SANDBOX_REPO="${BOSS_TARGET_GITHUB_REPO:-boss-sandbox}"
DEFAULT_BRANCH="main"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
export REPO_ROOT

python3 - <<'PY'
import base64
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from typing import Optional

OWNER = os.environ.get("BOSS_GITHUB_OWNER", "seanheiney")
BOSS_REPO = os.environ.get("BOSS_GITHUB_REPO", "boss")
SANDBOX_REPO = os.environ.get("BOSS_TARGET_GITHUB_REPO", "boss-sandbox")
DEFAULT_BRANCH = "main"
REPO_ROOT = pathlib.Path(os.environ["REPO_ROOT"])


def git(
    *args: str,
    cwd: Optional[pathlib.Path] = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=check,
    )


def load_auth_headers() -> dict[str, str]:
    token = os.environ.get("BOSS_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "boss-bootstrap",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    helper = subprocess.run(
        ["git", "credential-osxkeychain", "get"],
        input="protocol=https\nhost=github.com\n\n",
        text=True,
        capture_output=True,
        check=False,
    )
    if helper.returncode == 0:
        fields: dict[str, str] = {}
        for line in helper.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                fields[key] = value
        username = fields.get("username")
        password = fields.get("password")
        if username and password:
            basic = base64.b64encode(f"{username}:{password}".encode()).decode()
            return {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Basic {basic}",
                "User-Agent": "boss-bootstrap",
                "X-GitHub-Api-Version": "2022-11-28",
            }

    raise SystemExit(
        "No usable GitHub credentials found. Set BOSS_GITHUB_TOKEN/GITHUB_TOKEN or "
        "store GitHub credentials in the macOS keychain."
    )


HEADERS = load_auth_headers()


def request(
    method: str, path: str, payload: Optional[dict] = None
) -> tuple[int, dict]:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        data=data,
        method=method,
        headers=HEADERS,
    )
    try:
        with urllib.request.urlopen(req) as response:
            body = response.read().decode()
            return response.getcode(), json.loads(body or "{}")
    except urllib.error.HTTPError as error:
        body = error.read().decode()
        try:
            parsed = json.loads(body or "{}")
        except json.JSONDecodeError:
            parsed = {"raw": body}
        return error.code, parsed


def ensure_repo(repo: str, description: str) -> None:
    code, _ = request("GET", f"/repos/{OWNER}/{repo}")
    if code == 200:
        print(f"Repo {OWNER}/{repo} already exists.")
        return
    if code != 404:
        raise SystemExit(f"Failed to inspect {OWNER}/{repo}: HTTP {code}")

    code, body = request(
        "POST",
        "/user/repos",
        {
            "name": repo,
            "private": True,
            "auto_init": False,
            "description": description,
        },
    )
    if code != 201:
        raise SystemExit(
            f"Failed to create {OWNER}/{repo}: HTTP {code}\n{json.dumps(body, indent=2)}"
        )
    print(f"Created repo {OWNER}/{repo}.")


def ensure_local_origin() -> None:
    remote_url = f"https://github.com/{OWNER}/{BOSS_REPO}.git"
    has_origin = git("remote", "get-url", "origin", cwd=REPO_ROOT, check=False).returncode == 0
    if has_origin:
        git("remote", "set-url", "origin", remote_url, cwd=REPO_ROOT)
    else:
        git("remote", "add", "origin", remote_url, cwd=REPO_ROOT)

    current_branch = git("branch", "--show-current", cwd=REPO_ROOT).stdout.strip()
    if current_branch != DEFAULT_BRANCH:
        raise SystemExit(
            f"Expected current branch {DEFAULT_BRANCH}, found {current_branch}."
        )

    push_result = git(
        "push",
        "-u",
        "origin",
        DEFAULT_BRANCH,
        cwd=REPO_ROOT,
        check=False,
    )
    if push_result.returncode == 0:
        print(f"Pushed {DEFAULT_BRANCH} to {OWNER}/{BOSS_REPO}.")
        return
    if "Everything up-to-date" in push_result.stderr or "Everything up-to-date" in push_result.stdout:
        print(f"{OWNER}/{BOSS_REPO} is already up to date.")
        return
    raise SystemExit(push_result.stderr or push_result.stdout)


def ensure_sandbox_repo() -> None:
    code, body = request("GET", f"/repos/{OWNER}/{SANDBOX_REPO}")
    if code == 404:
        ensure_repo(SANDBOX_REPO, "Real-only BOSS sandbox target repository")
    elif code != 200:
        raise SystemExit(
            f"Failed to inspect {OWNER}/{SANDBOX_REPO}: HTTP {code}\n{json.dumps(body, indent=2)}"
        )
    else:
        print(f"Repo {OWNER}/{SANDBOX_REPO} already exists.")

    default_branch = None
    if code == 200:
        default_branch = body.get("default_branch")
    if default_branch == DEFAULT_BRANCH:
        print(f"{OWNER}/{SANDBOX_REPO} already has a {DEFAULT_BRANCH} default branch.")
        return

    template_dir = REPO_ROOT / "templates" / "boss-sandbox"
    temp_dir = pathlib.Path(tempfile.mkdtemp(prefix="boss-sandbox-"))
    try:
        shutil.copytree(template_dir, temp_dir, dirs_exist_ok=True)
        git("init", "-b", DEFAULT_BRANCH, cwd=temp_dir)
        git("add", ".", cwd=temp_dir)
        git(
            "-c",
            "user.name=BOSS Bootstrap",
            "-c",
            "user.email=boss-bootstrap@local.invalid",
            "commit",
            "-m",
            "Bootstrap sandbox repository",
            cwd=temp_dir,
        )
        git(
            "remote",
            "add",
            "origin",
            f"https://github.com/{OWNER}/{SANDBOX_REPO}.git",
            cwd=temp_dir,
        )
        git("push", "-u", "origin", DEFAULT_BRANCH, cwd=temp_dir)
        print(f"Pushed sandbox template to {OWNER}/{SANDBOX_REPO}.")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def protect_sandbox_main() -> None:
    code, body = request(
        "PUT",
        f"/repos/{OWNER}/{SANDBOX_REPO}/branches/{DEFAULT_BRANCH}/protection",
        {
            "required_status_checks": {
                "strict": True,
                "contexts": ["CI"],
            },
            "enforce_admins": True,
            "required_pull_request_reviews": {
                "required_approving_review_count": 1
            },
            "restrictions": None,
        },
    )
    if code == 403 and "Upgrade to GitHub Pro" in json.dumps(body):
        print(
            "Skipping branch protection for "
            f"{OWNER}/{SANDBOX_REPO}:{DEFAULT_BRANCH}. GitHub does not allow "
            "private-repo branch protection on this account tier."
        )
        return
    if code not in (200, 201):
        raise SystemExit(
            "Failed to protect sandbox main branch: "
            f"HTTP {code}\n{json.dumps(body, indent=2)}"
        )
    print(f"Protected {OWNER}/{SANDBOX_REPO}:{DEFAULT_BRANCH}.")


ensure_repo(BOSS_REPO, "BOSS - Business Operating System Sentinel")
ensure_local_origin()
ensure_sandbox_repo()
protect_sandbox_main()

print(f"Bootstrap complete for {OWNER}/{BOSS_REPO} and {OWNER}/{SANDBOX_REPO}.")
PY
