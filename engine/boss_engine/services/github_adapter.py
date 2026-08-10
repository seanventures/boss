from __future__ import annotations

from dataclasses import dataclass
import base64
from typing import Any, Optional
from urllib.parse import quote

import httpx

from ..config import Settings


class IntegrationConfigurationError(RuntimeError):
    pass


class GitHubMergeConflictError(RuntimeError):
    """GitHub could not merge the requested immutable commits cleanly."""


class GitHubMergeRejectedError(RuntimeError):
    """GitHub rejected a guarded pull-request merge."""


@dataclass
class PullRequestResult:
    url: str
    number: int


@dataclass
class PullRequestMergeResult:
    sha: str
    merged: bool
    message: str


@dataclass
class IssueCommentResult:
    url: str
    id: int


@dataclass
class PullRequestReviewResult:
    url: str
    id: int
    state: str


@dataclass
class DeploymentResult:
    id: int
    environment: str
    sha: str
    url: str


@dataclass
class DeploymentStatusResult:
    state: str
    url: str
    environment_url: Optional[str]


@dataclass
class PullRequestValidationCheck:
    name: str
    status: str
    conclusion: Optional[str]
    html_url: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]


@dataclass
class PullRequestValidationResult:
    status: str
    summary: str
    head_sha: str
    completed: bool
    checks: list[PullRequestValidationCheck]


