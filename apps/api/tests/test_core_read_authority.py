"""A project member cannot read another project's decisions or worker inventory."""

from uuid import uuid4

from sqlalchemy import text

from apps.api.app import main as api_main


PROJECT = "boss-dogfood"


def test_decisions_and_workers_reads_require_project_membership(client, engine, monkeypatch):
    reader = f"ga_read_{uuid4().hex[:10]}"
    key = f"ga-read-key-{uuid4().hex[:10]}"
    foreign_project = f"ga_foreign_{uuid4().hex[:10]}"
    home_decision = f"dec_ga_home_{uuid4().hex[:10]}"
    foreign_decision = f"dec_ga_foreign_{uuid4().hex[:10]}"
    home_worker = f"worker_ga_home_{uuid4().hex[:10]}"
    foreign_worker = f"worker_ga_foreign_{uuid4().hex[:10]}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{key}:{reader}")
    headers = {"Authorization": f"Bearer {key}"}

    with engine.begin() as connection:
        workspace_id = connection.execute(
            text("SELECT workspace_id FROM projects WHERE id=:project_id"),
            {"project_id": PROJECT},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) "
                 "VALUES (:id, :workspace_id, 'Foreign read test', :id)"),
            {"id": foreign_project, "workspace_id": workspace_id},
        )
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=reader,
            display_name="GA reader", member_type="human", role_keys=[],
            permissions=[], actor="ceo",
        )
        for decision_id, project_id, question in (
            (home_decision, PROJECT, "home-only-decision"),
            (foreign_decision, foreign_project, "foreign-private-decision"),
        ):
            connection.execute(
                text("INSERT INTO decisions (id, project_id, question) "
                     "VALUES (:id, :project_id, :question)"),
                {"id": decision_id, "project_id": project_id, "question": question},
            )
        for worker_id, project_id, label in (
            (home_worker, PROJECT, "home-only-worker"),
            (foreign_worker, foreign_project, "foreign-private-worker"),
        ):
            connection.execute(
                text("INSERT INTO worker_registrations "
                     "(id, project_id, worker_id, metadata) "
                     "VALUES (:id, :project_id, :worker_id, CAST(:metadata AS JSONB))"),
                {"id": worker_id, "project_id": project_id, "worker_id": worker_id,
                 "metadata": '{"label":"' + label + '"}'},
            )

    try:
        home_inbox = client.get(
            "/v1/decisions/inbox", params={"project_id": PROJECT}, headers=headers,
        )
        assert home_inbox.status_code == 200, home_inbox.text
        assert "home-only-decision" in home_inbox.text
        assert "foreign-private-decision" not in home_inbox.text

        home_detail = client.get(f"/v1/decisions/{home_decision}", headers=headers)
        assert home_detail.status_code == 200, home_detail.text
        assert home_detail.json()["question"] == "home-only-decision"

        home_workers = client.get(
            "/v1/workers", params={"project_id": PROJECT}, headers=headers,
        )
        assert home_workers.status_code == 200, home_workers.text
        assert "home-only-worker" in home_workers.text
        assert "foreign-private-worker" not in home_workers.text

        foreign_reads = (
            client.get("/v1/decisions/inbox",
                       params={"project_id": foreign_project}, headers=headers),
            client.get(f"/v1/decisions/{foreign_decision}", headers=headers),
            client.get(f"/v1/decisions/{foreign_decision}",
                       params={"project_id": foreign_project}, headers=headers),
            client.get("/v1/workers", params={"project_id": foreign_project},
                       headers=headers),
        )
        for response in foreign_reads:
            assert response.status_code == 403, response.text
            assert "foreign-private" not in response.text
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM worker_registrations WHERE id IN (:home, :foreign)"),
                {"home": home_worker, "foreign": foreign_worker},
            )
            connection.execute(
                text("DELETE FROM decisions WHERE id IN (:home, :foreign)"),
                {"home": home_decision, "foreign": foreign_decision},
            )
            connection.execute(
                text("DELETE FROM events WHERE project_id=:project_id"),
                {"project_id": foreign_project},
            )
            connection.execute(
                text("DELETE FROM projects WHERE id=:project_id"),
                {"project_id": foreign_project},
            )
