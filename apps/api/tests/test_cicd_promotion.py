from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text

from apps.api.app.config import Settings, get_settings
from apps.api.app.services.ci_config_generator import CIConfigGenerator
from apps.api.app.services.deployment_rollout_engine import DeploymentRolloutEngine
from apps.api.app.services.github_adapter import GitHubAdapter
from apps.api.app.services.pipeline_adapter import (
    GitLabPipeline,
    GitHubActionsPipeline,
    JenkinsPipeline,
    PipelineDescriptor,
    PipelineVerdict,
)
from apps.api.app.services.promotion_orchestrator import PromotionOrchestrator
from apps.api.app.services.resource_plane import ResourcePlaneService


def _settings() -> Settings:
    return Settings(
        BOSS_TARGET_GITHUB_OWNER="acme",
        BOSS_TARGET_GITHUB_REPO="widget",
        BOSS_GITHUB_TOKEN="test-token",
        BOSS_SANDBOX_CI_TIMEOUT_SECONDS=1,
        BOSS_SANDBOX_CI_POLL_INTERVAL_SECONDS=1,
    )


def test_pipeline_adapter_discovers_triggers_and_evaluates_github_actions(monkeypatch):
    github = GitHubAdapter(_settings())
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(method: str, path: str, json_body=None, **_kwargs):
        calls.append((method, path, json_body))
        if path.endswith("/actions/workflows?per_page=100"):
            return {
                "workflows": [
                    {
                        "id": 91,
                        "name": "CI",
                        "path": ".github/workflows/ci.yml",
                        "state": "active",
                        "html_url": "https://github.test/actions/91",
                    }
                ]
            }
        if path.endswith("/check-runs"):
            return {
                "check_runs": [
                    {
                        "name": "test",
                        "status": "completed",
                        "conclusion": "success",
                        "html_url": "https://github.test/check/1",
                    }
                ]
            }
        return {}

    monkeypatch.setattr(github, "_request", fake_request)
    pipeline = GitHubActionsPipeline(_settings(), github=github)

    descriptor = pipeline.discover("acme/widget")
    handle = pipeline.trigger("abc123")
    verdict = pipeline.verdict(handle)

    assert descriptor.configured is True
    assert descriptor.workflows[0]["path"] == ".github/workflows/ci.yml"
    assert handle.workflow_id == "91"
    assert verdict.status == "passed"
    assert calls[2] == (
        "POST",
        "/repos/acme/widget/actions/workflows/91/dispatches",
        {"ref": "abc123"},
    )


class _FakeResponse:
    def __init__(self, payload=None, *, headers=None):
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _JenkinsClient:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/job/BOSS/api/json"):
            return _FakeResponse({"displayName": "BOSS", "buildable": True, "url": url[:-8]})
        if url.endswith("/crumbIssuer/api/json"):
            return _FakeResponse({"crumbRequestField": "Jenkins-Crumb", "crumb": "crumb-1"})
        if "buildWithParameters" in url:
            return _FakeResponse(headers={"Location": "https://jenkins.test/queue/item/44/"})
        if url.endswith("/queue/item/44/api/json"):
            return _FakeResponse({"executable": {"url": "https://jenkins.test/job/BOSS/17/"}})
        if url.endswith("/job/BOSS/17/api/json"):
            return _FakeResponse({"building": False, "result": "SUCCESS", "url": url[:-8]})
        raise AssertionError(f"unexpected Jenkins request: {method} {url}")


class _GitLabClient:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/projects/acme%2Fwidget/pipelines?per_page=1"):
            return _FakeResponse([{"id": 8, "status": "success", "web_url": "https://gitlab.test/p/8"}])
        if method == "POST" and url.endswith("/projects/acme%2Fwidget/pipeline"):
            return _FakeResponse({"id": 9, "status": "pending"})
        if url.endswith("/projects/acme%2Fwidget/pipelines/9"):
            return _FakeResponse({
                "id": 9, "status": "success", "web_url": "https://gitlab.test/p/9",
                "started_at": "2026-07-16T01:00:00Z", "finished_at": "2026-07-16T01:01:00Z",
            })
        raise AssertionError(f"unexpected GitLab request: {method} {url}")


