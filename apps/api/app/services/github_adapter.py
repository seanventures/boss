from __future__ import annotations

from dataclasses import dataclass
import base64
import time
from typing import Any, Optional

import httpx

from ..config import Settings


class IntegrationConfigurationError(RuntimeError):
    pass


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
    def __init__(self, settings: Settings):
        self.settings = settings
        self._app = None

    @property
    def repository_full_name(self) -> str:
        return f"{self.settings.target_github_owner}/{self.settings.target_github_repo}"

    def _auth_token(self) -> str:
        """Prefer a short-lived, repo-scoped GitHub-App installation token
        (Phase 2 developer impersonation); fall back to the PAT (Phase 0)."""
        from .github_app_adapter import GitHubAppAdapter  # optional dependency

        if self._app is None:
            self._app = GitHubAppAdapter(self.settings)
        if self._app.configured:
            return self._app.token_for_repo(
                self.settings.target_github_owner, self.settings.target_github_repo
            )
        if not self.settings.github_token:
            raise IntegrationConfigurationError(
                "No GitHub credentials configured (need a GitHub App installation "
                "or BOSS_GITHUB_TOKEN)."
            )
        return self.settings.github_token

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
    ) -> Any:
        headers = self._headers()
        if accept:
            headers["Accept"] = accept
        response = httpx.request(
            method,
            f"https://api.github.com{path}",
            headers=headers,
            json=json_body,
            timeout=30.0,
        )
        response.raise_for_status()
        if response_text:
            return response.text
        return response.json()

    def repo_metadata(self) -> dict:
        return self._request("GET", f"/repos/{self.repository_full_name}")

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
            f"https://api.github.com/repos/{self.repository_full_name}/contents/{path}",
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
                "draft": True,
                "maintainer_can_modify": False,
            },
        )
        return PullRequestResult(url=response["html_url"], number=response["number"])

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
        start = time.monotonic()
        while True:
            payload = self._request(
                "GET",
                f"/repos/{self.repository_full_name}/commits/{head_sha}/check-runs",
            )
            check_runs = [
                PullRequestValidationCheck(
                    name=str(item.get("name") or "Unnamed check"),
                    status=str(item.get("status") or "unknown"),
                    conclusion=item.get("conclusion"),
                    html_url=item.get("html_url"),
                    started_at=item.get("started_at"),
                    completed_at=item.get("completed_at"),
                )
                for item in payload.get("check_runs", [])
            ]
            evaluation = self._evaluate_check_runs(head_sha, check_runs)
            if evaluation.completed:
                return evaluation

            elapsed = time.monotonic() - start
            if elapsed >= timeout_seconds:
                return PullRequestValidationResult(
                    status="timed_out",
                    summary=(
                        "Timed out while waiting for GitHub validation checks to "
                        "complete."
                    ),
                    head_sha=head_sha,
                    completed=False,
                    checks=check_runs,
                )

            time.sleep(max(1, poll_interval_seconds))

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
