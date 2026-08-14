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


def test_project_member_upsert_does_not_escalate_to_org_scope(client, engine):
    """Project upsert must not mint org-wide privileges usable on sibling projects.

    f51f19a mirrored every project member (with ON CONFLICT DO UPDATE) into org_members.
    require_member falls back to org scope, so member_manage on Project A could grant
    decision_resolve/ceo on Project B without an org-members API call.
    """
    project_a = get_settings().default_project_id
    project_b = "boss-member-escalation-victim"
    attacker = "project_scoped_attacker"
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES (:id, 'ws_boss', 'Member escalation victim', :id)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"id": project_b},
        )
        connection.execute(
            text("DELETE FROM org_members WHERE member_id = :m"),
            {"m": attacker},
        )
        connection.execute(
            text("DELETE FROM project_members WHERE member_id = :m"),
            {"m": attacker},
        )

    created = client.post(
        "/v1/members",
        json={
            "project_id": project_a,
            "member_id": attacker,
            "display_name": "Project Scoped Attacker",
            "member_type": "human",
            "role_keys": ["ceo"],
            "permissions": [
                "decision_resolve",
                "member_manage",
                "task_create",
                "command_submit",
            ],
            "actor": "ceo",
        },
    )
    assert created.status_code == 201, created.text

    with engine.begin() as connection:
        org_row = connection.execute(
            text(
                "SELECT permissions, role_keys FROM org_members "
                "WHERE member_id = :m AND status = 'active'"
            ),
            {"m": attacker},
        ).mappings().first()
    assert org_row is None, (
        "project collaborator must not appear in org_members; "
        f"got role_keys={org_row and org_row['role_keys']}"
    )

    # Sibling project must not authorize the attacker via org fallback.
    denied = client.post(
        "/v1/commands/propose",
        json={
            "project_id": project_b,
            "command": "Should be denied — no org-wide grant from project upsert.",
            "actor": attacker,
        },
    )
    assert denied.status_code in (401, 403), denied.text


def test_deprovisioned_scim_user_cannot_act_via_org_member_fallback(client, engine):
    """BOSS-320: deprovision must close require_member's org_members fallback.

    Older BOSS mirrored project members into org_members. MemberDirectory
    falls back to an *active* org row when the project row is inactive, so
    disabling only project_members left a deprovisioned SCIM user able to
    mutate. This test plants that leftover mirror, then deactivates via SCIM
    and asserts the actor is unauthorized.
    """
    project_id = get_settings().default_project_id
    user_name = "c2.scim.deprov@example.com"
    member_id = "scim_boss-dogfood_c2_scim_deprov_example_com"

    created = client.post(
        "/v1/enterprise/scim/users",
        json={
            "project_id": project_id,
            "external_id": "usr-c2-scim-deprov",
            "user_name": user_name,
            "display_name": "C2 SCIM Deprov",
            "permissions": ["command_submit"],
            "actor": "ceo",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["member_id"] == member_id
    assert created.json()["active"] is True

    with engine.begin() as connection:
        org_id = connection.execute(
            text(
                """
                SELECT o.id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                JOIN orgs o ON o.id = w.org_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO org_members (
                    id, org_id, member_id, display_name, member_type,
                    role_keys, permissions, status, created_by
                )
                VALUES (
                    :id, :org_id, :member_id, 'C2 SCIM Deprov leftover',
                    'human', CAST(:role_keys AS JSONB),
                    CAST(:permissions AS JSONB), 'active', 'legacy_mirror'
                )
                ON CONFLICT (org_id, member_id) DO UPDATE
                SET status = 'active',
                    permissions = EXCLUDED.permissions,
                    updated_at = now()
                """
            ),
            {
                "id": f"om_c2_deprov_{member_id}",
                "org_id": org_id,
                "member_id": member_id,
                "role_keys": json.dumps([]),
                "permissions": json.dumps(["command_submit"]),
            },
        )

    allowed = client.post(
        "/v1/commands/propose",
        json={
            "project_id": project_id,
            "command": "Active SCIM user may submit a command.",
            "actor": member_id,
        },
    )
    assert allowed.status_code == 201, allowed.text

    deactivated = client.post(
        "/v1/enterprise/scim/users",
        json={
            "project_id": project_id,
            "user_name": user_name,
            "display_name": "C2 SCIM Deprov",
            "active": False,
            "actor": "ceo",
        },
    )
    assert deactivated.status_code == 201, deactivated.text
    assert deactivated.json()["active"] is False

    with engine.begin() as connection:
        project_status = connection.execute(
            text(
                """
                SELECT status FROM project_members
                WHERE project_id = :project_id AND member_id = :member_id
                """
            ),
            {"project_id": project_id, "member_id": member_id},
        ).scalar_one()
        org_status = connection.execute(
            text(
                """
                SELECT status FROM org_members
                WHERE member_id = :member_id
                """
            ),
            {"member_id": member_id},
        ).scalar_one()
    assert project_status == "inactive"
    assert org_status == "inactive", (
        "deprovisioned SCIM user must not remain an active org member "
        f"(status={org_status})"
    )

    denied = client.post(
        "/v1/commands/propose",
        json={
            "project_id": project_id,
            "command": "Deprovisioned SCIM user must not submit via org fallback.",
            "actor": member_id,
        },
    )
    assert denied.status_code == 403, denied.text

    members_after = client.get("/v1/members", params={"project_id": project_id})
    assert members_after.status_code == 200
    assert member_id not in {
        item["member_id"] for item in members_after.json()["items"]
    }
