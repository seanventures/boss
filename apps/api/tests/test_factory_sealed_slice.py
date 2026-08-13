"""BOSS-351 sealed-slice honesty on GET /v1/factory/wip (no spend)."""

from __future__ import annotations

from sqlalchemy import text

from apps.api.app.db import begin


PID = "boss-dogfood"


def test_factory_wip_live_pr_receipts_report_k_of_n_not_a_percent(client, engine):
    with begin(engine) as connection:
        connection.execute(
            text(
                """
                INSERT INTO receipts (
                  id, org_id, workspace_id, project_id, dedupe_key, effect_kind,
                  effect_scope, payload_hash, status, result
                )
                SELECT :id, w.org_id, p.workspace_id, p.id, :dedupe,
                       'github.pull_request.create', '{}'::jsonb, :hash, :st,
                       CAST(:result AS jsonb)
                FROM projects p JOIN workspaces w ON w.id = p.workspace_id
                WHERE p.id = :project
                """
            ),
            {
                "id": "rcpt_live_pr_ok",
                "dedupe": "live-pr-ok",
                "hash": "a" * 64,
                "st": "succeeded",
                "result": '{"pr_url": "https://github.com/acme/app/pull/1"}',
                "project": PID,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO receipts (
                  id, org_id, workspace_id, project_id, dedupe_key, effect_kind,
                  effect_scope, payload_hash, status, result
                )
                SELECT :id, w.org_id, p.workspace_id, p.id, :dedupe,
                       'github.pull_request.create', '{}'::jsonb, :hash, :st,
                       '{}'::jsonb
                FROM projects p JOIN workspaces w ON w.id = p.workspace_id
                WHERE p.id = :project
                """
            ),
            {
                "id": "rcpt_live_pr_fail",
                "dedupe": "live-pr-fail",
                "hash": "b" * 64,
                "st": "failed",
                "project": PID,
            },
        )

    body = client.get("/v1/factory/wip", params={"project_id": PID}).json()
    live = body["live_pr_success"]
    assert live["sample_size"] == 2
    assert live["successes"] == 1
    assert live["rate"] == 0.5
    assert live["proven"] is False
    assert live["label"] == "1/2 from existing receipts (not a sealed weekly proof)"
    assert "%" not in live["label"]


def test_simulated_code_task_with_pr_url_does_not_count_as_live(client, engine):
    with begin(engine) as connection:
        connection.execute(
            text(
                """
                INSERT INTO code_task_runs (
                  id, project_id, status, phase, title, spec, usage, pull_request_url
                )
                VALUES (
                  :id, :p, 'simulated', 'fixture', 'fake pr fixture',
                  CAST(:spec AS jsonb), CAST(:usage AS jsonb),
                  'https://github.com/acme/app/pull/99'
                )
                """
            ),
            {
                "id": "crun_sim_pr",
                "p": PID,
                "spec": '{"simulated": true, "owner": "_fixture", "repo": "_fixture"}',
                "usage": '{"cost_usd_micros": 0}',
            },
        )

    body = client.get("/v1/factory/wip", params={"project_id": PID}).json()
    last = body["last_simulated_fixture"]
    assert last["run_id"] == "crun_sim_pr"
    assert last["status"] == "simulated"
    live = body["live_pr_success"]
    assert live["sample_size"] == 0
    assert live["rate"] is None
    assert live["label"] == "unknown / not proven"
