"""A project member or leased worker must authorize orchestration writes."""

from uuid import uuid4

from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.app.routers import captain
from boss_engine.services import activity_retention


HOME = "boss-dogfood"


def test_captain_writes_use_bound_project_member(client, engine, monkeypatch):
    suffix = uuid4().hex[:8]
    reader = f"captain_chat_only_{suffix}"
    executor = f"captain_executor_{suffix}"
    reader_key = f"captain-reader-key-{suffix}"
    executor_key = f"captain-executor-key-{suffix}"
    ceo_key = f"captain-ceo-key-{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(
        api_main.settings, "api_keys", f"{reader_key},{executor_key},{ceo_key}",
    )
    monkeypatch.setattr(
        api_main.settings, "api_key_principals",
        f"{reader_key}:{reader},{executor_key}:{executor},{ceo_key}:ceo",
    )
    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=HOME, member_id=reader,
            display_name="Captain chat only", member_type="human", role_keys=[],
            permissions=["agent_chat"], actor="ceo",
        )
        api_main.members.upsert_member(
            connection, project_id=HOME, member_id=executor,
            display_name="Captain executor", member_type="human", role_keys=[],
            permissions=["workflow_execute"], actor="ceo",
        )

    effects = []

    class FakeCaptain:
        def coordinate(self, _connection, _project_id, *, actor):
            effects.append(("coordinate", actor))
            return {"actor": actor}

    class FakePlaybooks:
        def save(self, _connection, *, project_id, actor, **_body):
            effects.append(("save", actor))
            return {"project_id": project_id}

        def start(self, _connection, *, project_id, actor, **_body):
            effects.append(("start", actor))
            return {"project_id": project_id}

        def loop_once(self, _connection, *, project_id, actor, **_body):
            effects.append(("loop", actor))
            return {"project_id": project_id}

    monkeypatch.setattr(captain, "_service", FakeCaptain)
    monkeypatch.setattr(captain, "_playbooks", FakePlaybooks)
    reader_headers = {"Authorization": f"Bearer {reader_key}"}
    requests = (
        ("/v1/captain/coordinate", {"project_id": HOME, "actor": "ceo"}),
        ("/v1/captain/playbooks", {
            "project_id": HOME, "actor": "ceo", "playbook_key": "test",
            "name": "Test",
        }),
        ("/v1/captain/playbooks/test/start", {
            "project_id": HOME, "actor": "ceo", "prd": "Build safely",
            "auto_build": True,
        }),
        ("/v1/captain/playbooks/test/loop", {
            "project_id": HOME, "actor": "ceo",
        }),
    )
    for path, body in requests:
        denied = client.post(path, json=body, headers=reader_headers)
        assert denied.status_code == 403, (path, denied.text)
    assert effects == []

    denied_build = client.post(
        "/v1/captain/playbooks/test/start",
        json={"project_id": HOME, "prd": "Build safely", "auto_build": True},
        headers={"Authorization": f"Bearer {executor_key}"},
    )
    assert denied_build.status_code == 403, denied_build.text
    assert effects == []

    allowed = client.post(
        "/v1/captain/playbooks/test/start",
        json={"project_id": HOME, "prd": "Build safely", "auto_build": True},
        headers={"Authorization": f"Bearer {ceo_key}"},
    )
    assert allowed.status_code == 201, allowed.text
    assert effects == [("start", "ceo")]


def test_agent_resume_requires_matching_worker_lease(client, engine, monkeypatch):
    suffix = uuid4().hex[:8]
    run_id = f"arun_{suffix}"
    worker_key = f"resume-worker-key-{suffix}"
    human_key = f"resume-human-key-{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", f"{worker_key},{human_key}")
    monkeypatch.setattr(
        api_main.settings, "api_key_principals",
        f"{worker_key}:ga_worker,{human_key}:ceo",
    )
    monkeypatch.setattr(api_main.settings, "worker_principal", "ga_worker")
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO agent_runs (id, project_id, status) "
                 "VALUES (:id, :project_id, 'awaiting_approval')"),
            {"id": run_id, "project_id": HOME},
        )
    effects = []
    monkeypatch.setattr(
        api_main.software_factory, "resume_plan_run",
        lambda _connection, _run_id: effects.append(_run_id) or {"status": "completed"},
    )
    headers = {"Authorization": f"Bearer {worker_key}"}
    body = {"worker_id": "worker-1", "job_id": "job-1", "lease_token": "lease-1"}
    assert client.post(
        f"/v1/agent-runs/{run_id}/resume", json=body,
        headers={"Authorization": f"Bearer {human_key}"},
    ).status_code == 403
    assert client.post(
        f"/v1/agent-runs/{run_id}/resume", json={}, headers=headers,
    ).status_code == 422
    monkeypatch.setattr(
        api_main.jobs, "require_active_lease",
        lambda *_args: {
            "kind": "agent.resume", "project_id": HOME,
            "payload": {"run_id": "a_different_run"},
        },
    )
    assert client.post(
        f"/v1/agent-runs/{run_id}/resume", json=body, headers=headers,
    ).status_code == 409
    assert effects == []

    monkeypatch.setattr(
        api_main.jobs, "require_active_lease",
        lambda *_args: {
            "kind": "agent.resume", "project_id": HOME,
            "payload": {"run_id": run_id},
        },
    )
    allowed = client.post(
        f"/v1/agent-runs/{run_id}/resume", json=body, headers=headers,
    )
    assert allowed.status_code == 200, allowed.text
    assert effects == [run_id]


def test_global_log_prune_is_worker_only(client, monkeypatch):
    suffix = uuid4().hex[:8]
    worker_key = f"prune-worker-key-{suffix}"
    human_key = f"prune-human-key-{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", f"{worker_key},{human_key}")
    monkeypatch.setattr(
        api_main.settings, "api_key_principals",
        f"{worker_key}:ga_worker,{human_key}:ceo",
    )
    monkeypatch.setattr(api_main.settings, "worker_principal", "ga_worker")
    effects = []
    monkeypatch.setattr(
        activity_retention, "prune",
        lambda *_args, **_kwargs: effects.append("prune") or {"total_removed": 0},
    )
    denied = client.post(
        "/v1/activity/retention/prune", json={"days": 30},
        headers={"Authorization": f"Bearer {human_key}"},
    )
    assert denied.status_code == 403, denied.text
    assert effects == []
    allowed = client.post(
        "/v1/activity/retention/prune", json={"days": 30},
        headers={"Authorization": f"Bearer {worker_key}"},
    )
    assert allowed.status_code == 200, allowed.text
    assert effects == ["prune"]