def test_jenkins_pipeline_triggers_polls_and_maps_terminal_verdict():
    settings = Settings(
        BOSS_TARGET_GITHUB_OWNER="acme", BOSS_TARGET_GITHUB_REPO="widget",
        BOSS_JENKINS_BASE_URL="https://jenkins.test", BOSS_JENKINS_JOB="BOSS",
        BOSS_JENKINS_USER="boss", BOSS_JENKINS_TOKEN="secret",
    )
    fake = _JenkinsClient()
    pipeline = JenkinsPipeline(settings, client=fake)
    assert pipeline.discover("acme/widget").configured is True
    handle = pipeline.trigger("abc123")
    verdict = pipeline.verdict(handle)
    assert handle.external_id == "https://jenkins.test/queue/item/44/"
    assert verdict.status == "passed"
    assert verdict.completed is True
    assert verdict.checks[0].conclusion == "success"
    trigger_call = next(call for call in fake.calls if "buildWithParameters" in call[1])
    assert trigger_call[2]["headers"]["Jenkins-Crumb"] == "crumb-1"


def test_gitlab_pipeline_triggers_polls_and_maps_terminal_verdict():
    settings = Settings(
        BOSS_TARGET_GITHUB_OWNER="acme", BOSS_TARGET_GITHUB_REPO="widget",
        BOSS_GITLAB_BASE_URL="https://gitlab.test", BOSS_GITLAB_PROJECT_ID="acme/widget",
        BOSS_GITLAB_TOKEN="secret",
    )
    fake = _GitLabClient()
    pipeline = GitLabPipeline(settings, client=fake)
    descriptor = pipeline.discover("acme/widget")
    handle = pipeline.trigger("main")
    verdict = pipeline.verdict(handle)
    assert descriptor.configured is True
    assert handle.external_id == "9"
    assert verdict.status == "passed"
    assert verdict.completed is True
    assert all(call[2]["headers"]["PRIVATE-TOKEN"] == "secret" for call in fake.calls)


def test_ci_config_generator_is_deterministic_and_provider_safe():
    generator = CIConfigGenerator()
    first = generator.generate(language="typescript", framework="nextjs")
    second = generator.generate(language="typescript", framework="nextjs")
    jenkins = generator.generate(language="python", target="jenkins")

    assert first == second
    assert first["path"] == ".github/workflows/ci.yml"
    assert "jobs:" in first["content"]
    assert "test:" in first["content"]
    assert "build:" in first["content"]
    assert "deploy:" in first["content"]
    assert "Provider deployment remains ChangeSet-gated" in first["content"]
    assert jenkins["path"] == "Jenkinsfile"


def test_deployment_rollout_api_is_permissioned_and_idempotent(client, engine):
    settings = get_settings()
    resource = ResourcePlaneService(settings)
    with engine.begin() as connection:
        target = resource.create_deployment_target(
            connection,
            project_id=settings.default_project_id,
            app_name="Rollout API fixture",
            repo="example-org/boss-sandbox",
            environment="preview",
            provider_account_id=None,
            target_kind="local",
            target_ref="rollout-api-fixture",
            domains=[],
            required_resources=[],
            secret_refs=[],
            health_checks=[],
            rollback_points=[{"step": "restore-previous-release"}],
            actor="ceo",
        )
        deployment_run = resource.plan_deployment_run(
            connection,
            project_id=settings.default_project_id,
            deployment_target_id=target["id"],
            commit_ref="abc123",
            release_packet={"source": "test"},
            expected_cost_usd_micros=0,
            reservation_id=None,
            actor="ceo",
        )

    denied = client.post(
        f"/v1/deployment-runs/{deployment_run['id']}/rollout/start",
        json={"actor": "unknown-user"},
    )
    assert denied.status_code == 403

    started = client.post(
        f"/v1/deployment-runs/{deployment_run['id']}/rollout/start",
        json={"actor": "ceo", "stages": ["canary", "full"]},
    )
    assert started.status_code == 200
    assert started.json()["current_stage"] == "canary"

    advanced = client.post(
        f"/v1/deployment-runs/{deployment_run['id']}/rollout/advance",
        json={"actor": "ceo", "observation": {"health": "healthy"}},
    )
    assert advanced.status_code == 200
    assert advanced.json()["current_stage"] == "full"

    first_rollback = client.post(
        f"/v1/deployment-runs/{deployment_run['id']}/rollout/rollback",
        json={"actor": "ceo", "reason": "operator rollback"},
    )
    second_rollback = client.post(
        f"/v1/deployment-runs/{deployment_run['id']}/rollout/rollback",
        json={"actor": "ceo", "reason": "operator rollback"},
    )
    assert first_rollback.status_code == 200
    assert second_rollback.status_code == 200
    assert first_rollback.json()["status"] == "rolled_back"
    assert second_rollback.json()["id"] == first_rollback.json()["id"]


