"""A governed host deploy in Infra › Deploys: Needs you with Approve/Reject, its plan (what will
happen, the secrets by name, rollback, cost), and the app's URL only once it is healthy
(governed_host_deploy.py, infra_view.deploy_detail)."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.tests.infra_seed import clear, seed

HOME = "boss-dogfood"
REVIEW = {
    "app_name": "invoice", "environment": "prod", "host": "edge-ready",
    "commit_ref": "main", "app_port": 8001,
    "steps": ["Preflight the host (read-only)", "Build the app at the commit",
              "Transfer and start it", "Health check"],
    "secret_refs": [{"name": "ssh_key", "ref": "sref_h_ready"}],
    "rollback": ["If the health check fails, BOSS restores the previous release on the host."],
    "cost": {"expected_cost_usd_micros": 0, "note": "Your own host: no provider cost."},
}


@pytest.fixture()
def world(engine):
    seed(engine, HOME, samples=False)
    with engine.begin() as c:
        c.execute(text("INSERT INTO decisions (id, project_id, question, status, risk_tier) "
                       "VALUES ('dec_host', :p, 'Deploy invoice to edge-ready · prod', "
                       "'pending', 3)"), {"p": HOME})
        c.execute(text(
            "INSERT INTO deployment_runs (id, project_id, deployment_target_id, decision_id, "
            "app_name, environment, repo, target_kind, target_ref, commit_ref, status, trigger, "
            "release_packet, created_by) VALUES ('dr_host', :p, 't_live', 'dec_host', "
            "'invoice', 'prod', 'acme/invoice', 'ssh', 'deploy@x:22', 'main', "
            "'awaiting_approval', 'manual', CAST(:rp AS JSONB), 'ceo')"),
            {"p": HOME, "rp": json.dumps({"governed": True, "plan_review": REVIEW})})
    yield
    with engine.begin() as c:
        c.execute(text("DELETE FROM deployment_runs WHERE id = 'dr_host'"))
        c.execute(text("DELETE FROM decisions WHERE id = 'dec_host'"))
        clear(c, HOME)


def _detail(client):
    r = client.get("/v1/infra/deploys/delivery:dr_host", params={"project_id": HOME})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_waiting_host_deploy_needs_you_and_shows_its_plan(client, world, monkeypatch):
    monkeypatch.setattr(api_main.settings, "boss_edition", "community")
    d = _detail(client)
    assert d["row"]["word"] == "Needs you"
    assert d["row"]["can"]["approve"] is True and d["row"]["can"]["reject"] is True
    assert d["row"]["decision_id"] == "dec_host"
    steps = d["plan"]["steps"]
    assert steps[0] == "Preflight the host (read-only)"
    assert any(s.startswith("Rollback: If the health check fails") for s in steps)
    assert "Cost: $0.00 (your own host)" in steps
    assert d["plan"]["env_keys"] == ["ssh_key"]
    assert d["open_url"] is None, "no link to an app that has not passed its health check"
    assert d["live"] is True


def test_a_healthy_host_deploy_links_its_app_and_a_rolled_back_one_says_why(
        client, engine, world, monkeypatch):
    monkeypatch.setattr(api_main.settings, "boss_edition", "community")
    with engine.begin() as c:
        c.execute(text("UPDATE deployment_runs SET status = 'succeeded', finished_at = now() "
                       "WHERE id = 'dr_host'"))
        c.execute(text("UPDATE decisions SET status = 'approved' WHERE id = 'dec_host'"))
    d = _detail(client)
    assert d["open_url"] == "http://203.0.113.10:8001/"
    with engine.begin() as c:
        c.execute(text("UPDATE deployment_runs SET status = 'rolled_back', failure_reason = "
                       "'The health check failed after release; BOSS rolled the host back.' "
                       "WHERE id = 'dr_host'"))
    d = _detail(client)
    assert d["row"]["word"] == "Rolled back"
    assert d["failure"] and "health check failed" in d["failure"]["error"]
    assert d["open_url"] is None
