from __future__ import annotations

from dataclasses import asdict, dataclass
import time
from typing import Optional, Protocol
from urllib.parse import quote, urlencode

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
    provider: str

    def discover(self, repo: str) -> PipelineDescriptor: ...

    def trigger(self, ref: str) -> PipelineRunHandle: ...

    def poll(self, handle: PipelineRunHandle) -> PipelineRunStatus: ...

    def verdict(self, handle: PipelineRunHandle) -> PipelineVerdict: ...


def _trigger_and_wait(
    adapter: PipelineAdapter, ref: str, *, timeout_seconds: int,
    poll_interval_seconds: int,
) -> PipelineVerdict:
    handle = adapter.trigger(ref)
    started = time.monotonic()
    while True:
        result = adapter.verdict(handle)
        if result.completed:
            return result
        if time.monotonic() - started >= timeout_seconds:
            return PipelineVerdict(
                adapter.provider, handle.repository, handle.ref, "timed_out",
                f"Timed out while waiting for {adapter.provider} pipeline.", False,
                result.checks,
            )
        time.sleep(max(1, poll_interval_seconds))


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
    """Jenkins job adapter. Triggering remains an effect for a gated caller."""

    provider = "jenkins"

    def __init__(self, settings: Settings, client=None):
        self.settings = settings
        self.base_url = settings.jenkins_base_url.rstrip("/")
        self.job = settings.jenkins_job.strip("/")
        self.repository = f"{settings.target_github_owner}/{settings.target_github_repo}"
        self._client = client

    @property
    def _job_path(self) -> str:
        return "/".join(f"job/{quote(part, safe='')}" for part in self.job.split("/") if part)

    def _request(self, method: str, path_or_url: str, **kwargs):
        if not self.base_url or not self.job:
            raise ValueError("Jenkins requires BOSS_JENKINS_BASE_URL and BOSS_JENKINS_JOB.")
        import httpx
        url = path_or_url if path_or_url.startswith(("http://", "https://")) else self.base_url + path_or_url
        auth = ((self.settings.jenkins_user, self.settings.jenkins_token)
                if self.settings.jenkins_user or self.settings.jenkins_token else None)
        if self._client is not None:
            response = self._client.request(method, url, auth=auth, **kwargs)
        else:
            with httpx.Client(timeout=30.0, auth=auth) as client:
                response = client.request(method, url, **kwargs)
        response.raise_for_status()
        return response

    def discover(self, repo: str) -> PipelineDescriptor:
        if repo.strip() != self.repository:
            raise ValueError(f"Pipeline repository must be {self.repository}.")
        if not self.base_url or not self.job:
            return PipelineDescriptor(self.provider, repo, False, [])
        response = self._request("GET", f"/{self._job_path}/api/json")
        data = response.json()
        return PipelineDescriptor(
            self.provider, repo, True,
            [{"id": self.job, "name": data.get("displayName") or self.job,
              "state": "disabled" if data.get("buildable") is False else "active",
              "url": data.get("url")}],
        )

    def trigger(self, ref: str) -> PipelineRunHandle:
        ref = ref.strip()
        if not ref:
            raise ValueError("Pipeline ref is required.")
        headers: dict[str, str] = {}
        try:
            crumb = self._request("GET", "/crumbIssuer/api/json").json()
            if crumb.get("crumbRequestField") and crumb.get("crumb"):
                headers[str(crumb["crumbRequestField"])] = str(crumb["crumb"])
        except Exception:  # Jenkins may have CSRF crumbs disabled
            pass
        response = self._request(
            "POST", f"/{self._job_path}/buildWithParameters?{urlencode({'REF': ref})}",
            headers=headers,
        )
        queue_url = response.headers.get("Location")
        if not queue_url:
            raise RuntimeError("Jenkins trigger did not return a queue Location header.")
        return PipelineRunHandle(
            self.provider, self.repository, ref, queue_url, workflow_id=self.job
        )

    def _build_payload(self, handle: PipelineRunHandle) -> tuple[dict, str]:
        queue = self._request("GET", handle.external_id.rstrip("/") + "/api/json").json()
        if queue.get("cancelled"):
            return {"building": False, "result": "ABORTED", "url": handle.external_id}, "queue"
        executable = queue.get("executable") or {}
        if not executable.get("url"):
            return {"building": True, "result": None, "url": handle.external_id}, "queue"
        build_url = str(executable["url"]).rstrip("/")
        return self._request("GET", build_url + "/api/json").json(), build_url

    def poll(self, handle: PipelineRunHandle) -> PipelineRunStatus:
        if handle.provider != self.provider or handle.repository != self.repository:
            raise ValueError("Pipeline run handle does not belong to this adapter.")
        payload, url = self._build_payload(handle)
        building = bool(payload.get("building"))
        result = str(payload.get("result") or "").upper()
        passed = result == "SUCCESS"
        completed = not building and bool(result)
        status = "passed" if passed else "failed" if completed else "pending"
        check = PullRequestValidationCheck(
            name=f"Jenkins · {self.job}", status="completed" if completed else "in_progress",
            conclusion="success" if passed else "failure" if completed else None,
            html_url=payload.get("url") or url,
            started_at=payload.get("timestamp"), completed_at=None,
        )
        return PipelineRunStatus(self.provider, self.repository, handle.ref, status, completed, [check])

    def verdict(self, handle: PipelineRunHandle) -> PipelineVerdict:
        run = self.poll(handle)
        summary = (
            "Jenkins pipeline passed." if run.status == "passed" else
            "Jenkins pipeline failed." if run.completed else "Jenkins pipeline is still running."
        )
        return PipelineVerdict(
            run.provider, run.repository, run.ref, run.status, summary, run.completed, run.checks
        )

    def wait_for_verdict(self, ref: str, *, timeout_seconds: int, poll_interval_seconds: int):
        return _trigger_and_wait(
            self, ref, timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )


