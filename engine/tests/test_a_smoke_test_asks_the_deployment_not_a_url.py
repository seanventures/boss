"""A smoke test checks the deployment through its own target, with the health the app declares.

HelpDesk's smoke test dead-lettered in Community ("Community URL probes require a
project-bound destination…"), and where a free probe did run, "GET / is 200" passed nginx
serving the app's unbuilt index.html while /readyz — the app's declared health — answered 404.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import deployment_smoke as smoke
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflow_outcomes as outcomes

from .test_workflow_capability_runner import (  # noqa: F401 - fixtures used by name
    Ctx,
    RecordingRegistry,
    RecordingTool,
    _publish,
    _start,
    _steps,
    eng,
    project,
)

MANIFEST = """
version: 1
name: helpdesk-ai
kind: compose
compose: {file: docker-compose.yml}
health: [{url: "{target.public_url}/readyz", expect: 200}]
"""
BLANK = ('<!doctype html><html><body><div id="root"></div>'
         '<script type="module" src="/src/main.tsx"></script></body></html>')
BUILT = ('<!doctype html><html><body><div id="root"></div>'
         '<script type="module" src="/assets/index-3f2a.js"></script></body></html>')


class FakeConnector:
    """observe() answers per path, the way the target's own health check reports."""

    def __init__(self, answers):
        self.answers, self.seen = answers, []

    def observe(self, target, settings):
        checks = []
        for check in target["health_checks"]:
            path = check.get("path") or "/"
            self.seen.append(path)
            code = self.answers.get(path, 404)
            expect = int(check.get("expect_status") or 200)
            checks.append({"url": f"http://203.0.113.9:8000{path}", "status": code,
                           "expected": expect, "ok": code == expect})
        return {"healthy": all(c["ok"] for c in checks), "checks": checks}


def _shipped(c, project_id):
    """A run whose deploy step succeeded onto a target that checks only `/`."""
    _, version = _publish(c, project_id, f"ship_{uuid.uuid4().hex[:6]}", [
        {"id": "deploy", "kind": "capability",
         "binding": {"capability": "deploy", "inputs": {"run_id": "crun_x"}},
         "next": ["smoke_test"]},
        {"id": "smoke_test", "kind": "capability",
         "binding": {"capability": "smoke_test", "inputs": {"url": "http://203.0.113.9:8000/"}},
         "next": []},
    ], "deploy")
    run = _start(c, project_id, version["id"], {})
    target, dep = f"deptgt_{uuid.uuid4().hex[:12]}", f"deprun_{uuid.uuid4().hex[:12]}"
    c.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
        "target_ref, created_by, domains, health_checks, required_resources) VALUES (:t, :p, "
        "'HelpDesk', 'boss-sandbox', 'staging', 'ssh', 'root@203.0.113.9:22', 'test', "
        "CAST(:d AS JSONB), CAST(:h AS JSONB), CAST(:r AS JSONB))"),
        {"t": target, "p": project_id, "d": json.dumps(["203.0.113.9:8000"]),
         "h": json.dumps([{"kind": "http", "path": "/", "port": 8000, "scheme": "http",
                           "expect_status": 200}]),
         "r": json.dumps([{"kind": "app_port", "name": "8000"}])})
    c.execute(text(
        "INSERT INTO deployment_runs (id, project_id, deployment_target_id, app_name, environment, "
        "repo, target_kind, target_ref, created_by, status) VALUES (:d, :p, :t, 'HelpDesk', "
        "'staging', 'boss-sandbox', 'ssh', 'root@203.0.113.9:22', 'test', 'succeeded')"),
        {"d": dep, "p": project_id, "t": target})
    c.execute(text(
        "UPDATE workflow_step_runs SET status='succeeded', completed_at=now(), "
        "output=CAST(:o AS JSONB) WHERE workflow_run_id=:r AND step_id='deploy'"),
        {"r": run["id"], "o": json.dumps({"await": {"id": dep, "kind": "deployment"},
                                          "deployment_id": dep})})
    return run, target, dep


@pytest.fixture()
def cleanup(eng, project):
    yield
    with eng.begin() as c:
        c.execute(text("DELETE FROM deployment_runs WHERE project_id=:p"), {"p": project})
        c.execute(text("DELETE FROM stack_deployments WHERE project_id=:p"), {"p": project})
        c.execute(text("DELETE FROM deployment_targets WHERE project_id=:p"), {"p": project})
        c.execute(text("DELETE FROM deployment_hosts WHERE project_id=:p"), {"p": project})


