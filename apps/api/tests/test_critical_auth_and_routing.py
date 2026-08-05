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


def test_deploy_code_task_requires_infra_provision_actor(client, engine):
    """POST /v1/code-tasks/{run_id}/deploy must not run without project authorization."""
    project_id = get_settings().default_project_id
    run_id = "crun_deploy_auth_victim"
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO code_task_runs (
                    id, project_id, title, status, phase, created_at, updated_at
                )
                VALUES (
                    :id, :project_id, 'Deploy auth victim', 'completed', 'done',
                    now(), now()
                )
                ON CONFLICT (id) DO UPDATE
                SET project_id = EXCLUDED.project_id, status = 'completed'
                """
            ),
            {"id": run_id, "project_id": project_id},
        )

    missing_actor = client.post(f"/v1/code-tasks/{run_id}/deploy", json={"mode": "dry_run"})
    assert missing_actor.status_code == 422, missing_actor.text

    stranger = client.post(
        f"/v1/code-tasks/{run_id}/deploy",
        json={"mode": "dry_run", "actor": "not_a_member"},
    )
    assert stranger.status_code == 403, stranger.text

    # chat-only member must not get deploy plans either
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO project_members (
                    id, project_id, member_id, display_name, member_type,
                    role_keys, permissions, status, created_by
                )
                VALUES (
                    :id, :project_id, 'chat_only', 'Chat Only', 'human',
                    CAST(:roles AS jsonb), CAST(:perms AS jsonb), 'active', 'test'
                )
                ON CONFLICT (project_id, member_id) DO UPDATE
                SET permissions = EXCLUDED.permissions, status = 'active'
                """
            ),
            {
                "id": "pm_chat_only_deploy",
                "project_id": project_id,
                "roles": '["viewer"]',
                "perms": '["agent_chat"]',
            },
        )
    denied_chat = client.post(
        f"/v1/code-tasks/{run_id}/deploy",
        json={"mode": "dry_run", "actor": "chat_only"},
    )
    assert denied_chat.status_code == 403, denied_chat.text


def test_get_code_task_tool_is_project_scoped(engine):
    """get_code_task must not return another project's run by id alone."""
    from boss_engine.context import EngineContext
    from boss_engine.tools import build_default_registry

    home = get_settings().default_project_id
    victim = "boss-tool-idor-victim"
    run_id = "crun_tool_idor_victim"
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES (:id, 'ws_boss', 'IDOR victim', :id)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"id": victim},
        )
        connection.execute(
            text(
                """
                INSERT INTO code_task_runs (
                    id, project_id, title, status, phase, created_at, updated_at
                )
                VALUES (
                    :id, :project_id, 'Secret run', 'completed', 'done', now(), now()
                )
                ON CONFLICT (id) DO UPDATE
                SET project_id = EXCLUDED.project_id
                """
            ),
            {"id": run_id, "project_id": victim},
        )

    ctx = EngineContext.create()
    reg = build_default_registry()
    leaked = reg.invoke(
        "get_code_task", ctx, project_id=home, actor="executive_console",
        args={"run_id": run_id},
    )
    assert leaked.get("ok") is False
    assert "run" not in leaked or leaked.get("run") is None

    ok = reg.invoke(
        "get_code_task", ctx, project_id=victim, actor="executive_console",
        args={"run_id": run_id},
    )
    assert ok.get("ok") is True
    assert ok["run"]["id"] == run_id
    assert ok["run"]["project_id"] == victim


def test_intent_router_answer_agent_uses_discuss_registry():
    """Tool-grounded discuss path must use the project-pinned discuss registry."""
    from boss_engine.tools import build_discuss_read_registry

    router = IntentRouter(get_settings())
    agent = router._answer_agent()
    names = set(agent.registry.names())
    assert "query_db" not in names
    assert names == set(build_discuss_read_registry().names())
    assert "create_code_task" not in names


