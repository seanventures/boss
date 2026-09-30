"""Discovery details and spend paths stay within the caller's project."""

import json
from uuid import uuid4

from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.app.services import discovery_service


def test_discovery_reads_and_effects_require_stored_project_authority(
    client, engine, monkeypatch,
):
    suffix = uuid4().hex[:10]
    home = "boss-dogfood"
    foreign = f"discovery_foreign_{suffix}"
    reader = f"discovery_reader_{suffix}"
    key = f"discovery-key-{suffix}"
    home_run = f"drun_home_{suffix}"
    foreign_run = f"drun_foreign_{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{key}:{reader}")
    headers = {"Authorization": f"Bearer {key}"}

    with engine.begin() as connection:
        workspace_id = connection.execute(
            text("SELECT workspace_id FROM projects WHERE id=:id"), {"id": home},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) "
                 "VALUES (:id, :workspace_id, 'Foreign discovery', :id)"),
            {"id": foreign, "workspace_id": workspace_id},
        )
        api_main.members.upsert_member(
            connection, project_id=home, member_id=reader,
            display_name="Discovery reader", member_type="human", role_keys=[],
            permissions=[], actor="ceo",
        )
        for run_id, project_id in ((home_run, home), (foreign_run, foreign)):
            connection.execute(
                text("INSERT INTO discovery_runs "
                     "(id, project_id, status, phase, title, seed, state) "
                     "VALUES (:id, :project_id, 'completed', 'done', 'Private dossier', "
                     "'private seed', CAST(:state AS JSONB))"),
                {"id": run_id, "project_id": project_id,
                 "state": json.dumps({"candidate_outcomes": [{"title": "Private"}]})},
            )

    starts = []
    monkeypatch.setattr(discovery_service, "start_discovery",
                        lambda **kwargs: starts.append(kwargs) or "should_not_start")
    assert client.get(f"/v1/discoveries/{home_run}", headers=headers).status_code == 200
    assert client.get(f"/v1/discoveries/{foreign_run}", headers=headers).status_code == 403
    for project_id in (home, foreign):
        assert client.post(
            "/v1/discoveries", json={"project_id": project_id, "seed": "test"},
            headers=headers,
        ).status_code == 403
    for run_id in (home_run, foreign_run):
        assert client.post(
            f"/v1/discoveries/{run_id}/deepen", headers=headers,
        ).status_code == 403
        assert client.post(
            f"/v1/discoveries/{run_id}/promote",
            json={"outcome_index": 0}, headers=headers,
        ).status_code == 403
    assert starts == []


def test_denied_discovery_promotion_records_policy_and_creates_no_work(
    client, engine, monkeypatch,
):
    suffix = uuid4().hex[:10]
    run_id = f"drun_budget_{suffix}"
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO discovery_runs "
                 "(id, project_id, status, phase, title, seed, state) "
                 "VALUES (:id, 'boss-dogfood', 'completed', 'done', 'Budget test', "
                 "'test', CAST(:state AS JSONB))"),
            {"id": run_id,
             "state": json.dumps({"candidate_outcomes": [{"outcome": "Build a test"}]})},
        )

    def deny_reservation(connection, project_id, *args, **kwargs):
        api_main.emit_event(
            connection, project_id, "budget.reservation_denied", "budget_governor",
            "discovery_run", run_id, {"reason": "test cap"},
        )
        return {"status": "denied", "message": "Budget cap exceeded"}

    monkeypatch.setattr(api_main.budget, "reserve", deny_reservation)
    response = client.post(
        f"/v1/discoveries/{run_id}/promote", json={"outcome_index": 0},
    )
    assert response.status_code == 409, response.text
    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM events WHERE entity_id=:id "
                 "AND event_type='budget.reservation_denied'"),
            {"id": run_id},
        ).scalar_one() == 1
        assert connection.execute(
            text("SELECT count(*) FROM tasks "
                 "WHERE inputs->>'discovery_run_id'=:id"),
            {"id": run_id},
        ).scalar_one() == 0