class GitHubAdapter:
    kind = "github"  # RepoHost conformance (BOSS-267) — the reference plugin

    def __init__(
        self,
        settings: Settings,
        *,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
        token: Optional[str] = None,
        api_base_url: str = "https://api.github.com",
    ):
        self.settings = settings
        self.owner = (owner or settings.target_github_owner).strip()
        self.repo = (repo or settings.target_github_repo).strip()
        self._token_override = token
        self.api_base_url = api_base_url.rstrip("/")
        self._app = None

    @property
    def repository_full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    def clone_url(self, token: str) -> str:
        from .repo_host import clone_url_for
        return clone_url_for("github", self.settings.target_github_owner,
                             self.settings.target_github_repo, token)

    def _auth_token(self) -> str:
        """Prefer a short-lived, repo-scoped GitHub-App installation token
        (Phase 2 developer impersonation); fall back to the PAT (Phase 0)."""
        from .github_app_adapter import (  # optional dependency
            GitHubAppAdapter, GitHubAppConfigurationError, repo_token,
        )

        if self._app is None:
            self._app = GitHubAppAdapter(self.settings, api_base_url=self.api_base_url)
        if self._token_override:
            return self._token_override
        try:
            # One shared decision: App token where the installation covers this repo, PAT
            # where it does not. Choosing here independently is how BOSS came to be unable to
            # open a PR against its own repository.
            return repo_token(self.settings, self.owner, self.repo,
                              api_base_url=self.api_base_url, app=self._app)
        except GitHubAppConfigurationError as error:
            raise IntegrationConfigurationError(str(error)) from error

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._auth_token()}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _request(
        self,
        method: str,
        path: str,
        json_body: Optional[dict] = None,
        *,
        accept: Optional[str] = None,
        response_text: bool = False,
        allow_empty: bool = False,
    ) -> Any:
        headers = self._headers()
        if accept:
            headers["Accept"] = accept
        response = httpx.request(
            method,
            f"{self.api_base_url}{path}",
            headers=headers,
            json=json_body,
            timeout=30.0,
        )
        response.raise_for_status()
        if response_text:
            return response.text
        if allow_empty and not response.content:
            return {}
        return response.json()

    def repo_metadata(self) -> dict:
        return self._request("GET", f"/repos/{self.repository_full_name}")

    def rate_limit_status(self) -> dict:
        """Return the current GitHub core API quota without exposing credentials."""
        payload = self._request("GET", "/rate_limit")
        core = (payload.get("resources") or {}).get("core") or payload.get("rate") or {}
        return {
            "resource": "core",
            "limit": max(0, int(core.get("limit") or 0)),
            "remaining": max(0, int(core.get("remaining") or 0)),
            "used": max(0, int(core.get("used") or 0)),
            "reset_epoch": max(0, int(core.get("reset") or 0)),
        }

    def branch_snapshot(self, branch_name: str) -> dict:
        """Read a branch head, tree, parents, and protection without mutation."""
        encoded_branch = quote(branch_name, safe="")
        try:
            ref = self._request(
                "GET",
                f"/repos/{self.repository_full_name}/git/ref/heads/{encoded_branch}",
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                return {
                    "ref": branch_name,
                    "exists": False,
                    "commit_sha": None,
                    "tree_sha": None,
                    "parents": [],
                    "protected": False,
                    "protection": {},
                }
            raise
        commit_sha = str((ref.get("object") or {}).get("sha") or "")
        if not commit_sha:
            raise RuntimeError(f"GitHub returned no commit SHA for {branch_name}.")
        commit = self._request(
            "GET",
            f"/repos/{self.repository_full_name}/git/commits/{quote(commit_sha, safe='')}",
        )
        try:
            branch = self._request(
                "GET",
                f"/repos/{self.repository_full_name}/branches/{encoded_branch}",
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 404:
                raise
            branch = {}
        protection = branch.get("protection") or {}
        return {
            "ref": branch_name,
            "exists": True,
            "commit_sha": commit_sha,
            "tree_sha": str((commit.get("tree") or {}).get("sha") or ""),
            "parents": [
                str(parent.get("sha"))
                for parent in (commit.get("parents") or [])
                if parent.get("sha")
            ],
            "protected": bool(branch.get("protected", False)),
            "protection": {
                "enabled": bool(branch.get("protected", False)),
                "required_status_checks": protection.get("required_status_checks"),
                "required_pull_request_reviews": protection.get(
                    "required_pull_request_reviews"
                ),
                "required_signatures": protection.get("required_signatures"),
                "enforce_admins": protection.get("enforce_admins"),
            },
        }

    def compare_commits(self, base_sha: str, head_sha: str) -> dict:
        """Compare immutable commit identities and return bounded topology data."""
        payload = self._request(
            "GET",
            (
                f"/repos/{self.repository_full_name}/compare/"
                f"{quote(base_sha, safe='')}...{quote(head_sha, safe='')}"
            ),
        )
        merge_base = payload.get("merge_base_commit") or {}
        return {
            "status": str(payload.get("status") or ""),
            "ahead_by": max(0, int(payload.get("ahead_by") or 0)),
            "behind_by": max(0, int(payload.get("behind_by") or 0)),
            "total_commits": max(0, int(payload.get("total_commits") or 0)),
            "merge_base_commit_sha": merge_base.get("sha"),
            "merge_base_tree_sha": (merge_base.get("commit") or {})
            .get("tree", {})
            .get("sha"),
        }

    def commit_identity(self, commit_sha: str) -> dict:
        """Return the commit and tree identity for an immutable Git object."""
        payload = self._request(
            "GET",
            (
                f"/repos/{self.repository_full_name}/git/commits/"
                f"{quote(commit_sha, safe='')}"
            ),
        )
        return {
            "commit_sha": str(payload.get("sha") or commit_sha),
            "tree_sha": str((payload.get("tree") or {}).get("sha") or ""),
        }

    def compare_changed_files(
        self,
        base_sha: str,
        head_sha: str,
        *,
        limit: int = 300,
    ) -> list[dict]:
        """Return bounded, reviewable file evidence for two immutable commits."""
        if limit < 1 or limit > 300:
            raise ValueError("limit must be between 1 and 300.")
        payload = self._request(
            "GET",
            (
                f"/repos/{self.repository_full_name}/compare/"
                f"{quote(base_sha, safe='')}...{quote(head_sha, safe='')}"
            ),
        )
        files = []
        for item in (payload.get("files") or [])[:limit]:
            patch = item.get("patch")
            files.append(
                {
                    "filename": str(item.get("filename") or ""),
                    "previous_filename": item.get("previous_filename"),
                    "status": str(item.get("status") or "modified"),
                    "additions": max(0, int(item.get("additions") or 0)),
                    "deletions": max(0, int(item.get("deletions") or 0)),
                    "changes": max(0, int(item.get("changes") or 0)),
                    "patch": (
                        str(patch)[:20000]
                        if isinstance(patch, str)
                        else None
                    ),
                }
            )
        return files

    def create_branch_ref(self, branch_name: str, commit_sha: str) -> dict:
        """Create a new branch at an immutable commit; never overwrites a ref."""
        response = self._request(
            "POST",
            f"/repos/{self.repository_full_name}/git/refs",
            {"ref": f"refs/heads/{branch_name}", "sha": commit_sha},
        )
        return {
            "ref": branch_name,
            "commit_sha": str((response.get("object") or {}).get("sha") or ""),
        }

    def fast_forward_ref(
        self,
        branch_name: str,
        *,
        expected_sha: str,
        new_sha: str,
    ) -> dict:
        """Update a branch without force after checking its expected head."""
        current = self.branch_snapshot(branch_name)
        if not current["exists"]:
            raise RuntimeError("GitHub branch no longer exists.")
        if current["commit_sha"] != expected_sha:
            raise RuntimeError("GitHub branch head changed before update.")
        encoded_branch = quote(branch_name, safe="")
        response = self._request(
            "PATCH",
            (
                f"/repos/{self.repository_full_name}/git/refs/heads/"
                f"{encoded_branch}"
            ),
            {"sha": new_sha, "force": False},
        )
        result_sha = str((response.get("object") or {}).get("sha") or "")
        if result_sha != new_sha:
            raise RuntimeError("GitHub returned an unexpected branch head.")
        return {
            "ref": branch_name,
            "previous_sha": expected_sha,
            "commit_sha": result_sha,
            "forced": False,
        }

    def merge_branch(
        self,
        *,
        base_branch: str,
        head_sha: str,
        message: str,
    ) -> dict:
        """Attempt a provider-side merge into an unprotected resolution branch."""
        try:
            response = self._request(
                "POST",
                f"/repos/{self.repository_full_name}/merges",
                {
                    "base": base_branch,
                    "head": head_sha,
                    "commit_message": message,
                },
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 409:
                raise GitHubMergeConflictError(
                    "GitHub reported merge conflicts."
                ) from error
            raise
        commit = response.get("commit") or response
        return {
            "base": base_branch,
            "head": head_sha,
            "commit_sha": str(response.get("sha") or commit.get("sha") or ""),
            "tree_sha": str(
                ((commit.get("tree") or {}).get("sha"))
                or ((commit.get("commit") or {}).get("tree") or {}).get("sha")
                or ""
            ),
        }

    def create_resolution_commit(
        self,
        *,
        base_tree_sha: str,
        parent_shas: list[str],
        resolved_files: list[dict],
        message: str,
    ) -> dict:
        """Create one explicit reviewed resolution commit from complete file bodies."""
        if len(parent_shas) != 2 or any(not str(value).strip() for value in parent_shas):
            raise ValueError("A resolution commit requires exactly two parent SHAs.")
        if not resolved_files:
            raise ValueError("resolved_files must contain at least one file.")
        tree_entries = []
        for item in resolved_files:
            path = str(item.get("path") or "").strip()
            content = item.get("content")
            mode = str(item.get("mode") or "100644")
            if not path or path.startswith("/") or ".." in path.split("/"):
                raise ValueError("Resolved file paths must be repository-relative.")
            if not isinstance(content, str):
                raise ValueError("Resolved file content must be UTF-8 text.")
            if mode not in {"100644", "100755"}:
                raise ValueError("Resolved file mode must be 100644 or 100755.")
            blob = self._request(
                "POST",
                f"/repos/{self.repository_full_name}/git/blobs",
                {
                    "content": base64.b64encode(content.encode("utf-8")).decode(
                        "ascii"
                    ),
                    "encoding": "base64",
                },
            )
            tree_entries.append(
                {
                    "path": path,
                    "mode": mode,
                    "type": "blob",
                    "sha": str(blob["sha"]),
                }
            )
        tree = self._request(
            "POST",
            f"/repos/{self.repository_full_name}/git/trees",
            {"base_tree": base_tree_sha, "tree": tree_entries},
        )
        commit = self._request(
            "POST",
            f"/repos/{self.repository_full_name}/git/commits",
            {
                "message": message,
                "tree": str(tree["sha"]),
                "parents": parent_shas,
            },
        )
        return {
            "commit_sha": str(commit["sha"]),
            "tree_sha": str((commit.get("tree") or {}).get("sha") or tree["sha"]),
            "parents": parent_shas,
            "files": [entry["path"] for entry in tree_entries],
        }

    def commit_checks(self, commit_sha: str) -> list[dict]:
        """Return bounded check-run state for an immutable commit."""
        payload = self._request(
            "GET",
            (
                f"/repos/{self.repository_full_name}/commits/"
                f"{quote(commit_sha, safe='')}/check-runs?per_page=100"
            ),
            accept="application/vnd.github+json",
        )
        return [
            {
                "name": check.get("name"),
                "status": check.get("status"),
                "conclusion": check.get("conclusion"),
                "app": (check.get("app") or {}).get("slug"),
            }
            for check in (payload.get("check_runs") or [])
        ]

    def default_branch_sha(self) -> tuple[str, str]:
        repo = self.repo_metadata()
        default_branch = repo["default_branch"]
        ref = self._request(
            "GET", f"/repos/{self.repository_full_name}/git/ref/heads/{default_branch}"
        )
        return default_branch, ref["object"]["sha"]

    def branch_exists(self, branch_name: str) -> bool:
        """True if the branch already exists on the remote (used to clone prior work
        when a feedback-iteration reuses an existing task branch)."""
        try:
            self._request(
                "GET", f"/repos/{self.repository_full_name}/git/ref/heads/{branch_name}"
            )
            return True
        except httpx.HTTPStatusError:
            return False

    def ensure_branch(self, branch_name: str) -> str:
        try:
            ref = self._request(
                "GET", f"/repos/{self.repository_full_name}/git/ref/heads/{branch_name}"
            )
            return ref["object"]["sha"]
        except httpx.HTTPStatusError:
            _, base_sha = self.default_branch_sha()
            ref = self._request(
                "POST",
                f"/repos/{self.repository_full_name}/git/refs",
                {"ref": f"refs/heads/{branch_name}", "sha": base_sha},
            )
            return ref["object"]["sha"]

    def upsert_file(self, branch_name: str, path: str, content: str, message: str) -> str:
        """Create or update a UTF-8 text file on a branch."""
        return self.upsert_binary_file(
            branch_name, path, content.encode("utf-8"), message
        )

    def read_text_file(self, branch_name: str, path: str) -> Optional[str]:
        """Read a UTF-8 repository file from a branch, returning ``None`` on 404."""
        try:
            payload = self._request(
                "GET",
                f"/repos/{self.repository_full_name}/contents/{path}?ref={branch_name}",
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                return None
            raise
        encoded = payload.get("content")
        if not isinstance(encoded, str):
            raise RuntimeError(f"GitHub did not return file content for {path}.")
        return base64.b64decode(encoded).decode("utf-8")

    def upsert_binary_file(
        self, branch_name: str, path: str, data: bytes, message: str
    ) -> str:
        """Create or update a file of arbitrary bytes (e.g. a PDF) on a branch."""
        sha = None
        try:
            existing = self._request(
                "GET",
                f"/repos/{self.repository_full_name}/contents/{path}?ref={branch_name}",
            )
            sha = existing["sha"]
        except httpx.HTTPStatusError:
            sha = None

        response = httpx.put(
            f"{self.api_base_url}/repos/{self.repository_full_name}/contents/{path}",
            headers=self._headers(),
            json={
                "message": message,
                "content": base64.b64encode(data).decode("ascii"),
                "branch": branch_name,
                **({"sha": sha} if sha else {}),
            },
            timeout=30.0,
        )
        response.raise_for_status()
        data_json = response.json()
        return data_json["commit"]["sha"]

    def create_pull_request(
        self, branch_name: str, title: str, body: str
    ) -> PullRequestResult:
        default_branch, _ = self.default_branch_sha()
        # Idempotent for feedback-iterations that reuse a branch: if a PR is already open
        # for this head, refresh its body and return it — GitHub would otherwise 422 on a
        # duplicate head. Lets the same task iterate on one branch + one PR.
        owner = self.repository_full_name.split("/")[0]
        try:
            existing = self._request(
                "GET",
                f"/repos/{self.repository_full_name}/pulls?state=open&head={owner}:{branch_name}",
            )
        except httpx.HTTPStatusError:
            existing = []
        if existing:
            pr = existing[0]
            try:
                self._request(
                    "PATCH",
                    f"/repos/{self.repository_full_name}/pulls/{pr['number']}",
                    {"body": body},
                )
            except httpx.HTTPStatusError:
                pass
            return PullRequestResult(url=pr["html_url"], number=pr["number"])
        response = self._request(
            "POST",
            f"/repos/{self.repository_full_name}/pulls",
            {
                "title": title,
                "head": branch_name,
                "base": default_branch,
                "body": body,
                "draft": self.settings.open_pr_as_draft,
                "maintainer_can_modify": False,
            },
        )
        return PullRequestResult(url=response["html_url"], number=response["number"])

    def update_pull_request_branch(self, pull_request_number: int) -> bool:
        """Merge the base branch INTO the PR's head branch (GitHub's "Update branch").

        This is the cheap half of conflict recovery. When several PRs branch from the same
        base and one merges, the others go CONFLICTING and GitHub answers the merge endpoint
        with 405 — even when the drift is trivial and git could reconcile it unattended.
        Measured: merging PR #66 immediately made #67 and #68 unmergeable, and BOSS recorded
        `pull_request_merge_failed` and stopped, so only the first of any batch could ever land.

        Returns True when GitHub accepted the update (the branch now contains the base), False
        when it refused — which means a REAL conflict that needs an agent, not a retry.
        """
        try:
            self._request(
                "PUT",
                f"/repos/{self.repository_full_name}/pulls/{pull_request_number}/update-branch",
                {},
            )
            return True
        except Exception:  # noqa: BLE001 — a refusal is information, not an error to raise
            return False

    def merge_pull_request(
        self, pull_request_number: int, commit_title: str
    ) -> PullRequestMergeResult:
        response = self._request(
            "PUT",
            f"/repos/{self.repository_full_name}/pulls/{pull_request_number}/merge",
            {
                "commit_title": commit_title,
                "merge_method": self.settings.sandbox_merge_method,
            },
        )
        return PullRequestMergeResult(
            sha=response["sha"],
            merged=bool(response["merged"]),
            message=response["message"],
        )

    def pull_request_snapshot(self, pull_number: int) -> dict:
        """Return the immutable identities and merge posture for one pull request."""
        response = self._request(
            "GET",
            f"/repos/{self.repository_full_name}/pulls/{pull_number}",
        )
        head = response.get("head") or {}
        base = response.get("base") or {}
        return {
            "number": int(response["number"]),
            "url": str(response.get("html_url") or ""),
            "state": str(response.get("state") or ""),
            "draft": bool(response.get("draft", False)),
            "mergeable": response.get("mergeable"),
            "mergeable_state": str(response.get("mergeable_state") or ""),
            "head_ref": str(head.get("ref") or ""),
            "head_sha": str(head.get("sha") or ""),
            "head_repository": str(
                ((head.get("repo") or {}).get("full_name")) or ""
            ),
            "base_ref": str(base.get("ref") or ""),
            "base_sha": str(base.get("sha") or ""),
            "base_repository": str(
                ((base.get("repo") or {}).get("full_name")) or ""
            ),
            "updated_at": response.get("updated_at"),
        }

    def pull_request_reviews(self, pull_number: int) -> list[dict]:
        """Return bounded review evidence used by merge authorization."""
        response = self._request(
            "GET",
            (
                f"/repos/{self.repository_full_name}/pulls/{pull_number}/reviews"
                "?per_page=100"
            ),
        )
        return [
            {
                "id": int(review["id"]),
                "reviewer": str((review.get("user") or {}).get("login") or ""),
                "state": str(review.get("state") or "").upper(),
                "commit_sha": str(review.get("commit_id") or ""),
                "submitted_at": review.get("submitted_at"),
            }
            for review in response[:100]
            if review.get("id") is not None
        ]

    def merge_pull_request_guarded(
        self,
        *,
        pull_number: int,
        expected_head_sha: str,
        expected_base_sha: str,
        expected_head_ref: str,
        expected_base_ref: str,
        merge_method: str,
        commit_title: str,
    ) -> dict:
        """Merge only if GitHub still exposes the exact authorized PR snapshot."""
        current = self.pull_request_snapshot(pull_number)
        expected = {
            "head_sha": expected_head_sha,
            "base_sha": expected_base_sha,
            "head_ref": expected_head_ref,
            "base_ref": expected_base_ref,
        }
        if current["state"] != "open":
            raise GitHubMergeRejectedError("Pull request is not open.")
        if current["draft"]:
            raise GitHubMergeRejectedError("Draft pull requests cannot be merged.")
        for field, value in expected.items():
            if current[field] != value:
                raise GitHubMergeRejectedError(
                    "Pull request snapshot changed after authorization."
                )
        response = self._request(
            "PUT",
            f"/repos/{self.repository_full_name}/pulls/{pull_number}/merge",
            {
                "sha": expected_head_sha,
                "commit_title": commit_title,
                "merge_method": merge_method,
            },
        )
        if not bool(response.get("merged")):
            raise GitHubMergeRejectedError(
                str(response.get("message") or "GitHub rejected the merge.")
            )
        return {
            "pull_request_number": pull_number,
            "commit_sha": str(response.get("sha") or ""),
            "merged": True,
            "message": str(response.get("message") or ""),
            "merge_method": merge_method,
            "previous_target_sha": expected_base_sha,
            "authorized_head_sha": expected_head_sha,
        }

    def create_issue_comment(
        self, issue_number: int, body: str
    ) -> IssueCommentResult:
        response = self._request(
            "POST",
            f"/repos/{self.repository_full_name}/issues/{issue_number}/comments",
            {"body": body},
        )
        return IssueCommentResult(url=response["html_url"], id=int(response["id"]))

    def create_review(
        self,
        pull_number: int,
        body: str,
        event: str = "COMMENT",
        comments: Optional[list[dict]] = None,
    ) -> PullRequestReviewResult:
        normalized_event = event.upper()
        if normalized_event not in {"COMMENT", "APPROVE", "REQUEST_CHANGES"}:
            raise ValueError(
                "event must be COMMENT, APPROVE, or REQUEST_CHANGES"
            )
        payload: dict = {"body": body, "event": normalized_event}
        if comments:
            payload["comments"] = comments
        response = self._request(
            "POST",
            f"/repos/{self.repository_full_name}/pulls/{pull_number}/reviews",
            payload,
        )
        return PullRequestReviewResult(
            url=response["html_url"],
            id=int(response["id"]),
            state=response["state"],
        )

    def get_pull_request_diff(self, pull_number: int) -> str:
        return self._request(
            "GET",
            f"/repos/{self.repository_full_name}/pulls/{pull_number}",
            accept="application/vnd.github.v3.diff",
            response_text=True,
        )

    def create_deployment(
        self, ref: str, environment: str, description: str
    ) -> DeploymentResult:
        response = self._request(
            "POST",
            f"/repos/{self.repository_full_name}/deployments",
            {
                "ref": ref,
                "environment": environment,
                "description": description,
                "auto_merge": False,
                "required_contexts": [],
                "production_environment": False,
                "transient_environment": True,
            },
        )
        return DeploymentResult(
            id=int(response["id"]),
            environment=response["environment"],
            sha=response["sha"],
            url=response["url"],
        )

    def mark_deployment_success(
        self,
        deployment_id: int,
        environment: str,
        *,
        environment_url: Optional[str] = None,
        description: Optional[str] = None,
    ) -> DeploymentStatusResult:
        response = self._request(
            "POST",
            f"/repos/{self.repository_full_name}/deployments/{deployment_id}/statuses",
            {
                "state": "success",
                "environment": environment,
                **(
                    {"environment_url": environment_url}
                    if environment_url
                    else {}
                ),
                **({"description": description} if description else {}),
            },
        )
        return DeploymentStatusResult(
            state=response["state"],
            url=response["url"],
            environment_url=response.get("environment_url"),
        )

    def mark_deployment_failure(
        self,
        deployment_id: int,
        environment: str,
        *,
        environment_url: Optional[str] = None,
        description: Optional[str] = None,
    ) -> DeploymentStatusResult:
        response = self._request(
            "POST",
            f"/repos/{self.repository_full_name}/deployments/{deployment_id}/statuses",
            {
                "state": "failure",
                "environment": environment,
                **(
                    {"environment_url": environment_url}
                    if environment_url
                    else {}
                ),
                **({"description": description} if description else {}),
            },
        )
        return DeploymentStatusResult(
            state=response["state"],
            url=response["url"],
            environment_url=response.get("environment_url"),
        )

    def wait_for_commit_checks(
        self,
        head_sha: str,
        *,
        timeout_seconds: int,
        poll_interval_seconds: int,
    ) -> PullRequestValidationResult:
        from .pipeline_adapter import GitHubActionsPipeline

        return GitHubActionsPipeline(
            self.settings,
            github=self,
        ).wait_for_verdict(
            head_sha,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        ).to_pull_request_validation()

    def _evaluate_check_runs(
        self, head_sha: str, check_runs: list[PullRequestValidationCheck]
    ) -> PullRequestValidationResult:
        if not check_runs:
            return PullRequestValidationResult(
                status="pending",
                summary="GitHub has not published validation checks for this commit yet.",
                head_sha=head_sha,
                completed=False,
                checks=[],
            )

        if any(check.status != "completed" for check in check_runs):
            return PullRequestValidationResult(
                status="pending",
                summary="GitHub validation checks are still running for this proposal.",
                head_sha=head_sha,
                completed=False,
                checks=check_runs,
            )

        failing = [
            check
            for check in check_runs
            if (check.conclusion or "").lower()
            not in {"success", "neutral", "skipped"}
        ]
        if failing:
            failed_names = ", ".join(check.name for check in failing[:3])
            if len(failing) > 3:
                failed_names = f"{failed_names}, and {len(failing) - 3} more"
            return PullRequestValidationResult(
                status="failed",
                summary=f"GitHub validation failed: {failed_names}.",
                head_sha=head_sha,
                completed=True,
                checks=check_runs,
            )

        passed_names = ", ".join(check.name for check in check_runs[:3])
        if len(check_runs) > 3:
            passed_names = f"{passed_names}, and {len(check_runs) - 3} more"
        return PullRequestValidationResult(
            status="passed",
            summary=f"GitHub validation passed: {passed_names}.",
            head_sha=head_sha,
            completed=True,
            checks=check_runs,
        )
