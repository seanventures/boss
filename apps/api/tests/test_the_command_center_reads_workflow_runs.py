"""The landing page shows the run an operator just started, and what is stopping it.

A run started by Build creates no `tasks` row, so the old task-based Command Center never
showed it: the operator pressed Build and the landing page carried on describing nothing. This
reads the endpoint against real columns, and checks the decision cards carry the actions the
engine allows (boss-1: Approve on a `model_capacity_denied` did nothing at all).
"""
from __future__ import annotations

import json

from sqlalchemy import text

from apps.api.app.config import get_settings


def _seed(engine, pid):
    spec = {"steps": [{"id": "build", "binding": {"capability": "build"}},
                      {"id": "deploy", "binding": {"capability": "deploy"}}]}
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO workflow_definitions (id, project_id, workflow_key, name, created_by, "
            "        namespace) "
            "VALUES ('wfd_cc1', :p, 'standard_delivery_cc', 'Standard Delivery', 'ceo', "
            "        'product') ON CONFLICT (id) DO NOTHING"), {"p": pid})
        c.execute(text(
            "INSERT INTO workflow_versions (id, project_id, workflow_definition_id, version, "
            "        status, spec, content_hash, created_by) "
            "VALUES ('wfv_cc1', :p, 'wfd_cc1', 1, 'published', CAST(:s AS JSONB), 'hcc1', "
            "        'ceo') ON CONFLICT (id) DO NOTHING"), {"p": pid, "s": json.dumps(spec)})
        for run_id, status in (("wfr_cc_live", "waiting"), ("wfr_cc_dead", "failed")):
            c.execute(text(
                "INSERT INTO workflow_runs (id, project_id, workflow_definition_id, "
                "        workflow_version_id, actor, status, input) "
                "VALUES (:i, :p, 'wfd_cc1', 'wfv_cc1', 'ceo', :s, "
                "        CAST('{\"title\": \"Incident timeline\"}' AS JSONB)) "
                "ON CONFLICT (id) DO NOTHING"), {"i": run_id, "p": pid, "s": status})
        c.execute(text(
            "INSERT INTO decisions (id, project_id, question, recommendation, status) "
            "VALUES ('dec_cc_gate', :p, 'Deploy?', CAST(:r AS JSONB), 'pending'), "
            "       ('dec_cc_cap', :p, 'Model capacity refused typed decision x/y', "
            "        CAST('{\"action\": \"model_capacity_denied\"}' AS JSONB), 'pending') "
            "ON CONFLICT (id) DO NOTHING"),
            {"p": pid, "r": json.dumps({"workflow_run_id": "wfr_cc_live",
                                        "capability": "deploy", "step_id": "deploy"})})
        c.execute(text(
            "INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, step_id, "
            "        step_kind, status, decision_id, last_error, started_at) VALUES "
            "('wsr_cc1', :p, 'wfr_cc_live', 'deploy', 'capability', 'waiting', "
            "  'dec_cc_gate', NULL, now() - interval '5 minutes'), "
            "('wsr_cc2', :p, 'wfr_cc_dead', 'build', 'capability', 'failed', NULL, "
            "  'the sandbox ran out of disk', now()) "
            "ON CONFLICT (id) DO NOTHING"), {"p": pid})


def test_a_waiting_run_a_failed_step_and_a_refusal_all_reach_the_landing_page(engine, client):
    pid = get_settings().default_project_id
    _seed(engine, pid)
    body = client.get("/v1/console/command-center", params={"project_id": pid}).json()

    live = [r for r in body["running"] if r["run_id"] == "wfr_cc_live"]
    assert live and live[0]["step_label"] == "Deploy"
    assert live[0]["agent"]["role_key"] == "deployment_manager"
    assert live[0]["elapsed_seconds"] >= 290

    kinds = {(n["kind"], n.get("decision_id") or n.get("run_id")) for n in body["needs_you"]}
    assert ("approval", "dec_cc_gate") in kinds
    assert ("failed_step", "wfr_cc_dead") in kinds
    assert ("approval", "dec_cc_cap") in kinds
    gate = next(n for n in body["needs_you"] if n.get("decision_id") == "dec_cc_gate")
    assert gate["run_id"] == "wfr_cc_live"


def test_decision_cards_carry_only_the_actions_that_work(engine, client):
    pid = get_settings().default_project_id
    _seed(engine, pid)
    items = client.get("/v1/decisions/inbox", params={"project_id": pid}).json()["items"]
    cap = next(i for i in items if i["id"] == "dec_cc_cap")
    assert cap["kind"] == "refusal"
    assert [a["action"] for a in cap["allowed_actions"]] == ["reject"]
    assert cap["guidance"]["area"] == "setup"
    gate = next(i for i in items if i["id"] == "dec_cc_gate")
    assert [a["action"] for a in gate["allowed_actions"]] == ["approve", "reject", "delegate"]


def test_approving_a_refusal_is_refused_with_what_to_do(engine, client):
    pid = get_settings().default_project_id
    _seed(engine, pid)
    response = client.post("/v1/decisions/dec_cc_cap/approve",
                           json={"actor": "ceo", "note": "ok"})
    assert response.status_code == 409
    assert "Repo and model" in response.json()["detail"]
