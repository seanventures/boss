"""DEPLOYING A NEW APP NEEDS ONLY A MACHINE. Nothing is filled in up front:

  * a workflow with a deploy step is ready (no "Needs Deploy set up") once the project has a
    verified host — no target, no manifest path, no port;
  * the app's FIRST deploy creates its target: the app's name, the environment, a FREE port on
    the host, a URL and health check derived from the host, the host's credential;
  * the plan says all of that to the person approving, with the Infra page that edits it;
  * the build is told to write the app's own boss.deploy.yaml.
"""
from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.services import delivery_pipeline, deployment_fleet, kit_connections, workflow_capability_runner

HOME = "boss-dogfood"


def _verified_host(client, engine, address="203.0.113.77"):
    with engine.begin() as c:
        host = deployment_fleet.register_host(c, project_id=HOME, name=f"vm-{uuid4().hex[:4]}",
                                              address=address, ssh_user="deploy", actor="ceo")
        c.execute(text("UPDATE deployment_hosts SET status = 'verified' WHERE id = :i"), {"i": host["id"]})
    return host


def _built(engine, app="Shift Swap Board"):
    run_id = f"crun_{uuid4().hex[:12]}"
    with engine.begin() as c:
        c.execute(text("INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state, finalize_ctx) "
                       "VALUES (:i, :p, 'completed', 'done', :t, CAST(:s AS jsonb), CAST(:st AS jsonb), CAST(:f AS jsonb))"),
                  {"i": run_id, "p": HOME, "t": f"Build {app}", "s": json.dumps({"repo": "paramg/boss-sandbox"}),
                   "st": json.dumps({"build_database": {"postgres": True, "mode": "auto", "why": ["the brief names PostgreSQL"]}}),
                   "f": json.dumps({"app_name": app})})
    return run_id


def test_a_verified_host_is_somewhere_to_deploy_no_target_needed(client, engine):
    with engine.begin() as c:
        assert kit_connections.deps(c, api_main.settings, HOME)["deploy_target"] is False
    _verified_host(client, engine)
    with engine.begin() as c:
        assert kit_connections.deps(c, api_main.settings, HOME)["deploy_target"] is True


def test_the_first_deploy_creates_the_target_on_a_free_port_and_the_plan_shows_it(client, engine, monkeypatch):
    host = _verified_host(client, engine)
    with engine.begin() as c:
        c.execute(text("INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
                       "target_ref, host_id, status, domains, created_by) VALUES (:i, :p, 'Other App', 'boss-sandbox', "
                       "'staging', 'ssh', 'deploy@203.0.113.77:22', :h, 'active', CAST(:d AS jsonb), 'ceo')"),
                  {"i": f"deptgt_{uuid4().hex[:10]}", "p": HOME, "h": host["id"],
                   "d": json.dumps(["203.0.113.77:8000"])})
    monkeypatch.setattr(deployment_fleet, "_first_free_port",
                        lambda c, address: 8001)   # the scan of the live host is not this test's
    planned = {}

    class Plane:
        def __init__(self, settings):
            pass

        def plan_deployment_run(self, c, **kw):
            planned.update(kw)
            return {"deployment_run_id": "dr_1", "changeset_id": "cs_1", "status": "planned"}

    monkeypatch.setattr("boss_engine.services.resource_plane.ResourcePlaneService", Plane)
    run_id = _built(engine)
    out = delivery_pipeline.deploy_build(api_main.engine, api_main.settings, project_id=HOME, run_id=run_id,
                                         environment="staging", actor="ceo")
    assert out["status"] == "planned", out
    assert out["target_created"] is True
    plan = out["plan"]
    assert plan["app"] == "Shift Swap Board" and plan["environment"] == "staging"
    assert plan["port"] == 8001 and plan["url"] == "http://203.0.113.77:8001/"
    assert plan["health"] == "http://203.0.113.77:8001/ expecting 200"
    assert plan["database"] == "Postgres" and plan["edit_href"].endswith(out["target_id"])
    with engine.begin() as c:
        row = c.execute(text("SELECT app_name, host_id, domains FROM deployment_targets WHERE id = :i"),
                        {"i": out["target_id"]}).mappings().one()
    assert row["app_name"] == "Shift Swap Board" and row["host_id"] == host["id"]
    assert row["domains"] == ["203.0.113.77:8001"]
    again = delivery_pipeline.deploy_build(api_main.engine, api_main.settings, project_id=HOME, run_id=run_id,
                                           environment="staging", actor="ceo")
    assert again["target_id"] == out["target_id"] and again["target_created"] is False


def test_with_two_machines_and_no_choice_it_asks_where(client, engine):
    _verified_host(client, engine, "203.0.113.78")
    _verified_host(client, engine, "203.0.113.79")
    out = delivery_pipeline.deploy_build(api_main.engine, api_main.settings, project_id=HOME,
                                         run_id=_built(engine, "Two Hosts App"), environment="staging", actor="ceo")
    assert out["status"] == "failed" and out["missing"] == "deployment_host" and len(out["hosts"]) == 2


def test_the_build_of_a_deploying_workflow_is_told_to_write_its_manifest():
    assert "boss.deploy.yaml" in workflow_capability_runner.DEPLOY_MANIFEST_BRIEF
    assert "{target.public_url}" in workflow_capability_runner.DEPLOY_MANIFEST_BRIEF


def test_the_plan_is_said_on_the_approval_card():
    words = delivery_pipeline.deploy_plan_words({
        "app": "Shift Swap Board", "environment": "staging", "host": "vm-a", "port": 8001,
        "url": "http://203.0.113.77:8001/", "health": "http://203.0.113.77:8001/ expecting 200",
        "credential": "the host's own key", "database": "Postgres", "created_now": True,
        "edit_href": "/?area=infra&view=fleet&target=deptgt_1"})
    assert words.splitlines() == [
        "Deploys Shift Swap Board (staging) to vm-a on port 8001 — its place there is created now",
        "URL: http://203.0.113.77:8001/", "Health: http://203.0.113.77:8001/ expecting 200",
        "Credential: the host's own key", "Database: Postgres for this build",
        "Edit: /?area=infra&view=fleet&target=deptgt_1"]
