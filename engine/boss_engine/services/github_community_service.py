from __future__ import annotations

from typing import Any, Callable, Optional

from sqlalchemy.engine import Connection

from ..config import Settings
from .github_adapter import GitHubAdapter
from .github_pr_review_agent import GitHubPullRequestReviewAgent
from .github_triage_agent import GitHubTriageAgent
from .jobs import JobLeaseService


class GitHubCommunityService:
    """Lease-validated worker boundary for community GitHub agent jobs."""

    def __init__(
        self,
        settings: Settings,
        adapter_factory: Optional[Callable[[Settings], GitHubAdapter]] = None,
    ) -> None:
        self.settings = settings
        self.jobs = JobLeaseService(settings)
        self._adapter_factory = adapter_factory
        self.triage = GitHubTriageAgent(settings, adapter_factory)
        self.pr_review = GitHubPullRequestReviewAgent(settings, adapter_factory)

    @property
    def adapter_factory(self) -> Optional[Callable[[Settings], GitHubAdapter]]:
        return self._adapter_factory

    @adapter_factory.setter
    def adapter_factory(
        self, value: Optional[Callable[[Settings], GitHubAdapter]]
    ) -> None:
        self._adapter_factory = value
        factory = value or GitHubAdapter
        self.triage.adapter_factory = factory
        self.pr_review.adapter_factory = factory

    def execute_job(
        self,
        connection: Connection,
        *,
        job_id: str,
        project_id: str,
        worker_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        job = self.jobs.require_active_lease(
            connection, worker_id, job_id, lease_token
        )
        if job["project_id"] != project_id:
            raise ValueError("Job does not belong to the requested project.")
        if job["kind"] not in {"github.triage", "github.pr_review"}:
            raise ValueError("Job is not a GitHub community agent job.")
        payload = job["payload"] or {}
        event_id = str(payload.get("event_id") or "")
        event_type = str(payload.get("event_type") or "")
        event_data = payload.get("event_data") or {}
        normalized = event_data.get("normalized") or {}
        if not event_id or not normalized:
            raise ValueError("GitHub community job is missing normalized event data.")
        if job["kind"] == "github.triage":
            return self.triage.execute(
                connection,
                project_id=project_id,
                event_id=event_id,
                normalized=normalized,
                job_id=job_id,
            )
        if event_type == "github.pr_review":
            # Human review submissions are durably recorded but do not trigger
            # recursive bot reviews. Open/reopen/synchronize events do.
            return {"status": "ignored", "reason": "human_review_event"}
        return self.pr_review.execute(
            connection,
            project_id=project_id,
            event_id=event_id,
            normalized=normalized,
            job_id=job_id,
        )
