"""BOSS's own GitHub calls for a change set: read a PR, edit its description, merge it at an exact
SHA, revert a merge through a pull request. Each call uses a credential for ONE repository
(github_app_adapter.repo_token: an installation token minted for that repository alone, else the
project's registered credential) and is checked against the set's grant first.

`TRANSPORT` is the one HTTP edge; tests replace it with a recording fake."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import quote

from .common import MultiRepoError


@dataclass
class Answer:
    status: int
    body: Any


def _default_transport(method: str, path: str, token: str, body: Any = None) -> Answer:
    from ..identity import github_app

    url = github_app.api_base().rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "boss-multirepo",
        **({"Content-Type": "application/json"} if data is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — GitHub API base only
            raw = resp.read()
            return Answer(resp.status, json.loads(raw) if raw else {})
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {"message": raw[:300].decode("utf-8", "replace")}
        return Answer(error.code, parsed)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return Answer(599, {"message": f"GitHub unreachable: {type(error).__name__}"})


TRANSPORT: Callable[[str, str, str, Any], Answer] = _default_transport
TokenFor = Callable[[Any, Any, str, str], str]


def default_token(db: Any, settings: Any, project_id: str, full_name: str) -> str:
    from ..services.github_app_adapter import repo_token

    owner, _, name = full_name.partition("/")
    return repo_token(settings, owner, name, connection=db, project_id=project_id)


TOKEN_FOR: TokenFor = default_token


class Repo:
    """One repository of one change set: every call is inside the set's grant."""

    def __init__(self, db: Any, settings: Any, project_id: str, change_set_id: str, full_name: str, *,
                 access: str = "write"):
        from . import grants

        grants.assert_within(db, change_set_id, full_name, access=access)
        self.full_name = full_name
        try:
            self._token = TOKEN_FOR(db, settings, project_id, full_name)
        except Exception as error:  # noqa: BLE001 — said as "Fix in GitHub", never a stack trace
            raise MultiRepoError(502, "no_github_access",
                                 f"BOSS has no working GitHub access to {full_name}. Fix it in GitHub "
                                 "(the App's repository access), then recheck.") from error

    def call(self, method: str, path: str, body: Any = None) -> Answer:
        return TRANSPORT(method, f"/repos/{self.full_name}{path}", self._token, body)

    def pull(self, number: int) -> dict[str, Any]:
        got = self.call("GET", f"/pulls/{int(number)}")
        if got.status != 200 or not isinstance(got.body, dict):
            raise MultiRepoError(502, "pull_unreadable", f"GitHub did not return {self.full_name}#{number}.")
        return got.body

    def files(self, number: int) -> list[dict[str, Any]]:
        got = self.call("GET", f"/pulls/{int(number)}/files?per_page=100")
        return got.body if got.status == 200 and isinstance(got.body, list) else []

    def set_body(self, number: int, body: str) -> bool:
        return self.call("PATCH", f"/pulls/{int(number)}", {"body": body}).status == 200

    def merge(self, number: int, sha: str, *, title: str) -> Answer:
        """Merge exactly `sha`: GitHub refuses (409) when the head moved."""
        return self.call("PUT", f"/pulls/{int(number)}/merge",
                         {"sha": sha, "merge_method": "squash", "commit_title": title[:200]})

    def ref(self, branch: str) -> Optional[str]:
        got = self.call("GET", f"/git/ref/heads/{quote(branch, safe='/')}")
        if got.status == 200 and isinstance(got.body, dict):
            return str((got.body.get("object") or {}).get("sha") or "") or None
        return None

    def commit(self, sha: str) -> Optional[dict[str, Any]]:
        got = self.call("GET", f"/git/commits/{sha}")
        return got.body if got.status == 200 and isinstance(got.body, dict) else None

    def revert_merge(self, *, merged_sha: str, base: str, branch: str, title: str, body: str) -> dict[str, Any]:
        """Undo a merge THROUGH A PULL REQUEST (never a push to the base): a commit whose tree is
        the base before the merge, on a boss/* branch, opened as a PR and merged. Refuses when
        something else landed on the base after the merge (that is a person's call)."""
        head = self.ref(base)
        if head != merged_sha:
            return {"ok": False, "reason": f"{base} moved after the merge; a person must revert {self.full_name}."}
        merged = self.commit(merged_sha) or {}
        parents = merged.get("parents") or []
        if not parents:
            return {"ok": False, "reason": "the merge commit has no parent to restore."}
        before = self.commit(str(parents[0].get("sha"))) or {}
        tree = (before.get("tree") or {}).get("sha")
        if not tree:
            return {"ok": False, "reason": "GitHub did not return the tree before the merge."}
        made = self.call("POST", "/git/commits", {"message": title, "tree": tree, "parents": [merged_sha]})
        if made.status not in (200, 201):
            return {"ok": False, "reason": f"GitHub refused the revert commit ({made.status})."}
        revert_sha = str(made.body.get("sha"))
        ref = self.call("POST", "/git/refs", {"ref": f"refs/heads/{branch}", "sha": revert_sha})
        if ref.status not in (200, 201):
            return {"ok": False, "reason": f"GitHub refused the revert branch ({ref.status})."}
        pr = self.call("POST", "/pulls", {"title": title, "head": branch, "base": base, "body": body})
        if pr.status not in (200, 201):
            return {"ok": False, "reason": f"GitHub refused the revert pull request ({pr.status})."}
        number = int(pr.body.get("number"))
        done = self.merge(number, revert_sha, title=title)
        if done.status != 200:
            return {"ok": False, "pr_number": number,
                    "reason": f"The revert pull request #{number} could not be merged ({done.status})."}
        return {"ok": True, "pr_number": number, "sha": str((done.body or {}).get("sha") or revert_sha)}
