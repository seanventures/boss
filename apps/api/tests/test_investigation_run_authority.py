"""Investigation execution is limited to the worker for its stored project."""

from uuid import uuid4

from boss_engine.services import debug_session
from sqlalchemy import text

from apps.api.app import main as api_main


PROJECT = "boss-dogfood"


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_investigation_run_requires_worker_and_stored_project_grant(
    client, engine, monkeypatch,
):
    worker = "ga_investigation_worker"
    low = "ga_investigation_viewer"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "boss_edition", "enterprise")
    monkeypatch.setattr(
        api_main.settings, "api_keys", "invest-worker-key,invest-low-key,invest-ceo-key"
    )
    monkeypatch.setattr(
        api_main.settings, "api_key_principals",
        f"invest-worker-key:{worker},invest-low-key:{low},invest-ceo-key:ceo",
    )
    monkeypatch.setattr(api_main.settings, "worker_principal", worker)

    local_id = f"inv_ga_{uuid4().hex[:12]}"
    foreign_id = f"inv_ga_{uuid4().hex[:12]}"
    other_project = f"ga-other-{uuid4().hex[:8]}"
    with engine.begin() as connection:
        workspace_id = connection.execute(
            text("SELECT workspace_id FROM projects WHERE id = :project"),
            {"project": PROJECT},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) "
                 "VALUES (:id, :workspace, 'Other', :id)"),
            {"id": other_project, "workspace": workspace_id},
        )
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=worker,
            display_name="Investigation worker", member_type="service", role_keys=[],
            permissions=["workflow_execute"], actor="ceo",
        )
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=low,
            display_name="Investigation viewer", member_type="human", role_keys=[],
            permissions=["audit_export"], actor="ceo",
        )
        connection.execute(
            text("INSERT INTO investigations (id, project_id, question, actor, verdict) "
                 "VALUES (:local, :project, 'local question', 'ceo', '{}'::jsonb), "
                 "(:foreign, :other_project, 'foreign question', 'ceo', "
                 "'{\"cause\":\"private-foreign-verdict\"}'::jsonb)"),
            {"local": local_id, "project": PROJECT,
             "foreign": foreign_id, "other_project": other_project},
        )

    calls = []
    sentinel = "private-investigation-result"

    def fake_run(_engine, _settings, *, investigation_id):
        calls.append(investigation_id)
        return {"ok": True, "summary": sentinel, "verdict": {"cause": sentinel}}

    monkeypatch.setattr(debug_session, "run_investigation", fake_run)
    try:
        local_list = client.get(
            "/v1/investigations", headers=_auth("invest-low-key"),
            params={"project_id": PROJECT},
        )
        assert local_list.status_code == 200, local_list.text
        assert local_id in {item["id"] for item in local_list.json()["investigations"]}

        foreign_list = client.get(
            "/v1/investigations", headers=_auth("invest-low-key"),
            params={"project_id": other_project},
        )
        assert foreign_list.status_code == 403, foreign_list.text
        assert "foreign question" not in foreign_list.text
        assert "private-foreign-verdict" not in foreign_list.text

        ceo_list = client.get(
            "/v1/investigations", headers=_auth("invest-ceo-key"),
            params={"project_id": PROJECT},
        )
        assert ceo_list.status_code == 200, ceo_list.text
        assert local_id in {item["id"] for item in ceo_list.json()["investigations"]}

        for key, investigation_id in (
            ("invest-low-key", local_id),
            ("invest-low-key", foreign_id),
            ("invest-low-key", "inv_forged_missing"),
            ("invest-worker-key", foreign_id),
        ):
            denied = client.post(
                f"/v1/investigations/{investigation_id}/run",
                headers=_auth(key), json={},
            )
            assert denied.status_code == 403, denied.text
            assert sentinel not in denied.text
        assert calls == []

        missing = client.post(
            "/v1/investigations/inv_forged_missing/run",
            headers=_auth("invest-worker-key"), json={},
        )
        assert missing.status_code == 200, missing.text
        assert missing.json()["ok"] is False
        assert calls == []

        allowed = client.post(
            f"/v1/investigations/{local_id}/run",
            headers=_auth("invest-worker-key"), json={},
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["summary"] == sentinel
        assert calls == [local_id]
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM investigations WHERE id IN (:local, :foreign)"),
                {"local": local_id, "foreign": foreign_id},
            )
            connection.execute(
                text("DELETE FROM events WHERE project_id = :project"),
                {"project": other_project},
            )
            connection.execute(
                text("DELETE FROM project_members WHERE project_id = :project"),
                {"project": other_project},
            )
            connection.execute(
                text("DELETE FROM projects WHERE id = :project"),
                {"project": other_project},
            )
