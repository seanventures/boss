"""BOSSAPP-937 (Kiro gap 4) — GitLab code host adapter implementing the ``RepoHost`` seam.

Supports GitLab.com and self-hosted GitLab instances (``BOSS_GITLAB_BASE_URL``) behind the
same ``RepoHost`` interface used by GitHub, Bitbucket, and Local repositories:

* clone      — ``https://oauth2:{token}@{host}/{namespace}/{repo}.git``
* branches   — ``/projects/{id}/repository/branches`` (create off default branch; existing → no-op)
* commits    — ``/projects/{id}/repository/files/{path}`` (create-or-update with signed commit message)
* MRs        — ``/projects/{id}/merge_requests`` (deduped on open MR from the same source branch),
               ``/merge_requests/{iid}/merge`` (squash mapped from ``sandbox_merge_method``),
               ``/merge_requests/{iid}/changes`` (unified diff)
* reviews    — ``/merge_requests/{iid}/notes`` (comments), ``/merge_requests/{iid}/discussions``
               (inline review comments), ``/merge_requests/{iid}/approve``
* merge truth — ``pull_request_snapshot(iid)`` normalizes GitLab MR state/merge_status/sha
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


class GitLabAdapter:
    kind = "gitlab"

    def __init__(
        self,
        settings,
        *,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.settings = settings
        self.base_url = (
            base_url
            or getattr(settings, "gitlab_base_url", "")
            or "https://gitlab.com"
        ).rstrip("/")
        self.namespace = (
            owner
            or getattr(settings, "target_gitlab_namespace", "")
            or getattr(settings, "target_github_owner", "")
        )
        self.repo = (
            repo
            or getattr(settings, "target_gitlab_repo", "")
            or getattr(settings, "target_github_repo", "")
        )
        self.project_id = getattr(settings, "gitlab_project_id", "") or ""

    # --- identity -------------------------------------------------------------
    @property
    def repository_full_name(self) -> str:
        if self.namespace and self.repo:
            return f"{self.namespace}/{self.repo}"
        return self.project_id

    def _encoded_project(self) -> str:
        ref = self.project_id or self.repository_full_name
        return quote(ref, safe="")

    def clone_url(self, token: str = "") -> str:
        from .repo_host import clone_url_for

        return clone_url_for(
            "gitlab",
            self.namespace,
            self.repo,
            token or self._token(),
            web_base=self.base_url,
        )

    def _token(self) -> str:
        return getattr(self.settings, "gitlab_token", "") or ""

    # --- auth + wire ----------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        token = self._token()
        if not token:
            raise IntegrationConfigurationError(
                "No GitLab credentials configured (need BOSS_GITLAB_TOKEN)."
            )
        return {"Accept": "application/json", "PRIVATE-TOKEN": token}

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
        allow_empty: bool = False,
    ) -> Any:
        url = f"{self.base_url}/api/v4{path}"
        response = httpx.request(
            method,
            url,
            headers=self._headers(),
            json=json_body,
            params=params,
            timeout=30.0,
        )
        response.raise_for_status()
        if allow_empty and not response.content:
            return {}
        return response.json()

    def _proj_path(self) -> str:
        return f"/projects/{self._encoded_project()}"

    # --- branches -------------------------------------------------------------
    def default_branch(self) -> str:
        meta = self._request("GET", self._proj_path())
        return str(meta.get("default_branch") or "main")

    def ensure_branch(self, branch_name: str, base_branch: Optional[str] = None) -> str:
        """Create the branch off the base/default branch if absent; return head commit SHA."""
        encoded = quote(branch_name, safe="")
        try:
            existing = self._request("GET", f"{self._proj_path()}/repository/branches/{encoded}")
            return str(((existing.get("commit") or {}).get("id")) or "")
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 404:
                raise
        ref = base_branch or self.default_branch()
        created = self._request(
            "POST",
            f"{self._proj_path()}/repository/branches",
            json_body={"branch": branch_name, "ref": ref},
        )
        return str(((created.get("commit") or {}).get("id")) or "")

    # --- files / commits ------------------------------------------------------
    def upsert_file(self, branch_name: str, path: str, content: str, message: str) -> str:
        """Create or update a file on ``branch_name`` with a signed commit message."""
        from .agent_acts import sign_commit_message

        signed_message = sign_commit_message(message)
        encoded_path = quote(path.lstrip("/"), safe="")
        payload = {
            "branch": branch_name,
            "content": content,
            "commit_message": signed_message,
        }
        try:
            self._request(
                "GET",
                f"{self._proj_path()}/repository/files/{encoded_path}",
                params={"ref": branch_name},
            )
            method = "PUT"
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 404:
                raise
            method = "POST"
        self._request(
            method,
            f"{self._proj_path()}/repository/files/{encoded_path}",
            json_body=payload,
        )
        return path

    # --- merge requests (PRs) -------------------------------------------------
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
        if draft and not title.lower().startswith("draft:"):
            title = f"Draft: {title}"
        existing = self._request(
            "GET",
            f"{self._proj_path()}/merge_requests",
            params={"state": "opened", "source_branch": branch_name},
        )
        for mr in existing if isinstance(existing, list) else []:
            return PullRequestResult(
                url=str(mr.get("web_url") or ""),
                number=int(mr.get("iid") or 0),
            )
        target_branch = base or self.default_branch()
        created = self._request(
            "POST",
            f"{self._proj_path()}/merge_requests",
            json_body={
                "source_branch": branch_name,
                "target_branch": target_branch,
                "title": title,
                "description": body,
                "remove_source_branch": True,
            },
        )
        return PullRequestResult(
            url=str(created.get("web_url") or ""),
            number=int(created.get("iid") or 0),
        )

    def pull_request_snapshot(self, pull_number: int) -> dict[str, Any]:
        """Return canonical merge-truth snapshot for a GitLab MR."""
        mr = self._request("GET", f"{self._proj_path()}/merge_requests/{int(pull_number)}")
        raw_state = str(mr.get("state") or "opened").lower()
        merged = raw_state == "merged"
        state = "open" if raw_state == "opened" else "closed"
        merge_status = str(mr.get("detailed_merge_status") or mr.get("merge_status") or "").lower()
        mergeable = (
            merge_status in {"mergeable", "can_be_merged"}
            if state == "open"
            else False
        )
        diff_refs = mr.get("diff_refs") or {}
        return {
            "number": int(mr.get("iid") or pull_number),
            "url": str(mr.get("web_url") or ""),
            "state": state,
            "merged": merged,
            "draft": bool(mr.get("draft") or mr.get("work_in_progress")),
            "mergeable": mergeable,
            "mergeable_state": merge_status,
            "head_ref": str(mr.get("source_branch") or ""),
            "head_sha": str(mr.get("sha") or diff_refs.get("head_sha") or ""),
            "head_repository": self.repository_full_name,
            "base_ref": str(mr.get("target_branch") or ""),
            "base_sha": str(diff_refs.get("base_sha") or ""),
            "base_repository": self.repository_full_name,
            "merge_commit_sha": str(mr.get("merge_commit_sha") or mr.get("squash_commit_sha") or ""),
            "updated_at": str(mr.get("updated_at") or ""),
        }

    def merge_pull_request(
        self, pull_request_number: int, commit_title: str
    ) -> PullRequestMergeResult:
        method = str(getattr(self.settings, "sandbox_merge_method", "squash") or "squash").lower()
        body: dict[str, Any] = {
            "merge_commit_message": commit_title,
            "should_remove_source_branch": True,
        }
        if method == "squash":
            body["squash"] = True
            body["squash_commit_message"] = commit_title
        merged = self._request(
            "PUT",
            f"{self._proj_path()}/merge_requests/{int(pull_request_number)}/merge",
            json_body=body,
        )
        sha = str(
            merged.get("merge_commit_sha")
            or merged.get("squash_commit_sha")
            or merged.get("sha")
            or ""
        )
        return PullRequestMergeResult(
            sha=sha,
            merged=str(merged.get("state") or "").lower() == "merged",
            message=commit_title,
        )

    # --- comments & reviews ---------------------------------------------------
    def create_issue_comment(self, issue_number: int, body: str) -> IssueCommentResult:
        created = self._request(
            "POST",
            f"{self._proj_path()}/merge_requests/{int(issue_number)}/notes",
            json_body={"body": body},
        )
        note_id = int(created.get("id") or 0)
        return IssueCommentResult(
            url=f"{self.base_url}/{self.repository_full_name}/-/merge_requests/{issue_number}#note_{note_id}",
            id=note_id,
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
        note = self.create_issue_comment(pull_number, body)
        for inline in comments or []:
            path = str(inline.get("path") or "")
            line = inline.get("line")
            text_body = str(inline.get("body") or "")
            if not text_body:
                continue
            self._request(
                "POST",
                f"{self._proj_path()}/merge_requests/{int(pull_number)}/discussions",
                json_body={
                    "body": f"`{path}:{line}` — {text_body}" if path else text_body,
                },
            )
        if normalized == "APPROVE":
            self._request(
                "POST",
                f"{self._proj_path()}/merge_requests/{int(pull_number)}/approve",
                allow_empty=True,
            )
        state = {
            "APPROVE": "APPROVED",
            "REQUEST_CHANGES": "CHANGES_REQUESTED",
        }.get(normalized, "COMMENTED")
        return PullRequestReviewResult(url=note.url, id=note.id, state=state)

    def get_pull_request_diff(self, pull_number: int) -> str:
        data = self._request(
            "GET",
            f"{self._proj_path()}/merge_requests/{int(pull_number)}/changes",
        )
        changes = data.get("changes") or []
        parts: list[str] = []
        for change in changes:
            old_path = change.get("old_path") or change.get("new_path") or "unknown"
            new_path = change.get("new_path") or old_path
            diff_text = str(change.get("diff") or "")
            parts.append(f"diff --git a/{old_path} b/{new_path}\n{diff_text}".rstrip())
        return "\n".join(parts)