class GitLabPipeline:
    """GitLab Pipelines API adapter with terminal-state normalization."""

    provider = "gitlab"
    _TERMINAL = {"success", "failed", "canceled", "skipped", "manual"}

    def __init__(self, settings: Settings, client=None):
        self.settings = settings
        self.base_url = settings.gitlab_base_url.rstrip("/")
        self.project = settings.gitlab_project_id.strip()
        self.repository = f"{settings.target_github_owner}/{settings.target_github_repo}"
        self._client = client

    def _request(self, method: str, path: str, **kwargs):
        if not self.project or not self.settings.gitlab_token:
            raise ValueError("GitLab requires BOSS_GITLAB_PROJECT_ID and BOSS_GITLAB_TOKEN.")
        import httpx
        headers = {"PRIVATE-TOKEN": self.settings.gitlab_token, **kwargs.pop("headers", {})}
        url = f"{self.base_url}/api/v4{path}"
        if self._client is not None:
            response = self._client.request(method, url, headers=headers, **kwargs)
        else:
            with httpx.Client(timeout=30.0) as client:
                response = client.request(method, url, headers=headers, **kwargs)
        response.raise_for_status()
        return response

    @property
    def _project_path(self) -> str:
        return quote(self.project, safe="")

    def discover(self, repo: str) -> PipelineDescriptor:
        if repo.strip() != self.repository:
            raise ValueError(f"Pipeline repository must be {self.repository}.")
        if not self.project or not self.settings.gitlab_token:
            return PipelineDescriptor(self.provider, repo, False, [])
        response = self._request("GET", f"/projects/{self._project_path}/pipelines?per_page=1")
        pipelines = response.json()
        return PipelineDescriptor(
            self.provider, repo, True,
            [{"id": str(item.get("id")), "name": "GitLab CI", "state": item.get("status"),
              "url": item.get("web_url")} for item in pipelines],
        )

    def trigger(self, ref: str) -> PipelineRunHandle:
        ref = ref.strip()
        if not ref:
            raise ValueError("Pipeline ref is required.")
        payload = self._request(
            "POST", f"/projects/{self._project_path}/pipeline", data={"ref": ref}
        ).json()
        pipeline_id = str(payload.get("id") or "")
        if not pipeline_id:
            raise RuntimeError("GitLab trigger did not return a pipeline id.")
        return PipelineRunHandle(
            self.provider, self.repository, ref, pipeline_id, workflow_id=self.project
        )

    def poll(self, handle: PipelineRunHandle) -> PipelineRunStatus:
        if handle.provider != self.provider or handle.repository != self.repository:
            raise ValueError("Pipeline run handle does not belong to this adapter.")
        payload = self._request(
            "GET", f"/projects/{self._project_path}/pipelines/{quote(handle.external_id, safe='')}"
        ).json()
        raw = str(payload.get("status") or "pending").lower()
        completed = raw in self._TERMINAL
        passed = raw == "success"
        status = "passed" if passed else "failed" if completed else "pending"
        check = PullRequestValidationCheck(
            name="GitLab CI", status="completed" if completed else "in_progress",
            conclusion="success" if passed else "failure" if completed else None,
            html_url=payload.get("web_url"),
            started_at=payload.get("started_at"), completed_at=payload.get("finished_at"),
        )
        return PipelineRunStatus(self.provider, self.repository, handle.ref, status, completed, [check])

    def verdict(self, handle: PipelineRunHandle) -> PipelineVerdict:
        run = self.poll(handle)
        summary = (
            "GitLab pipeline passed." if run.status == "passed" else
            "GitLab pipeline failed." if run.completed else "GitLab pipeline is still running."
        )
        return PipelineVerdict(
            run.provider, run.repository, run.ref, run.status, summary, run.completed, run.checks
        )

    def wait_for_verdict(self, ref: str, *, timeout_seconds: int, poll_interval_seconds: int):
        return _trigger_and_wait(
            self, ref, timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )


