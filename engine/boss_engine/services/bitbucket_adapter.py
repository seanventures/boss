"""BOSS-267 — Bitbucket Cloud adapter: the second ``RepoHost`` plugin (many enterprises target
Bitbucket, not GitHub).

Implements the ``RepoHost`` surface against the Bitbucket Cloud REST API 2.0
(``https://api.bitbucket.org/2.0``):

* branches   — ``refs/branches`` (create from the base branch's target hash; existing → no-op)
* files      — ``src`` (form-encoded commit of one or more files onto a branch)
* PRs        — ``pullrequests`` (create with dedupe on an open PR from the same source branch),
               ``pullrequests/{id}/merge`` (strategy mapped from ``sandbox_merge_method``),
               ``pullrequests/{id}/comments``, ``pullrequests/{id}/diff``
* clone      — ``https://x-token-auth:{token}@bitbucket.org/{workspace}/{repo_slug}.git``

Auth (env-indirect, same discipline as every BOSS connector): a workspace/repository **access
token** (``BOSS_BITBUCKET_TOKEN`` → ``Authorization: Bearer``) or a username + **app password**
(``BOSS_BITBUCKET_USERNAME``/``BOSS_BITBUCKET_APP_PASSWORD`` → Basic). GitHub's owner/repo pair
maps to Bitbucket's workspace/repo_slug.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

from .github_adapter import (
    IntegrationConfigurationError,
    IssueCommentResult,
    PullRequestMergeResult,
    PullRequestResult,
)

_API = "https://api.bitbucket.org/2.0"
_MERGE_STRATEGY = {"merge": "merge_commit", "squash": "squash", "rebase": "fast_forward"}


class BitbucketAdapter:
    kind = "bitbucket"

    def __init__(self, settings, *, workspace: Optional[str] = None,
                 repo_slug: Optional[str] = None):
        self.settings = settings
        self.workspace = workspace or getattr(settings, "target_bitbucket_workspace", "") \
            or getattr(settings, "target_github_owner", "")
        self.repo_slug = repo_slug or getattr(settings, "target_bitbucket_repo", "") \
            or getattr(settings, "target_github_repo", "")

    # --- identity -------------------------------------------------------------
    @property
    def repository_full_name(self) -> str:
        return f"{self.workspace}/{self.repo_slug}"

    def clone_url(self, token: str) -> str:
        from .repo_host import clone_url_for
        return clone_url_for("bitbucket", self.workspace, self.repo_slug,
                             token or self._token_for_clone())

    def _token_for_clone(self) -> str:
        return getattr(self.settings, "bitbucket_token", "") or ""

    # --- auth + wire ----------------------------------------------------------
    def _auth(self) -> dict[str, Any]:
        token = getattr(self.settings, "bitbucket_token", "") or ""
        if token:
            return {"headers": {"Authorization": f"Bearer {token}"}}
        user = getattr(self.settings, "bitbucket_username", "") or ""
        app_pw = getattr(self.settings, "bitbucket_app_password", "") or ""
        if user and app_pw:
            return {"auth": (user, app_pw)}
        raise IntegrationConfigurationError(
            "No Bitbucket credentials configured (need BOSS_BITBUCKET_TOKEN or "
            "BOSS_BITBUCKET_USERNAME + BOSS_BITBUCKET_APP_PASSWORD).")

    def _request(self, method: str, path: str, *, json_body: Optional[dict] = None,
                 form: Optional[dict] = None, response_text: bool = False,
                 allow_empty: bool = False) -> Any:
        auth = self._auth()
        response = httpx.request(
            method, f"{_API}{path}",
            headers={"Accept": "application/json", **auth.get("headers", {})},
            auth=auth.get("auth"),
            json=json_body, data=form, timeout=30.0,
        )
        response.raise_for_status()
        if response_text:
            return response.text
        if allow_empty and not response.content:
            return {}
        return response.json()

    def _repo_path(self) -> str:
        return f"/repositories/{self.workspace}/{self.repo_slug}"

    # --- branches -------------------------------------------------------------
    def default_branch(self) -> str:
        meta = self._request("GET", self._repo_path())
        return ((meta.get("mainbranch") or {}).get("name")) or "main"

    def ensure_branch(self, branch_name: str) -> str:
        """Create the branch off the default branch if absent; return its head hash."""
        try:
            ref = self._request("GET", f"{self._repo_path()}/refs/branches/{branch_name}")
            return ((ref.get("target") or {}).get("hash")) or ""
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 404:
                raise
        base = self.default_branch()
        base_ref = self._request("GET", f"{self._repo_path()}/refs/branches/{base}")
        base_hash = ((base_ref.get("target") or {}).get("hash")) or ""
        created = self._request("POST", f"{self._repo_path()}/refs/branches",
                                json_body={"name": branch_name, "target": {"hash": base_hash}})
        return ((created.get("target") or {}).get("hash")) or base_hash

    # --- files ----------------------------------------------------------------
    def upsert_file(self, branch_name: str, path: str, content: str, message: str) -> str:
        """Commit one file onto a branch via the src endpoint (create-or-update semantics)."""
        from .agent_acts import sign_commit_message

        message = sign_commit_message(message)
        self._request("POST", f"{self._repo_path()}/src",
                      form={path: content, "message": message, "branch": branch_name},
                      allow_empty=True)
        return path

    # --- pull requests --------------------------------------------------------
    def create_pull_request(self, branch_name: str, title: str, body: str) -> PullRequestResult:
        # Dedupe: an open PR from the same source branch is reused, mirroring GitHub behavior.
        existing = self._request(
            "GET", f"{self._repo_path()}/pullrequests"
                   f"?q=source.branch.name+%3D+%22{branch_name}%22+AND+state+%3D+%22OPEN%22")
        for pr in existing.get("values", []):
            return PullRequestResult(
                url=((pr.get("links") or {}).get("html") or {}).get("href", ""),
                number=int(pr.get("id", 0)))
        created = self._request("POST", f"{self._repo_path()}/pullrequests", json_body={
            "title": title,
            "description": body,
            "source": {"branch": {"name": branch_name}},
            "destination": {"branch": {"name": self.default_branch()}},
            "close_source_branch": True,
        })
        return PullRequestResult(
            url=((created.get("links") or {}).get("html") or {}).get("href", ""),
            number=int(created.get("id", 0)))

    def merge_pull_request(self, pull_request_number: int,
                           commit_title: str) -> PullRequestMergeResult:
        strategy = _MERGE_STRATEGY.get(
            str(getattr(self.settings, "sandbox_merge_method", "squash")).lower(), "squash")
        merged = self._request(
            "POST", f"{self._repo_path()}/pullrequests/{pull_request_number}/merge",
            json_body={"message": commit_title, "merge_strategy": strategy,
                       "close_source_branch": True})
        return PullRequestMergeResult(
            sha=((merged.get("merge_commit") or {}).get("hash")) or "",
            merged=str(merged.get("state", "")).upper() == "MERGED",
            message=commit_title)

    def create_issue_comment(self, issue_number: int, body: str) -> IssueCommentResult:
        """PR comment (Bitbucket comments live on the pull request, not a separate issue)."""
        created = self._request(
            "POST", f"{self._repo_path()}/pullrequests/{issue_number}/comments",
            json_body={"content": {"raw": body}})
        return IssueCommentResult(
            url=((created.get("links") or {}).get("html") or {}).get("href", ""),
            id=int(created.get("id", 0)))

    def get_pull_request_diff(self, pull_number: int) -> str:
        return self._request(
            "GET", f"{self._repo_path()}/pullrequests/{pull_number}/diff", response_text=True)
