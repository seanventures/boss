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
           "change_ref", "change_link", "change_title", "local_words",
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


def change_ref(url: Any, number: Any = None, *, lower: bool = False, local: bool = False) -> str:
    """How a person names it: "Change #1" for a local change set (or any change of a `local`
    project), "PR #1" for GitHub's — the engine's twin of the console's changeRef."""
    if local or is_local_pr_url(url) or is_local_repo(url):
        word = "change" if lower else "Change"
        return f"{word} #{number}" if number else f"the change" if lower else "The change"
    if number:
        return f"PR #{number}"
    return "the pull request" if lower else "The pull request"


def change_link(url: Any, number: Any = None) -> Optional[dict[str, Any]]:
    """A link to it as a view draws it: GitHub's page opens in a new tab (`external_url`); a
    local change set opens BOSS's own change-set view (`href`), never `http://boss-local/…`."""
    if not url:
        return None
    label = change_ref(url, number)
    if is_local_pr_url(url):
        return {"label": label, "href": change_view_path(url)}
    return {"label": label, "external_url": str(url)}


#: GitHub's words, and a local project's (BOSSAPP-946): a person reads "change", never "PR".
_GITHUB_WORDS = (
    (re.compile(r"\bdraft (?:PR|pull request)s?\b", re.I), "draft change"),
    (re.compile(r"\bpull requests\b", re.I), "changes"),
    (re.compile(r"\bpull request\b", re.I), "change"),
    (re.compile(r"\bPRs\b"), "changes"),
    (re.compile(r"\bPR #(\d+)"), r"change #\1"),
    (re.compile(r"\bPR\b"), "change"),
)


def local_words(text_: Any) -> str:
    """A sentence a local project shows, in its own words: an agent's "Open a draft PR bumping
    esbuild" reads "Open a draft change bumping esbuild". Text only — never a URL or a name."""
    out = str(text_ or "")
    for pattern, word in _GITHUB_WORDS:
        out = pattern.sub(word, out)
    # A sentence that began with the word keeps its capital.
    if out[:1].islower() and str(text_ or "")[:1].isupper():
        return out[:1].upper() + out[1:]
    return out


def change_title(connection: Any, url: Any, fallback: Any = None) -> Optional[str]:
    """A CHANGE SET KEEPS ITS OWN TITLE. Follow-up builds on the same branch (a repair, More
    tests) add commits to it; they never rename it. A local change set's title is its own row's
    (local_change_sets, set when it opened); a pull request's is its first build's. Before this,
    a view named the merged change after whichever build recorded the merge — HelpDesk's change
    #1 read "Follow-up: More tests — …" after its merge (BOSSAPP-946 follow-up)."""
    from sqlalchemy import text as _text

    if not url:
        return fallback
    try:
        with connection.begin_nested():
            parsed = parse_local_change(url)
            if parsed is not None:
                title = connection.execute(_text(
                    "SELECT title FROM local_change_sets WHERE repository = :r AND number = :n "
                    "ORDER BY created_at LIMIT 1"),
                    {"r": parsed["repository"], "n": parsed["number"]}).scalar()
            else:
                title = connection.execute(_text(
                    "SELECT title FROM code_task_runs WHERE pull_request_url = :u "
                    "AND COALESCE(title, '') <> '' ORDER BY created_at LIMIT 1"),
                    {"u": str(url)}).scalar()
    except Exception:  # noqa: BLE001 — the caller's own words stand
        return fallback
    return str(title) if title else fallback


def pr_link(url: Any) -> Optional[str]:
    """Where a person opens this pull request: GitHub's page, or BOSS's change-set view."""
    if not url:
        return None
    if is_local_pr_url(url):
        return change_view_path(url)
    return str(url)
