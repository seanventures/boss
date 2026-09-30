"""Agent read routes use the stored project and the caller's chat grant."""

from uuid import uuid4

from boss_engine.services import factory_briefing
from sqlalchemy import text

from apps.api.app import main as api_main


PROJECT = "boss-dogfood"


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_agent_briefing_progress_and_ledger_reads_are_project_scoped(
    client, engine, monkeypatch,
):
    reader = "ga_agent_reader"
    low = "ga_agent_low"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(
        api_main.settings, "api_keys", "agent-reader-key,agent-low-key,agent-ceo-key"
    )
    monkeypatch.setattr(
        api_main.settings, "api_key_principals",
        f"agent-reader-key:{reader},agent-low-key:{low},agent-ceo-key:ceo",
    )

    other_project = f"ga-other-{uuid4().hex[:8]}"
    local_thread = f"athr_ga_{uuid4().hex[:10]}"
    foreign_thread = f"athr_ga_{uuid4().hex[:10]}"
    local_investigation = f"inv_ga_{uuid4().hex[:10]}"
    foreign_investigation = f"inv_ga_{uuid4().hex[:10]}"
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
            connection, project_id=PROJECT, member_id=reader,
            display_name="Agent reader", member_type="human", role_keys=[],
            permissions=["agent_chat"], actor="ceo",
        )
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=low,
            display_name="Low grant member", member_type="human", role_keys=[],
            permissions=["health_refresh"], actor="ceo",
        )
        connection.execute(
            text("INSERT INTO agent_threads (id, project_id, actor) "
                 "VALUES (:local_thread, :project, 'ceo'), "
                 "(:foreign_thread, :other_project, 'ceo')"),
            {"local_thread": local_thread, "project": PROJECT,
             "foreign_thread": foreign_thread, "other_project": other_project},
        )
        connection.execute(
            text("INSERT INTO agent_session_ledger (thread_id, project_id, goal) "
                 "VALUES (:local_thread, :project, 'local-private-goal'), "
                 "(:foreign_thread, :other_project, 'foreign-private-goal')"),
            {"local_thread": local_thread, "project": PROJECT,
             "foreign_thread": foreign_thread, "other_project": other_project},
        )
        connection.execute(
            text("INSERT INTO agent_progress (thread_id, project_id, label) "
                 "VALUES (:local_thread, :project, 'local-private-progress'), "
                 "(:foreign_thread, :other_project, 'foreign-private-progress')"),
            {"local_thread": local_thread, "project": PROJECT,
             "foreign_thread": foreign_thread, "other_project": other_project},
        )
        connection.execute(
            text("INSERT INTO investigations "
                 "(id, project_id, thread_id, question, actor) "
                 "VALUES (:local_id, :project, :local_thread, 'local-private-question', 'ceo'), "
                 "(:foreign_id, :other_project, :foreign_thread, "
                 "'foreign-private-question', 'ceo')"),
            {"local_id": local_investigation, "project": PROJECT,
             "local_thread": local_thread, "foreign_id": foreign_investigation,
             "other_project": other_project, "foreign_thread": foreign_thread},
        )

    briefing_calls = []

    def fake_briefing(_connection, project_id):
        briefing_calls.append(project_id)
        return {"project_id": project_id, "summary": "private-briefing"}

    monkeypatch.setattr(factory_briefing, "factory_briefing", fake_briefing)
    try:
        for key, project_id, thread_id in (
            ("agent-low-key", PROJECT, local_thread),
            ("agent-reader-key", other_project, foreign_thread),
        ):
            for path in (
                f"/v1/agent/briefing?project_id={project_id}",
                f"/v1/agent/threads/{thread_id}/progress",
                f"/v1/agent/threads/{thread_id}/ledger",
            ):
                denied = client.get(path, headers=_auth(key))
                assert denied.status_code == 403, (path, denied.text)
                assert "private-" not in denied.text
        assert briefing_calls == []

        brief = client.get(
            "/v1/agent/briefing", headers=_auth("agent-reader-key"),
            params={"project_id": PROJECT},
        )
        assert brief.status_code == 200, brief.text
        assert brief.json()["summary"] == "private-briefing"
        assert briefing_calls == [PROJECT]

        progress = client.get(
            f"/v1/agent/threads/{local_thread}/progress",
            headers=_auth("agent-reader-key"),
        )
        assert progress.status_code == 200, progress.text
        assert progress.json()["steps"][0]["label"] == "local-private-progress"
        assert progress.json()["investigation"]["question"] == "local-private-question"
        assert "foreign-private" not in progress.text

        ledger = client.get(
            f"/v1/agent/threads/{local_thread}/ledger",
            headers=_auth("agent-reader-key"),
        )
        assert ledger.status_code == 200, ledger.text
        assert ledger.json()["ledger"]["goal"] == "local-private-goal"

        for path in ("progress", "ledger"):
            missing = client.get(
                f"/v1/agent/threads/athr_forged_missing/{path}",
                headers=_auth("agent-reader-key"),
            )
            assert missing.status_code == 404, missing.text
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM investigations WHERE id IN (:local, :foreign)"),
                {"local": local_investigation, "foreign": foreign_investigation},
            )
            connection.execute(
                text("DELETE FROM agent_progress WHERE thread_id IN (:local, :foreign)"),
                {"local": local_thread, "foreign": foreign_thread},
            )
            connection.execute(
                text("DELETE FROM agent_session_ledger "
                     "WHERE thread_id IN (:local, :foreign)"),
                {"local": local_thread, "foreign": foreign_thread},
            )
            connection.execute(
                text("DELETE FROM agent_threads WHERE id IN (:local, :foreign)"),
                {"local": local_thread, "foreign": foreign_thread},
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
