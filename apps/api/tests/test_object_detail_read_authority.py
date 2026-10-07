"""Object IDs must resolve to a readable project before returning private details."""

import json
from uuid import uuid4

from sqlalchemy import text

from apps.api.app import main as api_main


def test_cross_project_object_reports_are_denied(client, engine, monkeypatch):
    suffix = uuid4().hex[:10]
    home = "boss-dogfood"
    foreign = f"read_foreign_{suffix}"
    reader = f"read_member_{suffix}"
    key = f"read-key-{suffix}"
    ids = {name: f"read_{name}_{suffix}" for name in (
        "task", "qa_run", "evaluation", "program", "target_staging",
        "target_production", "deployment", "changeset", "proposal", "promotion",
        "foreign_batch_run", "home_batch_run", "mixed_foreign_run", "mixed_home_run",
    )}
    foreign_batch = f"foreign_batch_{suffix}"
    home_batch = f"home_batch_{suffix}"
    mixed_batch = f"mixed_batch_{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{key}:{reader}")
    headers = {"Authorization": f"Bearer {key}"}

    with engine.begin() as connection:
        workspace_id = connection.execute(
            text("SELECT workspace_id FROM projects WHERE id = :id"), {"id": home}
        ).scalar_one()
        connection.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) "
                 "VALUES (:id, :workspace_id, 'Private read test', :id)"),
            {"id": foreign, "workspace_id": workspace_id},
        )
        api_main.members.upsert_member(
            connection, project_id=home, member_id=reader,
            display_name="Read test member", member_type="human", role_keys=[],
            permissions=[], actor="ceo",
        )
        connection.execute(
            text("INSERT INTO tasks (id, project_id, title, dedupe_key) "
                 "VALUES (:id, :project_id, 'Private task', :id)"),
            {"id": ids["task"], "project_id": foreign},
        )
        for name, project_id, batch_id, qa in (
            ("qa_run", foreign, None, True),
            ("foreign_batch_run", foreign, foreign_batch, False),
            ("home_batch_run", home, home_batch, False),
            ("mixed_foreign_run", foreign, mixed_batch, False),
            ("mixed_home_run", home, mixed_batch, False),
        ):
            finalize_ctx = {"qa_benchmark": True} if qa else {
                "bakeoff": {"batch_id": batch_id, "config": "test"}
            }
            connection.execute(
                text("INSERT INTO code_task_runs (id, project_id, title, finalize_ctx) "
                     "VALUES (:id, :project_id, :title, CAST(:ctx AS JSONB))"),
                {"id": ids[name], "project_id": project_id, "title": name,
                 "ctx": json.dumps(finalize_ctx)},
            )
        connection.execute(
            text("INSERT INTO qa_benchmark_evaluations "
                 "(id, project_id, code_task_run_id, benchmark_id, scorecard_version, "
                 "status, source_hash) "
                 "VALUES (:id, :project_id, :run_id, 'test', '1', 'not_ready', :id)"),
            {"id": ids["evaluation"], "project_id": foreign,
             "run_id": ids["qa_run"]},
        )
        connection.execute(
            text("INSERT INTO improvement_programs "
                 "(id, project_id, name, objective, benchmark_id, "
                 "baseline_evaluation_id, metric_contract_hash, created_by) "
                 "VALUES (:id, :project_id, 'Private program', 'test', 'test', "
                 ":evaluation_id, 'hash', 'test')"),
            {"id": ids["program"], "project_id": foreign,
             "evaluation_id": ids["evaluation"]},
        )
        for name, environment in (("target_staging", "staging"),
                                  ("target_production", "production")):
            connection.execute(
                text("INSERT INTO deployment_targets "
                     "(id, project_id, app_name, repo, environment, target_kind, "
                     "target_ref, created_by) "
                     "VALUES (:id, :project_id, 'private-app', 'private/repo', "
                     ":environment, 'test', 'test', 'test')"),
                {"id": ids[name], "project_id": foreign,
                 "environment": environment},
            )
        connection.execute(
            text("INSERT INTO deployment_runs "
                 "(id, project_id, deployment_target_id, app_name, environment, "
                 "repo, target_kind, target_ref, created_by) "
                 "VALUES (:id, :project_id, :target_id, 'private-app', 'staging', "
                 "'private/repo', 'test', 'test', 'test')"),
            {"id": ids["deployment"], "project_id": foreign,
             "target_id": ids["target_staging"]},
        )
        connection.execute(
            text("INSERT INTO changesets (id, project_id, title, summary, created_by) "
                 "VALUES (:id, :project_id, 'Private change', 'Private details', 'test')"),
            {"id": ids["changeset"], "project_id": foreign},
        )
        connection.execute(
            text("INSERT INTO self_improvement_proposals "
                 "(id, project_id, task_id, title, summary, change_ref, "
                 "diff_summary, created_by) "
                 "VALUES (:id, :project_id, :task_id, 'Private proposal', "
                 "'Private details', 'test', 'test', 'test')"),
            {"id": ids["proposal"], "project_id": foreign,
             "task_id": ids["task"]},
        )
        connection.execute(
            text("INSERT INTO deployment_promotions "
                 "(id, project_id, app_name, repo, commit_ref, staging_target_id, "
                 "production_target_id, pipeline_provider, created_by) "
                 "VALUES (:id, :project_id, 'private-app', 'private/repo', 'abc123', "
                 ":staging_id, :production_id, 'test', 'test')"),
            {"id": ids["promotion"], "project_id": foreign,
             "staging_id": ids["target_staging"],
             "production_id": ids["target_production"]},
        )

    try:
        paths = (
            f"/v1/qa/benchmark-apps/runs/{ids['qa_run']}/report",
            f"/v1/improvement-programs/{ids['program']}/trials",
            f"/v1/delivery/deployments/{ids['deployment']}",
            f"/v1/bakeoff/{foreign_batch}/report",
            f"/v1/self-improvement/proposals/{ids['proposal']}",
            f"/v1/changesets/{ids['changeset']}/terraform-export",
            f"/v1/deployment-promotions/{ids['promotion']}",
        )
        # A proposal id is a classified object kind (identity object_kinds): the central
        # confinement answers another project's object as not found, the identity convention
        # (§6.7, R3). The others are refused by their handlers' member check. Denied either way.
        not_found = {f"/v1/self-improvement/proposals/{ids['proposal']}"}
        for path in paths:
            response = client.get(path, headers=headers)
            assert response.status_code == (404 if path in not_found else 403), (path, response.text)
            assert foreign not in response.text

        mixed = client.get(f"/v1/bakeoff/{mixed_batch}/report", headers=headers)
        assert mixed.status_code == 404, mixed.text
        assert foreign not in mixed.text
        home_batch_report = client.get(
            f"/v1/bakeoff/{home_batch}/report", headers=headers
        )
        assert home_batch_report.status_code == 200, home_batch_report.text
        assert home_batch_report.json()["batch_id"] == home_batch
    finally:
        with engine.begin() as connection:
            for table, names in (
                ("deployment_promotions", ("promotion",)),
                ("deployment_runs", ("deployment",)),
                ("deployment_targets", ("target_staging", "target_production")),
                ("improvement_programs", ("program",)),
                ("qa_benchmark_evaluations", ("evaluation",)),
                ("self_improvement_proposals", ("proposal",)),
                ("changesets", ("changeset",)),
                ("code_task_runs", ("qa_run", "foreign_batch_run", "home_batch_run",
                                    "mixed_foreign_run", "mixed_home_run")),
                ("tasks", ("task",)),
            ):
                for name in names:
                    connection.execute(
                        text(f"DELETE FROM {table} WHERE id = :id"), {"id": ids[name]}
                    )
            connection.execute(
                text("DELETE FROM events WHERE project_id = :id"), {"id": foreign}
            )
            connection.execute(
                text("DELETE FROM projects WHERE id = :id"), {"id": foreign}
            )
