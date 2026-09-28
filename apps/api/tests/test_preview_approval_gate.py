"""Approving a preview must actually release the build: it approves the run's
`review_pull_request` merge decision, which (policy permitting) queues the real
GitHub merge job. This exercises the /v1/previews/{id}/gate and
/v1/previews/{id}/approve seam in engine/boss_engine/services/preview_approval.py end
to end, rather than just the decision endpoint it delegates to.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from apps.api.app import main as api_main


def _insert_preview(engine, *, preview_id, run_id=None, branch=None, repo=None,
                     status="running", project_id="boss-dogfood"):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO app_previews (
                    id, project_id, run_id, repo, branch, status, url, mode
                )
                VALUES (
                    :id, :project_id, :run_id, :repo, :branch, :status,
                    'http://127.0.0.1:4100/', 'process'
                )
                """
            ),
            {"id": preview_id, "project_id": project_id, "run_id": run_id,
             "repo": repo, "branch": branch, "status": status},
        )


def _insert_merge_decision(engine, *, decision_id, branch_name, pull_request_url,
                            test_report_status="passed", project_id="boss-dogfood",
                            status="pending"):
    artifact_id = f"art_{decision_id}"
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO artifacts (
                    id, project_id, task_id, artifact_type, title, uri, content, content_hash, created_by_job_id
                )
                VALUES (
                    :id, :project_id, NULL, 'test_report', 'Test report', NULL,
                    CAST(:content AS JSONB), 'hash-test', NULL
                )
                """
            ),
            {"id": artifact_id, "project_id": project_id,
             "content": json.dumps({"status": test_report_status,
                                     "summary": "GitHub validation passed: CI.",
                                     "tests_added_or_run": ["CI"]})},
        )
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, task_id, question, recommendation, options, risk_tier,
                    cost_summary, default_action, status
                )
                VALUES (
                    :id, :project_id, NULL, 'Review and approve sandbox PR',
                    CAST(:recommendation AS JSONB), '[]'::jsonb, 2, '{}'::jsonb, 'hold', :status
                )
                """
            ),
            {"id": decision_id, "project_id": project_id, "status": status,
             "recommendation": json.dumps({
                 "action": "review_pull_request",
                 "test_report_artifact_id": artifact_id,
                 "pull_request_url": pull_request_url,
                 "branch_name": branch_name,
                 "title": "Preview approval build",
             })},
        )


def _insert_deployment_target(engine, *, target_id, repo, app_name="web",
                               auto_deploy=True, paused=False, project_id="boss-dogfood"):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO deployment_targets (
                    id, project_id, app_name, repo, environment, target_kind, target_ref,
                    status, created_by, auto_deploy, paused
                )
                VALUES (
                    :id, :project_id, :app_name, :repo, 'production', 'host', 'irrelevant-ref',
                    'active', 'test', :auto_deploy, :paused
                )
                """
            ),
            {"id": target_id, "project_id": project_id, "app_name": app_name, "repo": repo,
             "auto_deploy": auto_deploy, "paused": paused},
        )


def test_preview_gate_404_for_missing_preview(client):
    resp = client.get("/v1/previews/prev_does_not_exist/gate")
    assert resp.status_code == 404


def test_preview_gate_blocks_when_not_linked_to_a_run(client, engine):
    _insert_preview(engine, preview_id="prev_unlinked", run_id=None, branch=None,
                     repo="acme/demo-app")
    resp = client.get("/v1/previews/prev_unlinked/gate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["can_approve"] is False
    assert any("not linked to a code task run" in b for b in body["blockers"])


def test_approve_preview_releases_the_build(client, engine):
    """The full seam: approving the preview approves the merge decision AND, with the
    merge policy enabled, actually queues the GitHub merge job — the build is released,
    not just the decision recorded."""
    previous_merge = api_main.decisions.settings.enable_sandbox_merge
    api_main.decisions.settings.enable_sandbox_merge = True
    try:
        branch = "boss/demo-preview-release"
        _insert_preview(engine, preview_id="prev_release", run_id="crun_release",
                         branch=branch, repo="acme/demo-app")
        _insert_merge_decision(
            engine, decision_id="dec_preview_release", branch_name=branch,
            pull_request_url="https://github.com/seanheiney/boss-sandbox/pull/77",
        )
        _insert_deployment_target(engine, target_id="dep_target_release", repo="demo-app")

        gate = client.get("/v1/previews/prev_release/gate")
        assert gate.status_code == 200
        gate_body = gate.json()
        assert gate_body["can_approve"] is True
        assert gate_body["decision_id"] == "dec_preview_release"
        assert gate_body["will_deploy"] == ["web"]

        approve = client.post(
            "/v1/previews/prev_release/approve",
            json={"actor": "ceo", "note": "Ship it."},
        )
        assert approve.status_code == 200, approve.text
        body = approve.json()
        assert body["decision_id"] == "dec_preview_release"
        assert body["decision"]["status"] == "approved"
        assert body["app_targets"][0]["app_name"] == "web"

        with engine.begin() as connection:
            decision_status = connection.execute(
                text("SELECT status FROM decisions WHERE id = 'dec_preview_release'")
            ).scalar_one()
            merge_jobs = connection.execute(
                text(
                    """
                    SELECT status, payload FROM jobs
                    WHERE project_id = 'boss-dogfood' AND kind = 'github.pull_request.merge'
                    ORDER BY created_at DESC LIMIT 1
                    """
                )
            ).mappings().all()

        assert decision_status == "approved"
        assert len(merge_jobs) == 1
        assert merge_jobs[0]["status"] == "queued"
        assert merge_jobs[0]["payload"]["decision_id"] == "dec_preview_release"
        assert merge_jobs[0]["payload"]["pull_request_number"] == 77

        events = client.get("/v1/events", params={"project_id": "boss-dogfood"})
        assert events.status_code == 200
        event_types = [item["event_type"] for item in events.json()["items"]]
        assert "preview.approved" in event_types
    finally:
        api_main.decisions.settings.enable_sandbox_merge = previous_merge


def test_approve_preview_is_refused_once_the_merge_decision_is_resolved(client, engine):
    branch = "boss/demo-preview-already-approved"
    _insert_preview(engine, preview_id="prev_already", run_id="crun_already",
                     branch=branch, repo="acme/demo-app")
    _insert_merge_decision(
        engine, decision_id="dec_preview_already", branch_name=branch,
        pull_request_url="https://github.com/seanheiney/boss-sandbox/pull/78",
        status="approved",
    )

    resp = client.post(
        "/v1/previews/prev_already/approve",
        json={"actor": "ceo"},
    )
    assert resp.status_code == 409
    assert "approved" in resp.json()["detail"]


def test_approve_preview_requires_actor(client, engine):
    _insert_preview(engine, preview_id="prev_no_actor", run_id="crun_no_actor",
                     branch="boss/demo-preview-no-actor", repo="acme/demo-app")
    resp = client.post("/v1/previews/prev_no_actor/approve", json={})
    assert resp.status_code == 422
