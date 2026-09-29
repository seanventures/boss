"""Direct deployments reads stay bounded and scoped during Factory fan-out."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from time import monotonic
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from apps.api.app import main as api_main


PROJECT = "boss-dogfood"


def _insert_run(connection, run_id: str, project_id: str) -> None:
    connection.execute(
        text("INSERT INTO code_task_runs (id, project_id, title) "
             "VALUES (:id, :project_id, 'Deployment admission test')"),
        {"id": run_id, "project_id": project_id},
    )


def test_concurrent_direct_reads_return_fast_503_and_recover(
    client, engine, monkeypatch,
):
    run_id = f"ctr_admit_{uuid4().hex[:12]}"
    with engine.begin() as connection:
        _insert_run(connection, run_id, PROJECT)

    original_reader = api_main.require_code_task_reader
    entered = Event()
    release = Event()
    lock = Lock()
    active = 0

    def held_reader(connection, requested_run_id):
        nonlocal active
        project_id = original_reader(connection, requested_run_id)
        with lock:
            active += 1
            if active == api_main._CODE_TASK_DEPLOYMENT_READ_SLOTS:
                entered.set()
        if not release.wait(timeout=10):
            raise AssertionError("test did not release admitted deployment reads")
        return project_id

    monkeypatch.setattr(api_main, "require_code_task_reader", held_reader)
    monkeypatch.setattr(
        api_main, "database_is_reachable",
        lambda _engine: (_ for _ in ()).throw(AssertionError("redundant DB preflight")),
    )
    path = f"/v1/code-tasks/{run_id}/deployments"
    try:
        with ThreadPoolExecutor(max_workers=api_main._CODE_TASK_DEPLOYMENT_READ_SLOTS) as pool:
            futures = [pool.submit(client.get, path) for _ in range(
                api_main._CODE_TASK_DEPLOYMENT_READ_SLOTS
            )]
            try:
                assert entered.wait(timeout=5), "read slots were not filled"
                start = monotonic()
                overloaded = client.get(path)
                elapsed = monotonic() - start
                assert overloaded.status_code == 503
                assert overloaded.json() == {
                    "detail": "Deployment reads are busy; retry shortly."
                }
                assert overloaded.headers["retry-after"] == "1"
                assert elapsed < 1, f"overload waited {elapsed:.2f}s"
            finally:
                release.set()
            for future in futures:
                response = future.result(timeout=5)
                assert response.status_code == 200, response.text
                assert response.json() == {
                    "run_id": run_id, "deployments": [],
                    "linked_host": None, "linked_target_id": None,
                }
        recovered = client.get(path)
        assert recovered.status_code == 200, recovered.text
    finally:
        release.set()
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM code_task_runs WHERE id = :id"), {"id": run_id}
            )


def test_direct_deployment_read_keeps_project_membership_and_join_scope(
    client, engine, monkeypatch,
):
    suffix = uuid4().hex[:12]
    home_run, foreign_run = f"ctr_home_{suffix}", f"ctr_other_{suffix}"
    home_target, foreign_target = f"dt_home_{suffix}", f"dt_other_{suffix}"
    home_deploy, foreign_deploy = f"dr_home_{suffix}", f"dr_other_{suffix}"
    foreign_project = f"project_other_{suffix}"
    member, key = f"reader_{suffix}", f"read-key-{suffix}"
    app_name = "Same App"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{key}:{member}")
    headers = {"Authorization": f"Bearer {key}"}

    with engine.begin() as connection:
        workspace_id = connection.execute(
            text("SELECT workspace_id FROM projects WHERE id = :id"), {"id": PROJECT}
        ).scalar_one()
        connection.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) "
                 "VALUES (:id, :workspace_id, 'Other project', :id)"),
            {"id": foreign_project, "workspace_id": workspace_id},
        )
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=member,
            display_name="Deployment reader", member_type="human", role_keys=[],
            permissions=[], actor="ceo",
        )
        for run_id, project_id, target_id, deployment_id in (
            (home_run, PROJECT, home_target, home_deploy),
            (foreign_run, foreign_project, foreign_target, foreign_deploy),
        ):
            _insert_run(connection, run_id, project_id)
            connection.execute(
                text("INSERT INTO deployment_targets "
                     "(id, project_id, app_name, repo, environment, target_kind, "
                     "target_ref, created_by, status) "
                     "VALUES (:id, :project_id, :app_name, 'example/repo', 'dev', "
                     "'vm', 'host', 'ceo', 'active')"),
                {"id": target_id, "project_id": project_id, "app_name": app_name},
            )
            connection.execute(
                text("INSERT INTO deployment_runs "
                     "(id, project_id, deployment_target_id, app_name, environment, "
                     "repo, target_kind, target_ref, release_packet, created_by) "
                     "VALUES (:id, :project_id, :target_id, :app_name, 'dev', "
                     "'example/repo', 'vm', 'host', "
                     "jsonb_build_object('code_task_run_id', CAST(:run_id AS text)), 'ceo')"),
                {"id": deployment_id, "project_id": project_id,
                 "target_id": target_id, "app_name": app_name, "run_id": run_id},
            )

    try:
        home = client.get(
            f"/v1/code-tasks/{home_run}/deployments",
            params={"app_name": app_name}, headers=headers,
        )
        assert home.status_code == 200, home.text
        body = home.json()
        assert body["run_id"] == home_run
        assert [row["id"] for row in body["deployments"]] == [home_deploy]
        assert body["linked_target_id"] is None  # no host is bound to this target
        assert foreign_target not in home.text
        assert foreign_deploy not in home.text

        denied = client.get(
            f"/v1/code-tasks/{foreign_run}/deployments",
            params={"app_name": app_name}, headers=headers,
        )
        assert denied.status_code == 403, denied.text
        assert foreign_target not in denied.text
        assert foreign_deploy not in denied.text
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM deployment_runs WHERE id IN (:home, :foreign)"),
                {"home": home_deploy, "foreign": foreign_deploy},
            )
            connection.execute(
                text("DELETE FROM deployment_targets WHERE id IN (:home, :foreign)"),
                {"home": home_target, "foreign": foreign_target},
            )
            connection.execute(
                text("DELETE FROM code_task_runs WHERE id IN (:home, :foreign)"),
                {"home": home_run, "foreign": foreign_run},
            )
            connection.execute(
                text("DELETE FROM project_members WHERE project_id = :project_id"),
                {"project_id": foreign_project},
            )
            connection.execute(
                text("DELETE FROM events WHERE project_id = :project_id"),
                {"project_id": foreign_project},
            )
            connection.execute(
                text("DELETE FROM projects WHERE id = :id"),
                {"id": foreign_project},
            )


def test_database_connection_failure_is_503_and_releases_slot(
    client, engine, monkeypatch,
):
    run_id = f"ctr_db_failure_{uuid4().hex[:12]}"
    with engine.begin() as connection:
        _insert_run(connection, run_id, PROJECT)
    path = f"/v1/code-tasks/{run_id}/deployments"
    original_begin = api_main.begin

    def failed_begin(_engine):
        raise OperationalError("connect", {}, RuntimeError("test connection failure"))

    try:
        with monkeypatch.context() as patch:
            patch.setattr(api_main, "begin", failed_begin)
            failed = client.get(path)
            assert failed.status_code == 503, failed.text
            assert failed.json() == {"detail": "Postgres is not reachable."}
        assert api_main.begin is original_begin
        recovered = client.get(path)
        assert recovered.status_code == 200, recovered.text
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM code_task_runs WHERE id = :id"), {"id": run_id}
            )