@dataclass
class _PassingPipeline:
    provider: str = "github_actions"

    def discover(self, repo: str) -> PipelineDescriptor:
        return PipelineDescriptor(
            provider=self.provider,
            repository=repo,
            configured=True,
            workflows=[{"id": "1", "name": "CI", "state": "active"}],
        )

    def wait_for_verdict(self, ref: str, **_kwargs) -> PipelineVerdict:
        return PipelineVerdict(
            provider=self.provider,
            repository="example-org/boss-sandbox",
            ref=ref,
            status="passed",
            summary="CI passed.",
            completed=True,
            checks=[],
        )


def _provider_targets(connection, resource: ResourcePlaneService) -> tuple[str, str]:
    project_id = "boss-dogfood"
    account = resource.create_provider_account(
        connection,
        project_id=project_id,
        provider="digitalocean",
        account_label="promotion-test",
        credential_ref="env:BOSS_DIGITALOCEAN_TOKEN",
        scopes=["read", "write"],
        read_only=False,
        deploy_capable=True,
        billing_readable=False,
        destructive_actions_disabled=True,
        actor="ceo",
    )
    targets: dict[str, str] = {}
    for environment in ("staging", "production"):
        target = resource.create_deployment_target(
            connection,
            project_id=project_id,
            app_name="BOSS Sandbox",
            repo="example-org/boss-sandbox",
            environment=environment,
            provider_account_id=account["id"],
            target_kind="digitalocean_app",
            target_ref=f"boss-{environment}",
            domains=[f"{environment}.boss.test"],
            required_resources=[],
            secret_refs=[],
            health_checks=[
                {
                    "name": f"{environment}-health",
                    "url": f"https://{environment}.boss.test/health",
                    "expected_status": 200,
                }
            ],
            rollback_points=[{"step": f"restore-{environment}-release"}],
            actor="ceo",
        )
        resource.upsert_deployment_environment_policy(
            connection,
            project_id=project_id,
            environment=environment,
            runtime_mode=(
                "production_allowed" if environment == "production" else "staging_allowed"
            ),
            actor="ceo",
            allowed_target_kinds=["digitalocean_app"],
            requires_budget_reservation=False,
            requires_health_check=True,
            requires_rollback_plan=True,
        )
        targets[environment] = target["id"]
    return targets["staging"], targets["production"]


