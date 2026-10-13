"""A GitHub API failure as a person reads it: what was refused, which permission, where to fix it.

A pull request's approval card carried the raw client error — "Client error '403 Forbidden' for
url 'https://api.github.com/repos/…/check-runs' For more information check: https://developer.
mozilla.org/…" — which names neither the missing permission nor where to grant it.
"""
from __future__ import annotations

import re
from typing import Any

_STATUS = re.compile(r"\b(401|403|404|422|429)\b")
_URL = re.compile(r"https?://api\.github\.com/(\S+?)['\"]?(?:\s|$)")

#: What a path on the GitHub API reads or writes, and the fine-grained permission it needs.
_NEEDS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"/check-runs|/check-suites"), "this commit's checks", "Checks: Read"),
    (re.compile(r"/actions/"), "this repository's workflow runs", "Actions: Read"),
    (re.compile(r"/statuses|/status\b"), "this commit's statuses", "Commit statuses: Read"),
    (re.compile(r"/pulls/\d+/(reviews|comments)"), "pull request reviews",
     "Pull requests: Read and write"),
    (re.compile(r"/pulls"), "pull requests", "Pull requests: Read and write"),
    (re.compile(r"/contents/|/git/"), "the repository's files", "Contents: Read and write"),
)

WHERE = "Repo and models › GitHub"


def github_error_in_words(error: Any, *, doing: str = "") -> str:
    """One or two sentences for a GitHub failure, or the error's own text when it is not one."""
    text = str(error or "").strip()
    status = _STATUS.search(text)
    if not status:
        return text.splitlines()[0][:300] if text else ""
    code = status.group(1)
    path = (_URL.search(text) or [None, ""])[1] if _URL.search(text) else ""
    what, permission = next(((w, p) for rx, w, p in _NEEDS if rx.search(path or text)),
                            ("this", ""))
    lead = f"{doing}: " if doing else ""
    if code == "401":
        return (f"{lead}GitHub did not accept the project's token (401): it is wrong or has "
                f"expired. Replace it in {WHERE}.")
    if code == "403":
        need = f" It needs the “{permission}” permission." if permission else ""
        return (f"{lead}GitHub refused to show {what} (403): the project's token is missing a "
                f"permission.{need} Grant it in {WHERE}.")
    if code == "404":
        return (f"{lead}GitHub could not find {what} (404): the project's token cannot see this "
                f"repository, or it was renamed. Check the token's repository access in {WHERE}.")
    if code == "429":
        return f"{lead}GitHub is rate-limiting this token (429). It will be tried again later."
    return f"{lead}GitHub refused the request ({code})."