def test_the_apps_declared_health_is_asked_and_a_404_fails_by_name(eng, project, cleanup):
    """/ answers 200, /readyz 404: the blank page that passed must fail, naming /readyz."""
    fake = FakeConnector({"/": 200})
    with eng.begin() as c:
        run, _t, _d = _shipped(c, project)
        out = smoke.smoke_deployment(c, None, eng, project_id=project, run_id=run["id"],
                                     manifest_text=MANIFEST, connector=fake)
    assert "/readyz" in fake.seen, "the app's declared health was never asked"
    assert out["ok"] is False and "/readyz answered 404, expected 200" in out["error"]
    assert outcomes.judge("smoke_test", out, {}).outcome == outcomes.FAILED


def test_a_page_that_serves_unbuilt_source_fails_even_when_health_passes(
        eng, project, cleanup, monkeypatch):
    monkeypatch.setattr(smoke, "_root_body", lambda url: (200, BLANK))
    with eng.begin() as c:
        run, _t, _d = _shipped(c, project)
        out = smoke.smoke_deployment(c, None, eng, project_id=project, run_id=run["id"],
                                     manifest_text=MANIFEST,
                                     connector=FakeConnector({"/": 200, "/readyz": 200}))
    assert out["ok"] is False and "unbuilt source" in out["error"]


def test_a_healthy_deployment_passes_with_what_it_checked(eng, project, cleanup, monkeypatch):
    monkeypatch.setattr(smoke, "_root_body", lambda url: (200, BUILT))
    with eng.begin() as c:
        run, target, dep = _shipped(c, project)
        out = smoke.smoke_deployment(c, None, eng, project_id=project, run_id=run["id"],
                                     manifest_text=MANIFEST,
                                     connector=FakeConnector({"/": 200, "/readyz": 200}))
    assert out.get("ok") is not False and out["status"] == 200
    assert out["declared_health"] == ["/readyz"]
    assert out["deployment_id"] == dep and out["deployment_target_id"] == target
    assert outcomes.judge("smoke_test", out, {}).outcome == outcomes.SUCCEEDED


def test_the_runner_never_reaches_for_a_free_url_probe_after_a_deploy(
        eng, project, cleanup, monkeypatch):
    """In Community the step used to call http_probe and dead-letter on its refusal."""
    probe = RecordingTool("http_probe", {"ok": False, "error": "Community URL probes require…"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([probe]))
    seen = {}

    def fake_smoke(connection, settings, engine, *, project_id, run_id):
        seen["run"] = run_id
        return {"url": "http://203.0.113.9:8000/", "status": 200, "ms": 4, "checks": []}

    monkeypatch.setattr(smoke, "smoke_deployment", fake_smoke)
    with eng.begin() as c:
        run, _t, _d = _shipped(c, project)
        c.execute(text("UPDATE workflow_step_runs SET status='ready' WHERE workflow_run_id=:r "
                       "AND step_id='smoke_test'"), {"r": run["id"]})
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
    assert not probe.calls, "a free URL probe was used to smoke-test a deployment"
    assert seen.get("run") == run["id"]
    assert steps["smoke_test"]["status"] == "succeeded", steps["smoke_test"]


def test_community_with_no_deployment_says_there_is_nothing_to_test(eng, project):
    with eng.begin() as c:
        out = smoke.smoke_deployment(c, None, eng, project_id=project, run_id="wfr_none")
    assert out["ok"] is False and "made no deployment" in out["error"]


def test_declared_health_is_read_as_a_target_relative_path():
    assert smoke.declared_health(MANIFEST) == [
        {"path": "/readyz", "expect_status": 200, "declared_by": "boss.deploy.yaml"}]
    assert smoke.declared_health("health: http://127.0.0.1:8000/healthz")[0]["path"] == "/healthz"
    assert smoke.declared_health("name: x") == []


# ── A STACK DEPLOYMENT (the app's own manifest) is smoked through its target too ─────────────

def _shipped_as_a_stack(c, project_id):
    """A run whose deploy step planned and applied a stack deployment from the app's manifest."""
    _, version = _publish(c, project_id, f"stack_{uuid.uuid4().hex[:6]}", [
        {"id": "deploy", "kind": "capability",
         "binding": {"capability": "deploy", "inputs": {"run_id": "crun_x"}},
         "next": ["smoke_test"]},
        {"id": "smoke_test", "kind": "capability",
         "binding": {"capability": "smoke_test", "inputs": {"url": "http://203.0.113.9:8000/"}},
         "next": []},
    ], "deploy")
    run = _start(c, project_id, version["id"], {})
    host, target = f"dh_{uuid.uuid4().hex[:10]}", f"deptgt_{uuid.uuid4().hex[:12]}"
    sdep = f"sdep_{uuid.uuid4().hex[:12]}"
    c.execute(text("INSERT INTO deployment_hosts (id, project_id, name, address, status) "
                   "VALUES (:h, :p, 'vm', '203.0.113.9', 'verified')"), {"h": host, "p": project_id})
    c.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
        "target_ref, host_id, created_by, domains, health_checks) VALUES (:t, :p, 'HelpDesk', "
        "'acme/boss-sandbox', 'staging', 'ssh', 'root@203.0.113.9:22', :h, 'test', "
        "CAST(:d AS JSONB), '[]'::jsonb)"),
        {"t": target, "p": project_id, "h": host, "d": json.dumps(["203.0.113.9:8000"])})
    c.execute(text(
        "INSERT INTO stack_deployments (id, project_id, target_id, host_id, repository, ref, "
        "commit_sha, created_by, status, manifest_text) VALUES (:s, :p, :t, :h, "
        "'acme/boss-sandbox', 'pull/7', :sha, 'test', 'healthy', :m)"),
        {"s": sdep, "p": project_id, "t": target, "h": host, "sha": "c" * 40, "m": MANIFEST})
    c.execute(text(
        "UPDATE workflow_step_runs SET status='succeeded', completed_at=now(), "
        "output=CAST(:o AS JSONB) WHERE workflow_run_id=:r AND step_id='deploy'"),
        {"r": run["id"], "o": json.dumps({"await": {"id": sdep, "kind": "stack_deployment"},
                                          "stack_deployment_id": sdep})})
    return run, target, sdep


