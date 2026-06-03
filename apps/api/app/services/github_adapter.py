from __future__ import annotations

from dataclasses import dataclass
import base64
import time
from typing import Optional

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

    @property
    def repository_full_name(self) -> str:
        return f"{self.settings.target_github_owner}/{self.settings.target_github_repo}"

    def _headers(self) -> dict[str, str]:
        if not self.settings.github_token:
            raise IntegrationConfigurationError("BOSS_GITHUB_TOKEN is not configured.")
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.settings.github_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _request(
        self, method: str, path: str, json_body: Optional[dict] = None
    ) -> dict:
        response = httpx.request(
            method,
            f"https://api.github.com{path}",
            headers=self._headers(),
            json=json_body,
            timeout=30.0,
        )
        response.raise_for_status()
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
                "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
                "branch": branch_name,
                **({"sha": sha} if sha else {}),
            },
            timeout=30.0,
        )
        response.raise_for_status()
        data = response.json()
        return data["commit"]["sha"]

    def create_pull_request(
        self, branch_name: str, title: str, body: str
    ) -> PullRequestResult:
        default_branch, _ = self.default_branch_sha()
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
