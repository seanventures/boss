"""Community investigations cannot spend or act through legacy execution paths."""

from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.config import get_settings
from boss_engine.context import EngineContext
from boss_engine.services import debug_session


PROJECT = "boss-dogfood"


def test_new_community_investigation_rejected_before_db_or_queue(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "boss_edition", "community")

    class NoDatabase:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("investigation reached the database")

    with pytest.raises(debug_session.InvestigationExecutionBlocked, match="reservations"):
        debug_session.open_investigation(
            NoDatabase(), project_id=PROJECT, question="Why?", actor="ceo"
        )


def test_community_worker_blocks_legacy_queued_and_running_rows(
    client, engine, monkeypatch,
):
    suffix = uuid4().hex[:10]
    worker = f"investigation_gate_worker_{suffix}"
    key = f"investigation-gate-key-{suffix}"
    queued = f"inv_queued_{suffix}"
    running = f"inv_running_{suffix}"
    monkeypatch.setattr(api_main.settings, "boss_edition", "community")
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{key}:{worker}")
    monkeypatch.setattr(api_main.settings, "worker_principal", worker)

    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=worker,
            display_name="Investigation gate worker", member_type="service",
            role_keys=[], permissions=["workflow_execute"], actor="ceo",
        )
        connection.execute(
            text("INSERT INTO investigations "
                 "(id, project_id, question, actor, status, token) VALUES "
                 "(:queued, :project, 'queued question', 'ceo', 'queued', 'queued-token'), "
                 "(:running, :project, 'running question', 'ceo', 'running', 'running-token')"),
            {"queued": queued, "running": running, "project": PROJECT},
        )

    def no_model(*_args, **_kwargs):
        raise AssertionError("Community investigation reached model execution")

    monkeypatch.setattr(debug_session, "run_investigation", no_model)
    try:
        for investigation_id in (queued, running):
            response = client.post(
                f"/v1/investigations/{investigation_id}/run",
                headers={"Authorization": f"Bearer {key}"}, json={},
            )
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "blocked"
        with engine.begin() as connection:
            rows = connection.execute(
                text("SELECT id, status, token, error FROM investigations "
                     "WHERE id IN (:queued, :running)"),
                {"queued": queued, "running": running},
            ).mappings().all()
            assert len(rows) == 2
            assert all(row["status"] == "failed" and row["token"] is None
                       and "reservations" in row["error"] for row in rows)
            assert connection.execute(
                text("SELECT count(*) FROM events WHERE entity_id IN (:queued, :running) "
                     "AND event_type = 'investigation.blocked'"),
                {"queued": queued, "running": running},
            ).scalar_one() == 2
            assert connection.execute(
                text("SELECT count(*) FROM decisions WHERE project_id = :project "
                     "AND question = :question AND status = 'pending'"),
                {"project": PROJECT,
                 "question": f"Reconcile interrupted investigation {running}"},
            ).scalar_one() == 1
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM decisions WHERE project_id = :project "
                     "AND question = :question"),
                {"project": PROJECT,
                 "question": f"Reconcile interrupted investigation {running}"},
            )
            connection.execute(
                text("DELETE FROM events WHERE entity_id IN (:queued, :running)"),
                {"queued": queued, "running": running},
            )
            connection.execute(text("DELETE FROM investigations WHERE id IN (:queued, :running)"),
                               {"queued": queued, "running": running})


def test_direct_community_worker_service_never_imports_model(engine, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "boss_edition", "oss")
    run_id = f"inv_direct_{uuid4().hex[:10]}"
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO investigations (id, project_id, question, actor) "
                 "VALUES (:id, :project, 'direct question', 'ceo')"),
            {"id": run_id, "project": PROJECT},
        )
    try:
        result = debug_session.run_investigation(engine, settings, investigation_id=run_id)
        assert result["status"] == "blocked"
    finally:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM investigations WHERE id = :id"), {"id": run_id})


@pytest.mark.parametrize("edition", ["community", "oss", "local", "unexpected"])
def test_legacy_callback_cannot_reach_host_or_db_write(edition, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "boss_edition", edition)
    host = debug_session.run_on_investigation_host(
        host="any", command="touch /tmp/should-not-exist",
        project_id=PROJECT, allow_write=True,
    )
    assert host["ok"] is False and "unavailable" in host["error"]

    class NoDatabaseWrite:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("Community investigation wrote to the database")

    record = {"id": "inv_legacy", "project_id": PROJECT,
              "actor": "ceo", "allow_fix": True}
    db = debug_session.run_db(
        NoDatabaseWrite(), record,
        "UPDATE deployment_targets SET status = 'retired' WHERE id = 'target_1'",
    )
    assert db["ok"] is False and "unavailable" in db["error"]
    operation = debug_session.run_operation(
        EngineContext(settings=settings, engine=None), record,
        "redeploy", {"run_id": "run_1"},
    )
    assert operation["ok"] is False and "unavailable" in operation["error"]


def test_legacy_callback_holds_read_only_query_without_project_policy(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "boss_edition", "local")
    record = {"id": "inv_legacy", "project_id": PROJECT,
              "actor": "ceo", "allow_fix": True}
    class NoDatabase:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("project-scoped SQL reached the database")

    result = debug_session.run_db(NoDatabase(), record, "SELECT 1 AS answer")
    assert result["ok"] is False
    assert "raw SQL is unavailable" in result["error"]