def test_a_stack_deployment_is_smoked_through_its_targets_declared_health(
        eng, project, cleanup, monkeypatch):
    asked: list[str] = []

    def answer(url):
        asked.append(url)
        return (200, BUILT) if url.endswith(("/readyz", ":8000/")) else (404, "")

    monkeypatch.setattr(smoke, "_root_body", answer)
    with eng.begin() as c:
        run, target, sdep = _shipped_as_a_stack(c, project)
        assert smoke.run_deployment(c, run["id"])["id"] == sdep
        out = smoke.smoke_deployment(c, None, eng, project_id=project, run_id=run["id"])
    assert "http://203.0.113.9:8000/readyz" in asked, asked
    assert out.get("ok") is not False and out["status"] == 200, out
    assert out["stack_deployment_id"] == sdep and out["deployment_target_id"] == target
    assert outcomes.judge("smoke_test", out, {}).outcome == outcomes.SUCCEEDED


def test_a_stack_deployment_whose_declared_health_fails_fails_by_name(
        eng, project, cleanup, monkeypatch):
    monkeypatch.setattr(smoke, "_root_body", lambda url: (404, "") if url.endswith("/readyz")
                        else (200, BUILT))
    with eng.begin() as c:
        run, _t, _s = _shipped_as_a_stack(c, project)
        out = smoke.smoke_deployment(c, None, eng, project_id=project, run_id=run["id"])
    assert out["ok"] is False and "/readyz answered 404, expected 200" in out["error"], out
    assert outcomes.judge("smoke_test", out, {}).outcome == outcomes.FAILED


def test_the_runner_smokes_a_stack_deploy_without_a_free_url_probe(
        eng, project, cleanup, monkeypatch):
    probe = RecordingTool("http_probe", {"ok": False, "error": "Community URL probes require…"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([probe]))
    monkeypatch.setattr(smoke, "_root_body", lambda url: (200, BUILT))
    with eng.begin() as c:
        run, _t, _s = _shipped_as_a_stack(c, project)
        c.execute(text("UPDATE workflow_step_runs SET status='ready' WHERE workflow_run_id=:r "
                       "AND step_id='smoke_test'"), {"r": run["id"]})
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
    assert not probe.calls, "a stack deploy's smoke test fell through to a free URL probe"
    assert steps["smoke_test"]["status"] == "succeeded", steps["smoke_test"]
