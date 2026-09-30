"""A change to a project runs inline unless a workflow is picked — and then it runs that workflow.

Reported from the console: "I kicked off the Minor Feature workflow — why didn't it run?" Two
things stood between the picker and a workflow run:

  * the feedback endpoint never passed `workflow_version_id` on, so every change ran inline
    whatever was chosen — the picker had never worked;
  * a project born from a panel carries that panel's workflow run in `origin`, and the change
    inherited it, so even a correctly passed choice would have been recorded and not started.

Inline stays the default: no workflow picked, no workflow run.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from apps.api.app.config import get_settings


def _completed_run(engine, run_id, *, origin_workflow_run=None):
    settings = get_settings()
    spec = {"instructions": "Build a scorecard.", "acceptance_criteria": ["it scores"],
            "test_commands": ["python3 -m pytest"], "owner": "acme", "repo": "widget",
            "base_branch": "main"}
    fctx = {"attempt": 1, **({"origin": {"workflow_run_id": origin_workflow_run}}
                             if origin_workflow_run else {})}
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO code_task_runs (id,project_id,status,phase,title,pull_request_url,"
            "spec,state,finalize_ctx) VALUES (:id,:p,'completed','done','Scorecard',:pr,"
            "CAST(:spec AS JSONB),'{}'::jsonb,CAST(:fctx AS JSONB))"
        ), {"id": run_id, "p": settings.default_project_id, "pr": f"https://example.test/{run_id}",
            "spec": json.dumps(spec), "fctx": json.dumps(fctx)})
    return settings.default_project_id


def _minor_feature_version(client, engine, project_id):
    client.post("/v1/delivery-workflows/catalogue/install",
                json={"project_id": project_id, "actor": "ceo"})
    with engine.begin() as connection:
        return connection.execute(text(
            "SELECT d.active_version_id FROM workflow_definitions d WHERE d.project_id=:p "
            "AND d.workflow_key='minor_feature' AND d.active_version_id IS NOT NULL"
        ), {"p": project_id}).scalar()


def _workflow_runs(engine):
    with engine.begin() as connection:
        return connection.execute(text("SELECT count(*) FROM workflow_runs")).scalar_one()


CHANGE = {"message": "On the signed-out screen there are no buttons; add a way to open the "
                     "scoring link.", "expected": "A field and button open the link.",
          "anchor": "/scorecard"}


def test_a_picked_workflow_starts_that_workflow_even_on_a_panel_born_project(client, engine):
    project_id = _completed_run(engine, "crun_wf_pick", origin_workflow_run="wfr_the_panel")
    version = _minor_feature_version(client, engine, project_id)
    assert version, "the Minor Feature workflow should be installable in the test project"

    proposed = client.post("/v1/code-tasks/crun_wf_pick/feedback",
                           json={**CHANGE, "workflow_version_id": version})
    assert proposed.status_code == 200, proposed.text
    proposal = proposed.json()["proposal"]
    assert proposal["path"] == "workflow"
    assert proposal["workflow_name"] == "Minor Feature"

    before = _workflow_runs(engine)
    confirmed = client.post(f"/v1/feedback-threads/{proposed.json()['thread_id']}/confirm",
                            json={"actor": "ceo"}).json()
    assert confirmed["path"] == "workflow"
    assert confirmed["workflow_run_id"] and confirmed["workflow_run_id"] != "wfr_the_panel"
    assert _workflow_runs(engine) == before + 1
    with engine.begin() as connection:
        governed = connection.execute(text(
            "SELECT d.workflow_key FROM workflow_runs r JOIN workflow_definitions d "
            "ON d.id=r.workflow_definition_id WHERE r.id=:id"
        ), {"id": confirmed["workflow_run_id"]}).scalar()
    assert governed == "minor_feature"


def test_no_workflow_picked_means_an_inline_change_and_no_workflow_run(client, engine):
    _completed_run(engine, "crun_inline")
    proposed = client.post("/v1/code-tasks/crun_inline/feedback", json=CHANGE)
    assert proposed.json()["proposal"]["path"] == "inline"

    before = _workflow_runs(engine)
    confirmed = client.post(f"/v1/feedback-threads/{proposed.json()['thread_id']}/confirm",
                            json={"actor": "ceo"}).json()
    assert confirmed["path"] == "inline" and confirmed["dispatched"] is True
    assert "workflow_run_id" not in confirmed
    assert _workflow_runs(engine) == before


def test_a_workflow_that_is_not_in_the_project_is_refused_not_run_inline(client, engine):
    _completed_run(engine, "crun_bad_wf")
    refused = client.post("/v1/code-tasks/crun_bad_wf/feedback",
                          json={**CHANGE, "workflow_version_id": "wfv_not_here"})
    assert refused.status_code == 422
    assert "not in this project" in refused.json()["detail"]