def test_promotion_waits_for_staging_gate_then_rolls_out_and_auto_rolls_back(
    client, engine, monkeypatch
):
    settings = get_settings()
    resource = ResourcePlaneService(settings)
    rollout_engine = DeploymentRolloutEngine(settings)
    orchestrator = PromotionOrchestrator(
        settings,
        resource_plane=resource,
        rollout_engine=rollout_engine,
        pipeline=_PassingPipeline(),
    )
    project_id = settings.default_project_id

    with engine.begin() as connection:
        staging_target_id, production_target_id = _provider_targets(
            connection, resource
        )
        promotion = orchestrator.promote(
            connection,
            project_id=project_id,
            staging_target_id=staging_target_id,
            production_target_id=production_target_id,
            commit_ref="abc123",
            actor="ceo",
        )
        assert promotion["status"] == "staging_planned"
        assert promotion["production_deployment_run"] is None
        staging_run = promotion["staging_deployment_run"]
        connection.execute(
            text("UPDATE changesets SET status = 'applied' WHERE id = :id"),
            {"id": staging_run["changeset_id"]},
        )
        monkeypatch.setattr(
            resource,
            "_run_http_health_check",
            lambda check: {
                "name": check["name"],
                "status": "passed",
                "status_code": 200,
                "expected_status": 200,
            },
        )
        promotion = orchestrator.advance_after_staging_gate(
            connection,
            project_id=project_id,
            promotion_id=promotion["id"],
            actor="ceo",
        )
        assert promotion["status"] == "production_planned"
        assert promotion["gate_result"]["status"] == "passed"
        assert promotion["production_rollout"]["current_stage"] == "canary_10pct"
        timeline_types = [item["event_type"] for item in promotion["timeline"]]
        assert timeline_types[0] == "pipeline.verdict"
        assert "deployment.staging_planned" in timeline_types
        assert "deployment.staging_ok" in timeline_types
        assert "deployment.promoted" in timeline_types

        production_run = promotion["production_deployment_run"]
        connection.execute(
            text("UPDATE changesets SET status = 'applied' WHERE id = :id"),
            {"id": production_run["changeset_id"]},
        )
        rollout = rollout_engine.advance_rollout(
            connection,
            project_id=project_id,
            deployment_run_id=production_run["id"],
            actor="ceo",
            observation={"health": "healthy", "error_rate": 0},
        )
        assert rollout["current_stage"] == "expand_50pct"
        rolled_back = rollout_engine.advance_rollout(
            connection,
            project_id=project_id,
            deployment_run_id=production_run["id"],
            actor="ceo",
            observation={"health": "degraded", "error_rate": 0.2},
        )
        assert rolled_back["status"] == "rolled_back"
        assert "Auto-rollback" in rolled_back["rollback_reason"]
        assert (
            resource.get_deployment_run(connection, project_id, production_run["id"])[
                "status"
            ]
            == "rollback_pending"
        )
        assert orchestrator.get_promotion_detail(
            connection, project_id, promotion["id"]
        )["status"] == "rolled_back"
        assert connection.execute(
            text(
                """
                SELECT count(*) FROM events
                WHERE project_id = :project_id
                  AND event_type = 'deployment.rolled_back'
                """
            ),
            {"project_id": project_id},
        ).scalar_one() == 1

    detail = client.get(f"/v1/deployment-promotions/{promotion['id']}")
    listing = client.get(
        "/v1/deployment-promotions",
        params={"project_id": project_id, "limit": 10},
    )
    assert detail.status_code == 200
    assert detail.json()["status"] == "rolled_back"
    assert detail.json()["production_rollout"]["rollback_reason"]
    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()["items"]] == [promotion["id"]]


def test_production_policy_blocks_before_pipeline_or_staging_run(engine):
    settings = get_settings()
    resource = ResourcePlaneService(settings)
    pipeline = _PassingPipeline()
    orchestrator = PromotionOrchestrator(
        settings,
        resource_plane=resource,
        rollout_engine=DeploymentRolloutEngine(settings),
        pipeline=pipeline,
    )
    with engine.begin() as connection:
        staging_target_id, production_target_id = _provider_targets(
            connection, resource
        )
        resource.upsert_deployment_environment_policy(
            connection,
            project_id=settings.default_project_id,
            environment="production",
            runtime_mode="metadata_only",
            actor="ceo",
            allowed_target_kinds=["digitalocean_app"],
        )
        promotion = orchestrator.promote(
            connection,
            project_id=settings.default_project_id,
            staging_target_id=staging_target_id,
            production_target_id=production_target_id,
            commit_ref="abc123",
            actor="ceo",
        )
        assert promotion["status"] == "blocked"
        assert promotion["failure_reason"] == (
            "environment_production_not_enabled_for_provider_rollout"
        )
        assert promotion["staging_deployment_run"] is None
