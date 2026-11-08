"""Where a repository, a file in it, or a pull request is shown — GitHub's page, or BOSS's own
for a repository BOSS keeps on this machine (local-first projects, BOSSAPP-915).

A local repository is named `local_<project_id>/<name>` and has no web page anywhere but BOSS's
console; a local change set's URL is `http://boss-local/local_<project_id>/<name>/pull/<n>`
(local_repo_host.change_set_url), a placeholder no browser can open. Every reader that turns a
repository name or a pull-request URL into a link asks here, so no view builds
`https://github.com/local_…` or sends a person to `http://boss-local/…`.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from . import local_repo
from .local_repo_host import LOCAL_PR_HOST, is_local_pr_url

__all__ = ["is_local_repo", "host_of", "host_label", "repo_url", "blob_url", "change_view_path",
           "pr_link", "parse_local_change"]

_LOCAL_PR = re.compile(r"://" + re.escape(LOCAL_PR_HOST) + r"/(local_[^/]+)/([^/]+)/pull/(\d+)")


def is_local_repo(full_name: Any) -> bool:
    return local_repo.is_local(full_name)


def host_of(full_name: Any, host: Any = None) -> str:
    """'local' or 'github' (a binding's own `host` column wins when it says so)."""
    if str(host or "").lower() == "local" or is_local_repo(full_name):
        return "local"
    return str(host or "github").lower() or "github"


def host_label(full_name: Any, host: Any = None) -> str:
    """What a readiness dot or a chip calls the repository's host."""
    return "Local repo" if host_of(full_name, host) == "local" else "GitHub"


def repo_url(full_name: Any) -> Optional[str]:
    """The repository's page on GitHub; None for a local repository (BOSS's console shows it)."""
    name = str(full_name or "").strip().strip("/")
    if not name or "/" not in name or is_local_repo(name):
        return None
    return f"https://github.com/{name}"


def blob_url(full_name: Any, path: str, ref: str = "HEAD") -> Optional[str]:
    base = repo_url(full_name)
    if base is None:
        return None
    return f"{base}/blob/{ref}/{str(path or '').lstrip('/')}"


def parse_local_change(url: Any) -> Optional[dict[str, Any]]:
    """{project_id, repository, number} of a local change set's URL, else None."""
    match = _LOCAL_PR.search(str(url or ""))
    if not match:
        return None
    owner, repo, number = match.group(1), match.group(2), int(match.group(3))
    return {"project_id": local_repo.project_of(owner), "repository": f"{owner}/{repo}",
            "number": number}


def change_view_path(url: Any) -> Optional[str]:
    """BOSS's console page for a local change set (apps/web app/changes/[projectId]/[number])."""
    parsed = parse_local_change(url)
    if parsed is None:
        return None
    return f"/changes/{parsed['project_id']}/{parsed['number']}"


def pr_link(url: Any) -> Optional[str]:
    """Where a person opens this pull request: GitHub's page, or BOSS's change-set view."""
    if not url:
        return None
    if is_local_pr_url(url):
        return change_view_path(url)
    return str(url)
