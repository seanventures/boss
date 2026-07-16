from __future__ import annotations

from dataclasses import asdict, dataclass
import time
from typing import Optional, Protocol

from ..config import Settings
from .github_adapter import (
    GitHubAdapter,
    PullRequestValidationCheck,
    PullRequestValidationResult,
)


@dataclass(frozen=True)
class PipelineDescriptor:
    provider: str
    repository: str
    configured: bool
    workflows: list[dict]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PipelineRunHandle:
    provider: str
    repository: str
    ref: str
    external_id: str
    workflow_id: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PipelineRunStatus:
    provider: str
    repository: str
    ref: str
    status: str
    completed: bool
    checks: list[PullRequestValidationCheck]


@dataclass(frozen=True)
class PipelineVerdict:
    provider: str
    repository: str
    ref: str
    status: str
    summary: str
    completed: bool
    checks: list[PullRequestValidationCheck]

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "repository": self.repository,
            "ref": self.ref,
            "status": self.status,
            "summary": self.summary,
            "completed": self.completed,
            "checks": [asdict(check) for check in self.checks],
        }

    def to_pull_request_validation(self) -> PullRequestValidationResult:
        return PullRequestValidationResult(
            status=self.status,
            summary=self.summary,
            head_sha=self.ref,
            completed=self.completed,
            checks=self.checks,
        )


class PipelineAdapter(Protocol):
    def discover(self, repo: str) -> PipelineDescriptor: ...

    def trigger(self, ref: str) -> PipelineRunHandle: ...

    def poll(self, handle: PipelineRunHandle) -> PipelineRunStatus: ...

    def verdict(self, handle: PipelineRunHandle) -> PipelineVerdict: ...


class GitHubActionsPipeline:
    """GitHub Actions implementation over the existing GitHub adapter seam.

    Triggering is deliberately a low-level adapter operation. Callers must put it
    behind BOSS policy, budget, and Receipt gates before using it as an effect.
    Read-only discover/poll/verdict operations are safe for merge and promotion
    evidence gathering.
    """

    provider = "github_actions"

    def __init__(self, settings: Settings, github: Optional[GitHubAdapter] = None):
        self.settings = settings
        self.github = github or GitHubAdapter(settings)

    @property
    def repository(self) -> str:
        return self.github.repository_full_name

    def _require_repository(self, repo: str) -> str:
        normalized = repo.strip()
        if normalized != self.repository:
            raise ValueError(
                f"Pipeline repository must be the configured target {self.repository}."
            )
        return normalized

    def discover(self, repo: str) -> PipelineDescriptor:
        repository = self._require_repository(repo)
        payload = self.github._request(
            "GET",
            f"/repos/{repository}/actions/workflows?per_page=100",
        )
        workflows = [
            {
                "id": str(item.get("id")),
                "name": str(item.get("name") or item.get("path") or "workflow"),
                "path": item.get("path"),
                "state": str(item.get("state") or "unknown"),
                "html_url": item.get("html_url"),
            }
            for item in payload.get("workflows", [])
            if str(item.get("state") or "active") == "active"
        ]
        return PipelineDescriptor(
            provider=self.provider,
            repository=repository,
            configured=bool(workflows),
            workflows=workflows,
        )

    def trigger(self, ref: str) -> PipelineRunHandle:
        normalized_ref = ref.strip()
        if not normalized_ref:
            raise ValueError("Pipeline ref is required.")
        descriptor = self.discover(self.repository)
        if not descriptor.configured:
            raise ValueError("No active GitHub Actions workflow is configured.")
        workflow_id = str(descriptor.workflows[0]["id"])
        self.github._request(
            "POST",
            f"/repos/{self.repository}/actions/workflows/{workflow_id}/dispatches",
            {"ref": normalized_ref},
            allow_empty=True,
        )
        return PipelineRunHandle(
            provider=self.provider,
            repository=self.repository,
            ref=normalized_ref,
            external_id=f"{workflow_id}:{normalized_ref}",
            workflow_id=workflow_id,
        )

    def _checks(self, ref: str) -> list[PullRequestValidationCheck]:
        payload = self.github._request(
            "GET",
            f"/repos/{self.repository}/commits/{ref}/check-runs",
        )
        return [
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

    def poll(self, handle: PipelineRunHandle) -> PipelineRunStatus:
        if handle.provider != self.provider or handle.repository != self.repository:
            raise ValueError("Pipeline run handle does not belong to this adapter.")
        checks = self._checks(handle.ref)
        evaluation = self.github._evaluate_check_runs(handle.ref, checks)
        return PipelineRunStatus(
            provider=self.provider,
            repository=self.repository,
            ref=handle.ref,
            status=evaluation.status,
            completed=evaluation.completed,
            checks=checks,
        )

    def verdict(self, handle: PipelineRunHandle) -> PipelineVerdict:
        run = self.poll(handle)
        evaluation = self.github._evaluate_check_runs(run.ref, run.checks)
        return PipelineVerdict(
            provider=self.provider,
            repository=self.repository,
            ref=run.ref,
            status=evaluation.status,
            summary=evaluation.summary,
            completed=evaluation.completed,
            checks=run.checks,
        )

    def wait_for_verdict(
        self,
        ref: str,
        *,
        timeout_seconds: int,
        poll_interval_seconds: int,
    ) -> PipelineVerdict:
        normalized_ref = ref.strip()
        if not normalized_ref:
            raise ValueError("Pipeline ref is required.")
        handle = PipelineRunHandle(
            provider=self.provider,
            repository=self.repository,
            ref=normalized_ref,
            external_id=f"checks:{normalized_ref}",
        )
        started = time.monotonic()
        while True:
            result = self.verdict(handle)
            if result.completed:
                return result
            if time.monotonic() - started >= timeout_seconds:
                return PipelineVerdict(
                    provider=self.provider,
                    repository=self.repository,
                    ref=normalized_ref,
                    status="timed_out",
                    summary="Timed out while waiting for pipeline validation to complete.",
                    completed=False,
                    checks=result.checks,
                )
            time.sleep(max(1, poll_interval_seconds))


class JenkinsPipeline:
    def __init__(self, *_args, **_kwargs):
        raise NotImplementedError(
            "Jenkins pipeline support is additive follow-on work (BOSS-63)."
        )


class GitLabPipeline:
    def __init__(self, *_args, **_kwargs):
        raise NotImplementedError(
            "GitLab pipeline support is additive follow-on work (BOSS-63)."
        )


def make_pipeline_adapter(
    settings: Settings,
    *,
    github: Optional[GitHubAdapter] = None,
) -> GitHubActionsPipeline:
    provider = settings.pipeline_provider.strip().lower()
    if provider == "github_actions":
        return GitHubActionsPipeline(settings, github=github)
    if provider == "jenkins":
        return JenkinsPipeline(settings)
    if provider == "gitlab":
        return GitLabPipeline(settings)
    raise ValueError(f"Unsupported pipeline provider: {settings.pipeline_provider}")
