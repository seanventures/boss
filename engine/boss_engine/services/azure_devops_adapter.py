"""BOSSAPP-937 (Kiro gap 4) — Azure DevOps Repos adapter implementing the ``RepoHost`` seam.

Supports Azure DevOps Services (``https://dev.azure.com/{org}``) and Azure DevOps Server behind
the same ``RepoHost`` interface used by GitHub, Bitbucket, GitLab, and Local repositories:

* clone      — ``https://pat:{token}@dev.azure.com/{org}/{project}/_git/{repo}``
* branches   — ``/_apis/git/repositories/{repo}/refs`` (create off default branch; existing → no-op)
* commits    — ``/_apis/git/repositories/{repo}/pushes`` (signed commit with add/edit change)
* PRs        — ``/_apis/git/repositories/{repo}/pullrequests`` (deduped on active PR from same branch),
               ``PATCH /pullrequests/{id}`` (status=completed with merge strategy),
               ``/pullrequests/{id}/iterations`` (diff)
* reviews    — ``/pullrequests/{id}/threads`` (general and inline file/line review threads)
* merge truth — ``pull_request_snapshot(pr_id)`` normalizes status/mergeStatus/commits
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

import httpx

from .github_adapter import (
    IntegrationConfigurationError,
    IssueCommentResult,
    PullRequestMergeResult,
    PullRequestResult,
    PullRequestReviewResult,
)

_API_VERSION = "7.1"
_ZERO_SHA = "0" * 40
_MERGE_STRATEGY = {
    "merge": "noFastForward",
    "squash": "squash",
    "rebase": "rebase",
}


class AzureDevOpsAdapter:
    kind = "azure_devops"

    def __init__(
        self,
        settings,
        *,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
        project: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.settings = settings
        self.base_url = (
            base_url
            or getattr(settings, "azure_devops_base_url", "")
            or "https://dev.azure.com"
        ).rstrip("/")
        raw_owner = (
            owner
            or getattr(settings, "azure_devops_org", "")
            or getattr(settings, "target_github_owner", "")
        )
        # Accept either "org/project" in owner or separate org + azure_devops_project.
        if "/" in raw_owner and not project:
            org_part, proj_part = raw_owner.split("/", 1)
            self.org = org_part
            self.project = proj_part
        else:
            self.org = raw_owner
            self.project = (
                project
                or getattr(settings, "azure_devops_project", "")
                or raw_owner
            )
        self.repo = (
            repo
            or getattr(settings, "azure_devops_repo", "")
            or getattr(settings, "target_github_repo", "")
        )

    # --- identity -------------------------------------------------------------
    @property
    def repository_full_name(self) -> str:
        if self.org and self.project and self.project != self.org:
            return f"{self.org}/{self.project}/{self.repo}"
        return f"{self.org}/{self.repo}"

    def clone_url(self, token: str = "") -> str:
        from .repo_host import clone_url_for

        owner = f"{self.org}/{self.project}" if self.project else self.org
        return clone_url_for(
            "azure_devops",
            owner,
            self.repo,
            token or self._pat(),
            web_base=self.base_url,
        )

    def _pat(self) -> str:
        return getattr(self.settings, "azure_devops_pat", "") or ""

    # --- auth + wire ----------------------------------------------------------
    def _auth(self) -> tuple[str, str]:
        pat = self._pat()
        if not pat:
            raise IntegrationConfigurationError(
                "No Azure DevOps credentials configured (need BOSS_AZURE_DEVOPS_PAT)."
            )
        return ("", pat)

    def _repo_base(self) -> str:
        return (
            f"{self.base_url}/{quote(self.org, safe='')}/{quote(self.project, safe='')}"
            f"/_apis/git/repositories/{quote(self.repo, safe='')}"
        )

    def _web_pr_url(self, pr_id: int) -> str:
        return f"{self.base_url}/{self.org}/{self.project}/_git/{self.repo}/pullrequest/{int(pr_id)}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
        allow_empty: bool = False,
    ) -> Any:
        query = {"api-version": _API_VERSION, **(params or {})}
        url = f"{self._repo_base()}{path}"
        response = httpx.request(
            method,
            url,
            headers={"Accept": "application/json"},
            auth=self._auth(),
            json=json_body,
            params=query,
            timeout=30.0,
        )
        response.raise_for_status()
        if allow_empty and not response.content:
            return {}
        return response.json()

    # --- branches -------------------------------------------------------------
    def default_branch(self) -> str:
        meta = self._request("GET", "")
        raw = str(meta.get("defaultBranch") or "refs/heads/main")
        return raw.removeprefix("refs/heads/")

    def _branch_sha(self, branch_name: str) -> str:
        data = self._request("GET", "/refs", params={"filter": f"heads/{branch_name}"})
        for ref in data.get("value") or []:
            if ref.get("name") == f"refs/heads/{branch_name}":
                return str(ref.get("objectId") or "")
        return ""

    def ensure_branch(self, branch_name: str, base_branch: Optional[str] = None) -> str:
        """Create the branch off the default/base branch if absent; return its commit SHA."""
        existing = self._branch_sha(branch_name)
        if existing:
            return existing
        base = base_branch or self.default_branch()
        base_sha = self._branch_sha(base)
        created = self._request(
            "POST",
            "/refs",
            json_body=[
                {
                    "name": f"refs/heads/{branch_name}",
                    "oldObjectId": _ZERO_SHA,
                    "newObjectId": base_sha,
                }
            ],
        )
        values = created.get("value") or []
        if values and values[0].get("newObjectId"):
            return str(values[0]["newObjectId"])
        return base_sha

    # --- files / commits ------------------------------------------------------
    def upsert_file(self, branch_name: str, path: str, content: str, message: str) -> str:
        """Push a signed single-file add/edit commit onto ``branch_name``."""
        from .agent_acts import sign_commit_message

        signed_message = sign_commit_message(message)
        old_sha = self._branch_sha(branch_name) or self.ensure_branch(branch_name)
        item_path = "/" + path.lstrip("/")
        change_type = "edit"
        try:
            self._request(
                "GET",
                "/items",
                params={"path": item_path, "versionDescriptor.version": branch_name},
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 404:
                raise
            change_type = "add"
        self._request(
            "POST",
            "/pushes",
            json_body={
                "refUpdates": [{"name": f"refs/heads/{branch_name}", "oldObjectId": old_sha}],
                "commits": [
                    {
                        "comment": signed_message,
                        "changes": [
                            {
                                "changeType": change_type,
                                "item": {"path": item_path},
                                "newContent": {"content": content, "contentType": "rawtext"},
                            }
                        ],
                    }
                ],
            },
        )
        return path

    # --- pull requests --------------------------------------------------------
    def create_pull_request(
        self,
        branch_name: str,
        title: str,
        body: str,
        *,
        draft: bool = False,
        base: Optional[str] = None,
    ) -> PullRequestResult:
        from .run_secret_redaction import scrub as _scrub_known

        title, body = _scrub_known(title), _scrub_known(body)
        existing = self._request(
            "GET",
            "/pullrequests",
            params={
                "searchCriteria.sourceRefName": f"refs/heads/{branch_name}",
                "searchCriteria.status": "active",
            },
        )
        for pr in existing.get("value") or []:
            pr_id = int(pr.get("pullRequestId") or 0)
            return PullRequestResult(url=self._web_pr_url(pr_id), number=pr_id)
        target = base or self.default_branch()
        created = self._request(
            "POST",
            "/pullrequests",
            json_body={
                "sourceRefName": f"refs/heads/{branch_name}",
                "targetRefName": f"refs/heads/{target}",
                "title": title,
                "description": body,
                "isDraft": bool(draft),
            },
        )
        pr_id = int(created.get("pullRequestId") or 0)
        return PullRequestResult(url=self._web_pr_url(pr_id), number=pr_id)

    def pull_request_snapshot(self, pull_number: int) -> dict[str, Any]:
        """Return canonical merge-truth snapshot for an Azure DevOps PR."""
        pr = self._request("GET", f"/pullrequests/{int(pull_number)}")
        raw_status = str(pr.get("status") or "active").lower()
        merged = raw_status == "completed"
        state = "open" if raw_status == "active" else "closed"
        merge_status = str(pr.get("mergeStatus") or "").lower()
        mergeable = merge_status == "succeeded" if state == "open" else False
        return {
            "number": int(pr.get("pullRequestId") or pull_number),
            "url": self._web_pr_url(int(pr.get("pullRequestId") or pull_number)),
            "state": state,
            "merged": merged,
            "draft": bool(pr.get("isDraft")),
            "mergeable": mergeable,
            "mergeable_state": merge_status,
            "head_ref": str(pr.get("sourceRefName") or "").removeprefix("refs/heads/"),
            "head_sha": str(((pr.get("lastMergeSourceCommit") or {}).get("commitId")) or ""),
            "head_repository": self.repository_full_name,
            "base_ref": str(pr.get("targetRefName") or "").removeprefix("refs/heads/"),
            "base_sha": str(((pr.get("lastMergeTargetCommit") or {}).get("commitId")) or ""),
            "base_repository": self.repository_full_name,
            "merge_commit_sha": str(((pr.get("lastMergeCommit") or {}).get("commitId")) or ""),
            "updated_at": str(pr.get("closedDate") or pr.get("creationDate") or ""),
        }

    def merge_pull_request(
        self, pull_request_number: int, commit_title: str
    ) -> PullRequestMergeResult:
        pr = self._request("GET", f"/pullrequests/{int(pull_request_number)}")
        source_commit = (pr.get("lastMergeSourceCommit") or {}).get("commitId") or ""
        strategy = _MERGE_STRATEGY.get(
            str(getattr(self.settings, "sandbox_merge_method", "squash") or "squash").lower(),
            "squash",
        )
        completed = self._request(
            "PATCH",
            f"/pullrequests/{int(pull_request_number)}",
            json_body={
                "status": "completed",
                "lastMergeSourceCommit": {"commitId": source_commit},
                "completionOptions": {
                    "mergeCommitMessage": commit_title,
                    "mergeStrategy": strategy,
                    "deleteSourceBranch": True,
                },
            },
        )
        sha = str(((completed.get("lastMergeCommit") or {}).get("commitId")) or "")
        return PullRequestMergeResult(
            sha=sha,
            merged=str(completed.get("status") or "").lower() == "completed",
            message=commit_title,
        )

    # --- comments & reviews ---------------------------------------------------
    def create_issue_comment(self, issue_number: int, body: str) -> IssueCommentResult:
        created = self._request(
            "POST",
            f"/pullrequests/{int(issue_number)}/threads",
            json_body={
                "comments": [{"parentCommentId": 0, "content": body, "commentType": 1}],
                "status": 1,
            },
        )
        thread_id = int(created.get("id") or 0)
        return IssueCommentResult(
            url=f"{self._web_pr_url(issue_number)}?discussionId={thread_id}",
            id=thread_id,
        )

    def create_review(
        self,
        pull_number: int,
        body: str,
        event: str = "COMMENT",
        comments: Optional[list[dict[str, Any]]] = None,
    ) -> PullRequestReviewResult:
        normalized = str(event or "COMMENT").upper()
        if normalized not in {"COMMENT", "APPROVE", "REQUEST_CHANGES"}:
            raise ValueError("event must be COMMENT, APPROVE, or REQUEST_CHANGES")
        main = self.create_issue_comment(pull_number, body)
        for inline in comments or []:
            text_body = str(inline.get("body") or "")
            if not text_body:
                continue
            payload: dict[str, Any] = {
                "comments": [{"parentCommentId": 0, "content": text_body, "commentType": 1}],
                "status": 1,
            }
            if inline.get("path"):
                line = int(inline.get("line") or 1)
                payload["threadContext"] = {
                    "filePath": "/" + str(inline["path"]).lstrip("/"),
                    "rightFileStart": {"line": line, "offset": 1},
                    "rightFileEnd": {"line": line, "offset": 1},
                }
            self._request("POST", f"/pullrequests/{int(pull_number)}/threads", json_body=payload)
        state = {
            "APPROVE": "APPROVED",
            "REQUEST_CHANGES": "CHANGES_REQUESTED",
        }.get(normalized, "COMMENTED")
        return PullRequestReviewResult(url=main.url, id=main.id, state=state)

    def get_pull_request_diff(self, pull_number: int) -> str:
        iterations = self._request("GET", f"/pullrequests/{int(pull_number)}/iterations")
        items = iterations.get("value") or []
        if not items:
            return ""
        latest_id = int(items[-1].get("id") or 1)
        changes = self._request(
            "GET",
            f"/pullrequests/{int(pull_number)}/iterations/{latest_id}/changes",
        )
        parts: list[str] = []
        for entry in changes.get("changeEntries") or []:
            item = entry.get("item") or {}
            path = str(item.get("path") or "unknown").lstrip("/")
            change_type = str(entry.get("changeType") or "edit")
            patch = str(entry.get("patch") or "")
            header = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path} ({change_type})"
            parts.append(f"{header}\n{patch}".rstrip() if patch else header)
        return "\n".join(parts)
