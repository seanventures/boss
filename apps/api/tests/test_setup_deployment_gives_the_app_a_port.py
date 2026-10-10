"""Set target must leave an app its first deploy can plan, and say so in words.

D1 (2026-10-09): setup-deployment answered 200 with `bootstrap: {"error": "actor is required"}`.
It called bootstrap_deployment_target without an actor, and that function cannot assign a port
anyway (a direct host bootstrap is refused without a governed ChangeSet). The app was left with
no port, its first deploy was refused with `app_port_missing`, and the refusal reached the
operator as "could not plan a deployment: {'status': 'blocked', 'refused': True, ...}".
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from apps.api.tests.test_setup_deployment_binds_orphan_target import (  # noqa: F401
    ACTOR, PROJECT_ID, _orphan_target, _setup, seeded,
)


@pytest.fixture(autouse=True)
def _no_network_probe(monkeypatch):
    from boss_engine.services import deployment_fleet

    monkeypatch.setattr(deployment_fleet, "_first_free_port",
                        lambda connection, address, start=3000: 3417)


def _target(engine, target_id):
    with engine.begin() as c:
        return dict(c.execute(text("SELECT * FROM deployment_targets WHERE id = :i"),
                              {"i": target_id}).mappings().first())


def _assert_words(report):
    assert isinstance(report, dict), report
    assert "error" not in report, report
    assert report.get("ok") is True, report
    assert isinstance(report.get("message"), str) and "{" not in report["message"], report


def test_a_new_target_gets_a_port_and_a_domain_its_deploy_can_plan(client, engine, seeded):
    response = _setup(client, seeded)
    assert response.status_code in (200, 201), response.text
    body = response.json()
    _assert_words(body["bootstrap"])
    assert body["bootstrap"]["port"] == 3417

    from boss_engine.services.governed_host_deploy import plan_problem

    target = _target(engine, body["target_id"])
    assert "203.0.113.10:3417" in (target["domains"] or [])
    with engine.begin() as c:
        assert plan_problem(c, target) is None, plan_problem(c, target)


def test_an_adopted_target_gets_its_port_too(client, engine, seeded):
    target_id = _orphan_target(engine, seeded["app_name"])
    body = _setup(client, seeded).json()
    _assert_words(body["bootstrap"])
    target = _target(engine, target_id)
    assert any(r.get("kind") == "app_port" and r.get("name") == "3417"
               for r in target["required_resources"] or [])


def test_a_port_the_operator_gave_is_kept(client, engine, seeded):
    body = _setup(client, seeded, app_port="3555").json()
    _assert_words(body["bootstrap"])
    assert body["bootstrap"]["port"] == 3555


def test_a_refused_deploy_is_words_not_a_dict(client, engine, seeded):
    """A target with no port is refused by the planner; the reason must be its sentence."""
    target_id = _orphan_target(engine, seeded["app_name"])
    with engine.begin() as c:
        c.execute(text("UPDATE deployment_targets SET host_id = :h WHERE id = :i"),
                  {"h": seeded["host_id"], "i": target_id})
    response = client.post("/v1/delivery/first-deploy", json={
        "project_id": PROJECT_ID, "run_id": seeded["run_id"], "actor": ACTOR,
        "target_id": target_id})
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, str) and "{" not in detail and "'refused'" not in detail, detail
    assert "port" in detail.lower(), detail


def test_planned_reason_never_renders_a_dict():
    from boss_engine.services.delivery_pipeline import planned_reason

    assert planned_reason({"status": "blocked", "failure_reason": "No port. Set one."}) == \
        "No port. Set one."
    assert "{" not in planned_reason({"status": "blocked", "id": None})