def test_retry_code_task_requires_task_create_actor(client, engine):
    """POST /v1/code-tasks/{run_id}/retry enqueues a new governed attempt under the
    victim run's project. Without an actor gate, knowing a run_id was enough to
    spend another tenant's budget and mutate their lineage.
    """
    project_id = get_settings().default_project_id
    run_id = "crun_retry_auth_victim"
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO code_task_runs (
                    id, project_id, title, status, phase, created_at, updated_at
                )
                VALUES (
                    :id, :project_id, 'Retry auth victim', 'failed',
                    'done', now(), now()
                )
                ON CONFLICT (id) DO UPDATE
                SET project_id = EXCLUDED.project_id,
                    status = 'failed',
                    phase = 'done'
                """
            ),
            {"id": run_id, "project_id": project_id},
        )

    missing_actor = client.post(
        f"/v1/code-tasks/{run_id}/retry",
        json={"fix_note": "try again"},
    )
    assert missing_actor.status_code == 422, missing_actor.text

    stranger = client.post(
        f"/v1/code-tasks/{run_id}/retry",
        json={"fix_note": "try again", "actor": "not_a_member"},
    )
    assert stranger.status_code == 403, stranger.text


def test_steer_code_task_requires_task_create_actor(client, engine):
    """POST /v1/code-tasks/{run_id}/steer injects an operator override into the next stage.

    Without an actor gate, knowing a run_id was enough to redirect any tenant's
    running task (including destructive guidance).
    """
    project_id = get_settings().default_project_id
    run_id = "crun_steer_auth_victim"
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO code_task_runs (
                    id, project_id, title, status, phase, created_at, updated_at
                )
                VALUES (
                    :id, :project_id, 'Steer auth victim', 'running',
                    'implement', now(), now()
                )
                ON CONFLICT (id) DO UPDATE
                SET project_id = EXCLUDED.project_id,
                    status = 'running',
                    phase = 'implement'
                """
            ),
            {"id": run_id, "project_id": project_id},
        )

    missing_actor = client.post(
        f"/v1/code-tasks/{run_id}/steer",
        json={"guidance": "delete all files"},
    )
    assert missing_actor.status_code == 422, missing_actor.text

    stranger = client.post(
        f"/v1/code-tasks/{run_id}/steer",
        json={"guidance": "delete all files", "actor": "not_a_member"},
    )
    assert stranger.status_code == 403, stranger.text


def test_override_code_task_requires_task_create_actor(client, engine):
    """POST /v1/code-tasks/{run_id}/override must not waive gates without project authz."""
    project_id = get_settings().default_project_id
    run_id = "crun_override_auth_victim"
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO code_task_runs (
                    id, project_id, title, status, phase, created_at, updated_at
                )
                VALUES (
                    :id, :project_id, 'Override auth victim', 'blocked',
                    'awaiting_gate_override', now(), now()
                )
                ON CONFLICT (id) DO UPDATE
                SET project_id = EXCLUDED.project_id,
                    status = 'blocked',
                    phase = 'awaiting_gate_override'
                """
            ),
            {"id": run_id, "project_id": project_id},
        )

    missing_actor = client.post(
        f"/v1/code-tasks/{run_id}/override",
        json={"waive_gates": ["all"]},
    )
    assert missing_actor.status_code == 422, missing_actor.text

    stranger = client.post(
        f"/v1/code-tasks/{run_id}/override",
        json={"waive_gates": ["all"], "actor": "not_a_member"},
    )
    assert stranger.status_code == 403, stranger.text


def test_preview_start_and_stop_require_task_create_actor(client, engine):
    """Preview start/stop mutate host/docker resources — require project authorization."""
    project_id = get_settings().default_project_id
    run_id = "crun_preview_auth_victim"
    preview_id = "prev_auth_victim"
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO code_task_runs (
                    id, project_id, title, status, phase, created_at, updated_at
                )
                VALUES (
                    :id, :project_id, 'Preview auth victim', 'completed', 'done',
                    now(), now()
                )
                ON CONFLICT (id) DO UPDATE
                SET project_id = EXCLUDED.project_id, status = 'completed'
                """
            ),
            {"id": run_id, "project_id": project_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO app_previews (
                    id, project_id, run_id, status, expires_at, created_at
                )
                VALUES (
                    :id, :project_id, :run_id, 'running',
                    now() + interval '1 hour', now()
                )
                ON CONFLICT (id) DO UPDATE
                SET project_id = EXCLUDED.project_id, status = 'running'
                """
            ),
            {"id": preview_id, "project_id": project_id, "run_id": run_id},
        )

    missing_actor = client.post(f"/v1/code-tasks/{run_id}/preview", json={})
    assert missing_actor.status_code == 422, missing_actor.text

    stranger = client.post(
        f"/v1/code-tasks/{run_id}/preview",
        json={"actor": "not_a_member"},
    )
    assert stranger.status_code == 403, stranger.text

    stop_missing = client.post(f"/v1/previews/{preview_id}/stop", json={})
    assert stop_missing.status_code == 422, stop_missing.text

    stop_stranger = client.post(
        f"/v1/previews/{preview_id}/stop",
        json={"actor": "not_a_member"},
    )
    assert stop_stranger.status_code == 403, stop_stranger.text