class BitbucketPipelines:
    """Bitbucket Pipelines adapter (BOSS-269) — CI checks for Bitbucket-hosted target repos.

    Rides the RepoHost seam's BitbucketAdapter for auth + repo identity; normalizes Bitbucket's
    state machine (PENDING/IN_PROGRESS/COMPLETED x SUCCESSFUL/FAILED/STOPPED) onto the pipeline
    verdict vocabulary used by the other providers."""

    provider = "bitbucket_pipelines"
    _TERMINAL_RESULTS = {"SUCCESSFUL": "passed", "FAILED": "failed", "STOPPED": "failed",
                         "ERROR": "failed"}

    def __init__(self, settings: Settings, bitbucket=None):
        from .bitbucket_adapter import BitbucketAdapter
        self.settings = settings
        self.bitbucket = bitbucket or BitbucketAdapter(settings)

    @property
    def repository(self) -> str:
        return self.bitbucket.repository_full_name

    def discover(self, repo: str) -> PipelineDescriptor:
        try:
            self.bitbucket._request("GET", f"{self.bitbucket._repo_path()}/pipelines_config")
            configured = True
        except Exception:
            configured = False
        return PipelineDescriptor(self.provider, self.repository, configured,
                                  workflows=[{"name": "bitbucket-pipelines.yml"}] if configured else [])

    def trigger(self, ref: str) -> PipelineRunHandle:
        created = self.bitbucket._request(
            "POST", f"{self.bitbucket._repo_path()}/pipelines/",
            json_body={"target": {"type": "pipeline_ref_target", "ref_type": "branch",
                                  "ref_name": ref}})
        return PipelineRunHandle(self.provider, self.repository, ref,
                                 external_id=str(created.get("uuid", "")))

    def poll(self, handle: PipelineRunHandle) -> PipelineRunStatus:
        run = self.bitbucket._request(
            "GET", f"{self.bitbucket._repo_path()}/pipelines/{handle.external_id}")
        state = (run.get("state") or {})
        stage = str(state.get("name") or "PENDING").upper()
        result = str(((state.get("result") or {}).get("name")) or "").upper()
        completed = stage == "COMPLETED"
        status = self._TERMINAL_RESULTS.get(result, "failed") if completed else "in_progress"
        check = PullRequestValidationCheck(
            name="Bitbucket Pipelines", status="completed" if completed else "in_progress",
            conclusion=(status if completed else None), html_url=None,
            started_at=str(run.get("created_on") or "") or None,
            completed_at=str(run.get("completed_on") or "") or None)
        return PipelineRunStatus(self.provider, handle.repository, handle.ref,
                                 status, completed, [check])

    def verdict(self, handle: PipelineRunHandle) -> PipelineVerdict:
        run = self.poll(handle)
        summary = (
            "Bitbucket pipeline passed." if run.status == "passed" else
            "Bitbucket pipeline failed." if run.completed else
            "Bitbucket pipeline is still running."
        )
        return PipelineVerdict(
            run.provider, run.repository, run.ref, run.status, summary, run.completed, run.checks
        )

    def wait_for_verdict(self, ref: str, *, timeout_seconds: int, poll_interval_seconds: int):
        return _trigger_and_wait(
            self, ref, timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )


def make_pipeline_adapter(
    settings: Settings,
    *,
    github: Optional[GitHubAdapter] = None,
) -> PipelineAdapter:
    if getattr(github, "kind", "") == "local":
        # BOSSAPP-918: a local repository's CI is BOSS's own sandbox, which already ran.
        from .local_repo_host import LocalSandboxPipeline

        return LocalSandboxPipeline(settings, github)
    provider = settings.pipeline_provider.strip().lower()
    if provider == "github_actions":
        return GitHubActionsPipeline(settings, github=github)
    if provider == "jenkins":
        return JenkinsPipeline(settings)
    if provider == "gitlab":
        return GitLabPipeline(settings)
    if provider == "bitbucket_pipelines":
        return BitbucketPipelines(settings)
    raise ValueError(f"Unsupported pipeline provider: {settings.pipeline_provider}")


