"""High-severity regressions: unauthenticated archive, enhancement base branch, plan scope."""
from __future__ import annotations

import json

from sqlalchemy import text

from apps.api.app.config import get_settings
from apps.api.app.services.intent_router import IntentRouter


def test_archive_task_requires_authorized_actor(client, engine):
    project_id = get_settings().default_project_id
    task_id = "task_archive_auth_victim"
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    id, project_id, title, description, status, priority, risk_tier,
                    data_boundary, dedupe_key, owner_role_id
                )
                VALUES (
                    :id, :project_id, 'Keep me', 'must not archive without authz',
                    'todo', 'p1', 1, 'internal', :dedupe, 'program_manager'
                )
                ON CONFLICT (id) DO UPDATE SET status = 'todo'
                """
            ),
            {"id": task_id, "project_id": project_id, "dedupe": f"dedupe:{task_id}"},
        )

    missing_actor = client.delete(f"/v1/tasks/{task_id}?project_id={project_id}")
    assert missing_actor.status_code == 422, missing_actor.text

    stranger = client.delete(
        f"/v1/tasks/{task_id}?project_id={project_id}&actor=not_a_member"
    )
    assert stranger.status_code == 403, stranger.text

    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT status FROM tasks WHERE id = :id"),
            {"id": task_id},
        ).scalar_one() == "todo"

    ok = client.delete(
        f"/v1/tasks/{task_id}?project_id={project_id}&actor=executive_console"
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "archived"
    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT status FROM tasks WHERE id = :id"),
            {"id": task_id},
        ).scalar_one() == "archived"
        assert connection.execute(
            text(
                "SELECT count(*) FROM events WHERE entity_id = :id "
                "AND event_type = 'task.archived'"
            ),
            {"id": task_id},
        ).scalar_one() >= 1


def test_plan_dispatch_rejects_plan_from_another_project(client, engine):
    project_id = get_settings().default_project_id
    victim_project_id = "boss-plan-dispatch-victim"
    plan_id = "plan_cross_project_victim"
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES (:id, 'ws_boss', 'Plan dispatch victim', :id)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"id": victim_project_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO plans (
                    id, project_id, title, objective, phases, created_by
                )
                VALUES (
                    :id, :project_id, 'Victim plan', 'Must remain isolated',
                    '[{"title": "Victim phase"}]'::jsonb, 'victim'
                )
                ON CONFLICT (id) DO UPDATE
                SET project_id = EXCLUDED.project_id, status = 'draft',
                    phases = EXCLUDED.phases
                """
            ),
            {"id": plan_id, "project_id": victim_project_id},
        )

    response = client.post(
        f"/v1/plans/{plan_id}/dispatch",
        json={"project_id": project_id, "actor": "executive_console"},
    )

    assert response.status_code == 409, response.text
    with engine.begin() as connection:
        assert connection.execute(
            text(
                "SELECT status FROM plans "
                "WHERE id = :id AND project_id = :project_id"
            ),
            {"id": plan_id, "project_id": victim_project_id},
        ).scalar_one() == "draft"
        assert connection.execute(
            text("SELECT count(*) FROM tasks WHERE project_id = :project_id"),
            {"project_id": victim_project_id},
        ).scalar_one() == 0


def test_existing_app_for_task_falls_back_to_run_id_when_root_missing(engine):
    """First builds omit root_run_id; enhancements must still extend boss/code/{run_id}."""
    project_id = get_settings().default_project_id
    task_id = "task_enhance_base_branch"
    run_id = "crun_enhance_base_branch"
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    id, project_id, title, description, status, priority, risk_tier,
                    data_boundary, dedupe_key, owner_role_id
                )
                VALUES (
                    :id, :project_id, 'App task', 'built once',
                    'todo', 'p1', 1, 'internal', :dedupe, 'program_manager'
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"id": task_id, "project_id": project_id, "dedupe": f"dedupe:{task_id}"},
        )
        connection.execute(
            text(
                """
                INSERT INTO code_task_runs (
                    id, project_id, task_id, title, status, phase, pull_request_url,
                    finalize_ctx, created_at, updated_at
                )
                VALUES (
                    :id, :project_id, :task_id, 'Initial build', 'completed', 'done',
                    'https://github.com/acme/demo/pull/1',
                    CAST(:fc AS jsonb), now(), now()
                )
                ON CONFLICT (id) DO UPDATE
                SET status = 'completed',
                    task_id = EXCLUDED.task_id,
                    pull_request_url = EXCLUDED.pull_request_url,
                    finalize_ctx = EXCLUDED.finalize_ctx
                """
            ),
            {
                "id": run_id,
                "project_id": project_id,
                "task_id": task_id,
                "fc": json.dumps({"source": "orchestration_agent", "actor": "executive_console"}),
            },
        )
        router = IntentRouter(get_settings())
        branch, root = router._existing_app_for_task(connection, project_id, task_id)

    assert root == run_id
    assert branch == f"boss/code/{run_id}"
