"""Preview effects and run metadata require the stored project's authority."""

import json
from uuid import uuid4

from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.app.services import preview_service


def test_preview_smoke_approval_and_run_metadata_reject_unauthorized_effects(
    client, engine, monkeypatch,
):
    suffix = uuid4().hex[:10]
    home = "boss-dogfood"
    foreign = f"write_foreign_{suffix}"
    reader = f"write_reader_{suffix}"
    key = f"write-key-{suffix}"
    home_run = f"crun_home_{suffix}"
    foreign_run = f"crun_foreign_{suffix}"
    home_preview = f"prev_home_{suffix}"
    foreign_preview = f"prev_foreign_{suffix}"
    decision_id = f"dec_preview_{suffix}"
    branch = f"boss/code/{home_run}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{key}:{reader}")
    monkeypatch.setattr(api_main.settings, "worker_principal", "ga_worker")
    headers = {"Authorization": f"Bearer {key}"}

    with engine.begin() as connection:
        workspace_id = connection.execute(
            text("SELECT workspace_id FROM projects WHERE id=:id"), {"id": home},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) "
                 "VALUES (:id, :workspace_id, 'Foreign write test', :id)"),
            {"id": foreign, "workspace_id": workspace_id},
        )
        api_main.members.upsert_member(
            connection, project_id=home, member_id=reader,
            display_name="Read only", member_type="human", role_keys=[],
            permissions=[], actor="ceo",
        )
        for run_id, project_id in ((home_run, home), (foreign_run, foreign)):
            connection.execute(
                text("INSERT INTO code_task_runs (id, project_id, title, status, phase) "
                     "VALUES (:id, :project_id, 'Private run', 'completed', 'done')"),
                {"id": run_id, "project_id": project_id},
            )
        for preview_id, project_id, run_id, preview_branch in (
            (home_preview, home, home_run, branch),
            (foreign_preview, foreign, foreign_run, f"boss/code/{foreign_run}"),
        ):
            connection.execute(
                text("INSERT INTO app_previews "
                     "(id, project_id, run_id, status, mode, url, branch) "
                     "VALUES (:id, :project_id, :run_id, 'running', 'process', "
                     "'http://127.0.0.1:9999', :branch)"),
                {"id": preview_id, "project_id": project_id,
                 "run_id": run_id, "branch": preview_branch},
            )
        connection.execute(
            text("INSERT INTO decisions "
                 "(id, project_id, question, recommendation, options, risk_tier, "
                 "cost_summary, default_action, status) "
                 "VALUES (:id, :project_id, 'Review this pull request', "
                 "CAST(:recommendation AS JSONB), '[]'::jsonb, 1, '{}'::jsonb, "
                 "'hold', 'pending')"),
            {"id": decision_id, "project_id": home,
             "recommendation": json.dumps({"action": "review_pull_request",
                                           "branch_name": branch})},
        )

    called = []
    monkeypatch.setattr(preview_service, "smoke_test",
                        lambda *args, **kwargs: called.append(args) or {"ok": True})

    mutation = client.post(
        f"/v1/previews/{home_preview}/smoke",
        json={"checks": [{"method": "DELETE", "path": "/data"}]}, headers=headers,
    )
    assert mutation.status_code == 422
    foreign_smoke = client.post(
        f"/v1/previews/{foreign_preview}/smoke",
        json={"checks": [{"method": "GET", "path": "/"}]}, headers=headers,
    )
    assert foreign_smoke.status_code == 403
    assert called == []
    assert client.post(
        f"/v1/previews/{home_preview}/smoke",
        json={"checks": [{"method": "GET", "path": "/"}]}, headers=headers,
    ).status_code == 200
    assert len(called) == 1

    denied_approval = client.post(
        f"/v1/previews/{home_preview}/approve",
        json={"actor": "ceo"}, headers=headers,
    )
    assert denied_approval.status_code == 403, denied_approval.text
    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM events WHERE entity_id=:id "
                 "AND event_type='preview.approved'"),
            {"id": home_preview},
        ).scalar_one() == 0
        assert connection.execute(
            text("SELECT status FROM decisions WHERE id=:id"),
            {"id": decision_id},
        ).scalar_one() == "pending"

    for run_id in (home_run, foreign_run):
        denied_origin = client.post(
            f"/v1/code-tasks/{run_id}/origin",
            json={"actor": reader, "kind": "discovery"}, headers=headers,
        )
        assert denied_origin.status_code == 403
        denied_replay = client.post(
            f"/v1/code-tasks/{run_id}/redelivered",
            json={"worker_id": "ga_worker", "job_id": "fake",
                  "lease_token": "fake", "attempt": 2}, headers=headers,
        )
        assert denied_replay.status_code == 403
    with engine.begin() as connection:
        rows = connection.execute(
            text("SELECT finalize_ctx FROM code_task_runs WHERE id IN (:home, :foreign)"),
            {"home": home_run, "foreign": foreign_run},
        ).scalars().all()
        assert all(not (row or {}).get("origin") and
                   not (row or {}).get("redelivered_attempts") for row in rows)