# Static discovery catalog — kinds/capabilities only. Never includes secret values.
_PIPELINE_ADAPTER_CATALOG: list[dict] = [
    {
        "kind": "github_actions",
        "display_name": "GitHub Actions",
        "description": (
            "Read check-runs and optionally dispatch workflows for the configured "
            "target GitHub repository."
        ),
        "capabilities": ["discover", "trigger", "poll", "verdict", "wait_for_verdict"],
        "read_only_operations": ["discover", "poll", "verdict"],
        "effect_operations": ["trigger"],
        "config_keys": [
            "BOSS_PIPELINE_PROVIDER",
            "BOSS_TARGET_GITHUB_OWNER",
            "BOSS_TARGET_GITHUB_REPO",
        ],
        "secret_config_keys": ["BOSS_GITHUB_TOKEN"],
        "requires_credentials": True,
        "status": "available",
    },
    {
        "kind": "jenkins",
        "display_name": "Jenkins",
        "description": (
            "Drive a Jenkins job via buildWithParameters; poll queue/build status."
        ),
        "capabilities": ["discover", "trigger", "poll", "verdict", "wait_for_verdict"],
        "read_only_operations": ["discover", "poll", "verdict"],
        "effect_operations": ["trigger"],
        "config_keys": [
            "BOSS_PIPELINE_PROVIDER",
            "BOSS_JENKINS_BASE_URL",
            "BOSS_JENKINS_JOB",
            "BOSS_JENKINS_USER",
            "BOSS_TARGET_GITHUB_OWNER",
            "BOSS_TARGET_GITHUB_REPO",
        ],
        "secret_config_keys": ["BOSS_JENKINS_TOKEN"],
        "requires_credentials": True,
        "status": "available",
    },
    {
        "kind": "gitlab",
        "display_name": "GitLab CI",
        "description": "Trigger and poll GitLab project pipelines via the REST API.",
        "capabilities": ["discover", "trigger", "poll", "verdict", "wait_for_verdict"],
        "read_only_operations": ["discover", "poll", "verdict"],
        "effect_operations": ["trigger"],
        "config_keys": [
            "BOSS_PIPELINE_PROVIDER",
            "BOSS_GITLAB_BASE_URL",
            "BOSS_GITLAB_PROJECT_ID",
            "BOSS_TARGET_GITHUB_OWNER",
            "BOSS_TARGET_GITHUB_REPO",
        ],
        "secret_config_keys": ["BOSS_GITLAB_TOKEN"],
        "requires_credentials": True,
        "status": "available",
    },
    {
        "kind": "bitbucket_pipelines",
        "display_name": "Bitbucket Pipelines",
        "description": (
            "CI checks for Bitbucket-hosted target repos via the Pipelines API."
        ),
        "capabilities": ["discover", "trigger", "poll", "verdict", "wait_for_verdict"],
        "read_only_operations": ["discover", "poll", "verdict"],
        "effect_operations": ["trigger"],
        "config_keys": [
            "BOSS_PIPELINE_PROVIDER",
            "BOSS_TARGET_BITBUCKET_WORKSPACE",
            "BOSS_TARGET_BITBUCKET_REPO",
            "BOSS_BITBUCKET_USERNAME",
        ],
        "secret_config_keys": [
            "BOSS_BITBUCKET_TOKEN",
            "BOSS_BITBUCKET_APP_PASSWORD",
        ],
        "requires_credentials": True,
        "status": "available",
    },
]


def pipeline_adapter_catalog(
    settings: Optional[Settings] = None,
) -> dict:
    """Read-only discovery of pipeline adapter kinds/capabilities (no secrets).

    Returns static kind metadata plus the configured default provider when
    settings are supplied. Credential *names* are listed; values never are.
    """
    default_provider = "github_actions"
    if settings is not None:
        raw = getattr(settings, "pipeline_provider", None) or default_provider
        default_provider = str(raw).strip().lower() or default_provider
    items = [dict(item) for item in _PIPELINE_ADAPTER_CATALOG]
    for item in items:
        item["is_default"] = item["kind"] == default_provider
    return {
        "default_provider": default_provider,
        "note": (
            "Catalog lists adapter kinds and capability seams only. Secret values "
            "are never included. Trigger is an effect and must sit behind policy, "
            "budget, and Receipt gates."
        ),
        "items": items,
    }
