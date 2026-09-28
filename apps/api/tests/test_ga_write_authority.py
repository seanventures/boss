"""Community write routes must honor the caller's project grants before effects."""

from uuid import uuid4

import pytest
from boss_engine.services import code_task_service, prd_decomposition, secrets_service
from sqlalchemy import text

from apps.api.app import main as api_main

PROJECT = "boss-dogfood"


@pytest.fixture
def authenticated_keys(monkeypatch):
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(
        api_main.settings, "api_keys", "ceo-key,coo-key,release-key,runner-key,operator-key"
    )
    monkeypatch.setattr(
        api_main.settings,
        "api_key_principals",
        "ceo-key:ceo,coo-key:coo,release-key:release_manager,"
        "runner-key:ga_runner,operator-key:ga_operator",
    )
    monkeypatch.setattr(api_main.settings, "worker_principal", "ceo")


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def scoped_operator(engine, authenticated_keys):
    other_project = f"ga-other-{uuid4().hex[:8]}"
    with engine.begin() as connection:
        workspace_id = connection.execute(text(
            "SELECT workspace_id FROM projects WHERE id = :project"
        ), {"project": PROJECT}).scalar_one()
        connection.execute(text(
            "INSERT INTO projects (id, workspace_id, name, slug) "
            "VALUES (:id, :workspace, 'Other', :id)"
        ), {"id": other_project, "workspace": workspace_id})
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id="ga_operator",
            display_name="Scoped GA operator", member_type="human", role_keys=[],
            permissions=["integration_manage", "providerops_manage", "budget_manage"],
            actor="ceo",
        )
    return other_project


def test_mcp_registry_mutations_require_pack_manage(client, authenticated_keys):
    payload = {
        "project_id": PROJECT,
        "name": f"GA authority {uuid4().hex[:8]}",
        "url": "https://example.test/mcp",
        "actor": "coo",
    }
    denied = client.post("/v1/mcp/servers", headers=_auth("coo-key"), json=payload)
    assert denied.status_code == 403, denied.text

    spoofed = client.post(
        "/v1/mcp/servers", headers=_auth("release-key"),
        json={**payload, "actor": "ceo"},
    )
    assert spoofed.status_code == 403, spoofed.text

    created = client.post(
        "/v1/mcp/servers", headers=_auth("ceo-key"),
        json={**payload, "actor": "ceo"},
    )
    assert created.status_code == 201, created.text
    path = f"/v1/mcp/servers/{created.json()['id']}"
    params = {"project_id": PROJECT, "enabled": False}
    assert client.patch(path, headers=_auth("coo-key"), params=params).status_code == 403
    assert client.delete(
        path, headers=_auth("coo-key"), params={"project_id": PROJECT}
    ).status_code == 403
    assert client.patch(path, headers=_auth("ceo-key"), params=params).status_code == 200
    assert client.delete(
        path, headers=_auth("ceo-key"), params={"project_id": PROJECT}
    ).status_code == 204


def test_secret_writes_require_integration_grant_before_backend_call(
    client, authenticated_keys, monkeypatch,
):
    calls = []

    def fake_put(_connection, _settings, **kwargs):
        calls.append(("put", kwargs))
        return {"secret_ref_id": "sref_demo", "raw_secret_stored": False}

    def fake_keypair(_connection, _settings, **kwargs):
        calls.append(("keypair", kwargs))
        return {"secret_ref_id": "sref_demo", "public_key": "ssh-ed25519 test"}

    monkeypatch.setattr(secrets_service, "put_secret", fake_put)
    monkeypatch.setattr(secrets_service, "generate_keypair", fake_keypair)
    upload = {"project_id": PROJECT, "name": "demo", "value": "test-only", "actor": "coo"}
    keypair = {"project_id": PROJECT, "name": "demo", "actor": "coo"}

    assert client.post("/v1/secrets", headers=_auth("coo-key"), json=upload).status_code == 403
    assert client.post(
        "/v1/secrets/keypair", headers=_auth("coo-key"), json=keypair
    ).status_code == 403
    assert client.post(
        "/v1/secrets", headers=_auth("release-key"),
        json={**upload, "actor": "ceo"},
    ).status_code == 403
    assert calls == []

    stored = client.post(
        "/v1/secrets", headers=_auth("ceo-key"), json={**upload, "actor": "ceo"}
    )
    assert stored.status_code == 201, stored.text
    minted = client.post(
        "/v1/secrets/keypair", headers=_auth("ceo-key"),
        json={**keypair, "actor": "ceo"},
    )
    assert minted.status_code == 201, minted.text
    assert [kind for kind, _ in calls] == ["put", "keypair"]
    assert all(args["scope_type"] == "project" and args["scope_id"] == PROJECT
               for _, args in calls)


def test_secret_scope_cannot_name_another_projects_backend_key(
    client, authenticated_keys, monkeypatch,
):
    monkeypatch.setattr(
        secrets_service, "put_secret",
        lambda *_args, **_kwargs: pytest.fail("backend must not be called"),
    )
    response = client.post(
        "/v1/secrets", headers=_auth("ceo-key"),
        json={"project_id": PROJECT, "name": "deployment", "value": "test-only",
              "scope_id": "other-project", "actor": "ceo"},
    )
    assert response.status_code == 403, response.text


def test_secret_binding_requires_grant_before_service_call(
    client, authenticated_keys, monkeypatch,
):
    monkeypatch.setattr(
        secrets_service, "bind_secret_to_target",
        lambda *_args, **_kwargs: {"target_id": "target_demo", "bindings": []},
    )
    payload = {"project_id": PROJECT, "name": "ssh_key",
               "secret_ref_id": "sref_demo", "actor": "coo"}
    denied = client.post(
        "/v1/deployment-targets/target_demo/secrets",
        headers=_auth("coo-key"), json=payload,
    )
    assert denied.status_code == 403, denied.text
    allowed = client.post(
        "/v1/deployment-targets/target_demo/secrets",
        headers=_auth("ceo-key"), json={**payload, "actor": "ceo"},
    )
    assert allowed.status_code == 200, allowed.text


def test_secret_metadata_and_verify_require_project_grant(
    client, engine, authenticated_keys, monkeypatch,
):
    ref_id = f"sref_ga_auth_{uuid4().hex[:8]}"
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO secret_refs "
            "(id, project_id, scope_type, scope_id, ref_kind, ref_key, created_by) "
            "VALUES (:id, :project, 'project', :project, 'env', :key, 'ceo')"
        ), {"id": ref_id, "project": PROJECT, "key": f"BOSS_{ref_id.upper()}"})
    monkeypatch.setattr(
        secrets_service, "verify_secret",
        lambda *_args, **_kwargs: {"secret_ref_id": ref_id, "resolvable": True},
    )

    path = f"/v1/secrets/{ref_id}"
    assert client.get(
        "/v1/secrets", headers=_auth("coo-key"), params={"project_id": PROJECT}
    ).status_code == 403
    assert client.get(
        path, headers=_auth("coo-key"), params={"project_id": PROJECT}
    ).status_code == 403
    assert client.post(
        f"{path}/verify", headers=_auth("coo-key"), json={"actor": "coo"}
    ).status_code == 403
    assert client.get(
        path, headers=_auth("ceo-key"), params={"project_id": PROJECT}
    ).status_code == 200
    assert client.post(
        f"{path}/verify", headers=_auth("ceo-key"), json={"actor": "ceo"}
    ).status_code == 200


def test_secret_binding_rejects_foreign_project_reference(engine):
    other_project = f"ga-other-{uuid4().hex[:8]}"
    target_id = f"deptgt_ga_{uuid4().hex[:8]}"
    ref_id = f"sref_ga_{uuid4().hex[:8]}"
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            workspace_id = connection.execute(text(
                "SELECT workspace_id FROM projects WHERE id = :project"
            ), {"project": PROJECT}).scalar_one()
            connection.execute(text(
                "INSERT INTO projects (id, workspace_id, name, slug) "
                "VALUES (:id, :workspace, 'Other', :id)"
            ), {"id": other_project, "workspace": workspace_id})
            connection.execute(text(
                "INSERT INTO deployment_targets "
                "(id, project_id, app_name, repo, environment, target_kind, "
                "target_ref, created_by) VALUES "
                "(:id, :project, :app, 'example/repo', 'staging', 'ssh', 'host', 'ceo')"
            ), {"id": target_id, "project": PROJECT, "app": target_id})
            connection.execute(text(
                "INSERT INTO secret_refs "
                "(id, project_id, scope_type, scope_id, ref_kind, ref_key, created_by) "
                "VALUES (:id, :project, 'project', :project, 'env', :key, 'ceo')"
            ), {"id": ref_id, "project": other_project, "key": f"BOSS_{ref_id.upper()}"})

            with pytest.raises(ValueError, match="not in this project"):
                secrets_service.bind_secret_to_target(
                    connection, target_id=target_id, name="ssh_key",
                    secret_ref_id=ref_id, project_id=PROJECT, actor="ceo",
                )
            assert connection.execute(text(
                "SELECT secret_refs FROM deployment_targets WHERE id = :id"
            ), {"id": target_id}).scalar_one() == []
        finally:
            transaction.rollback()


def test_direct_job_enqueue_requires_execution_and_effect_grants(
    client, engine, authenticated_keys,
):
    base = {"project_id": PROJECT, "kind": "plan.generate", "payload": {}}
    assert client.post("/v1/jobs", headers=_auth("coo-key"), json=base).status_code == 403

    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id="ga_runner",
            display_name="GA runner", member_type="human", role_keys=[],
            permissions=["workflow_execute"], actor="ceo",
        )
    destructive = {**base, "kind": "infra.droplet.delete"}
    assert client.post(
        "/v1/jobs", headers=_auth("runner-key"), json=destructive
    ).status_code == 403

    allowed = client.post("/v1/jobs", headers=_auth("ceo-key"), json=base)
    assert allowed.status_code == 201, allowed.text
    assert allowed.json()["project_id"] == PROJECT


def test_worker_autoscaling_requires_project_infra_grant(client, authenticated_keys):
    policy = {
        "project_id": PROJECT,
        "actor": "ceo",
        "pool_key": f"ga-authority-{uuid4().hex[:8]}",
        "kind": "local",
        "max_workers": 1,
    }
    denied = client.put(
        "/v1/worker-autoscaling/policies",
        headers=_auth("coo-key"), json=policy,
    )
    assert denied.status_code == 403, denied.text
    assert client.post(
        "/v1/worker-autoscaling/apply", headers=_auth("coo-key"),
        json={"project_id": PROJECT, "actor": "ceo", "pool_key": policy["pool_key"]},
    ).status_code == 403
    assert client.get(
        "/v1/worker-autoscaling/policies", headers=_auth("coo-key"),
        params={"project_id": PROJECT},
    ).status_code == 403
    assert client.get(
        "/v1/worker-autoscaling/policies", headers=_auth("ceo-key"),
        params={"project_id": "missing-project"},
    ).status_code == 404
    assert client.put(
        "/v1/worker-autoscaling/policies", headers=_auth("ceo-key"), json=policy,
    ).status_code == 200


def test_settings_and_budget_writes_require_project_grants(
    client, scoped_operator, monkeypatch,
):
    other_project = scoped_operator
    setting_path = "/v1/settings/code_agent_max_concurrency"
    setting = {"project_id": PROJECT, "actor": "coo", "value": 2}
    assert client.put(setting_path, headers=_auth("coo-key"), json=setting).status_code == 403
    assert client.put(
        setting_path, headers=_auth("operator-key"),
        json={**setting, "project_id": other_project, "actor": "ga_operator"},
    ).status_code == 403
    assert client.put(
        setting_path, headers=_auth("ceo-key"), json={**setting, "actor": "ceo"}
    ).status_code == 200

    intensity = {"scope_id": PROJECT, "intensity": 90, "actor": "coo"}
    assert client.post(
        "/v1/budget/set_intensity", headers=_auth("coo-key"), json=intensity
    ).status_code == 403
    assert client.post(
        "/v1/budget/set_intensity", headers=_auth("operator-key"),
        json={**intensity, "scope_id": other_project, "actor": "ga_operator"},
    ).status_code == 403
    assert client.post(
        "/v1/budget/set_intensity", headers=_auth("ceo-key"),
        json={**intensity, "actor": "ceo"},
    ).status_code == 200

    calls = []

    def reserve(_connection, *_args):
        calls.append("reserve")
        return {"status": "reserved", "reservation_id": "bres_demo",
                "budget_state": "green", "message": "reserved"}

    def release(_connection, *_args):
        calls.append("release")
        return {"status": "released", "reservation_id": "bres_demo",
                "budget_state": "green", "message": "released"}

    monkeypatch.setattr(api_main.budget, "reserve", reserve)
    monkeypatch.setattr(api_main.budget, "release", release)
    request = {"project_id": PROJECT, "category": "model", "amount_usd_micros": 1,
               "reason": "GA authority"}
    release_request = {"project_id": PROJECT, "reservation_id": "bres_demo",
                       "reason": "GA authority"}
    assert client.post(
        "/v1/budget/reserve", headers=_auth("coo-key"), json=request
    ).status_code == 403
    assert client.post(
        "/v1/budget/reserve", headers=_auth("operator-key"),
        json={**request, "project_id": other_project},
    ).status_code == 403
    assert client.post(
        "/v1/budget/release", headers=_auth("coo-key"), json=release_request
    ).status_code == 403
    assert calls == []
    assert client.post(
        "/v1/budget/reserve", headers=_auth("ceo-key"), json=request
    ).status_code == 201
    assert client.post(
        "/v1/budget/release", headers=_auth("ceo-key"), json=release_request
    ).status_code == 200
    assert calls == ["reserve", "release"]


def test_event_subscription_writes_require_integration_grant(
    client, scoped_operator,
):
    payload = {"project_id": PROJECT, "event_type": "ga.authority.*",
               "sink_kind": "job", "sink_ref": "docs.update", "actor": "coo"}
    assert client.post(
        "/v1/event-subscriptions", headers=_auth("coo-key"), json=payload
    ).status_code == 403
    assert client.post(
        "/v1/event-subscriptions", headers=_auth("operator-key"),
        json={**payload, "project_id": scoped_operator, "actor": "ga_operator"},
    ).status_code == 403

    created = client.post(
        "/v1/event-subscriptions", headers=_auth("ceo-key"),
        json={**payload, "actor": "ceo"},
    )
    assert created.status_code == 201, created.text
    path = f"/v1/event-subscriptions/{created.json()['id']}"
    assert client.patch(
        path, headers=_auth("coo-key"),
        json={"project_id": PROJECT, "enabled": False, "actor": "coo"},
    ).status_code == 403
    assert client.delete(
        path, headers=_auth("coo-key"), params={"project_id": PROJECT}
    ).status_code == 403
    assert client.patch(
        path, headers=_auth("ceo-key"),
        json={"project_id": PROJECT, "enabled": False},
    ).status_code == 200
    assert client.delete(
        path, headers=_auth("ceo-key"), params={"project_id": PROJECT}
    ).status_code == 204


def test_code_task_enqueue_requires_execution_grant_in_target_project(
    client, engine, scoped_operator, monkeypatch,
):
    calls = []

    def fake_start(_connection, **kwargs):
        calls.append(kwargs["project_id"])
        return "crun_ga_authority"

    monkeypatch.setattr(code_task_service, "start_code_task", fake_start)
    payload = {"project_id": PROJECT, "title": "GA authority", "actor": "coo"}
    denied = client.post("/v1/code-tasks", headers=_auth("coo-key"), json=payload)
    assert denied.status_code == 403, denied.text
    assert calls == []

    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id="ga_runner",
            display_name="GA runner", member_type="human", role_keys=[],
            permissions=["workflow_execute"], actor="ceo",
        )
    cross_project = client.post(
        "/v1/code-tasks", headers=_auth("runner-key"),
        json={**payload, "project_id": scoped_operator, "actor": "ga_runner"},
    )
    assert cross_project.status_code == 403, cross_project.text
    assert calls == []

    allowed = client.post(
        "/v1/code-tasks", headers=_auth("ceo-key"),
        json={**payload, "actor": "ceo"},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["status"] == "queued"
    assert calls == [PROJECT]


def test_authenticated_api_key_needs_principal_even_without_a_mapping(
    client, monkeypatch,
):
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", "unbound-key")
    monkeypatch.setattr(api_main.settings, "api_key_principals", "")
    response = client.get("/v1/projects", headers=_auth("unbound-key"))
    assert response.status_code == 401
    assert "not bound to a project member principal" in response.json()["detail"]
    assert client.post(
        "/v1/worker/poll", headers=_auth("unbound-key"),
        json={"worker_id": "fake", "capabilities": ["code.task"]},
    ).status_code == 401


def test_only_configured_worker_principal_can_claim_or_settle_jobs(
    client, authenticated_keys, monkeypatch,
):
    poll = {"worker_id": "fake", "capabilities": ["code.task"]}
    job = {"worker_id": "fake", "job_id": "job_fake", "lease_token": "lease_fake"}
    requests = [
        ("/v1/worker/poll", poll),
        ("/v1/worker/heartbeat", {**job, "lease_ttl_seconds": 60}),
        ("/v1/worker/complete", {**job, "result": {}}),
        ("/v1/worker/fail", {**job, "error": {"message": "fake"}}),
    ]
    for path, payload in requests:
        denied = client.post(path, headers=_auth("coo-key"), json=payload)
        assert denied.status_code == 403, (path, denied.text)

    monkeypatch.setattr(api_main.jobs, "claim_job", lambda *_args: None)
    accepted = client.post("/v1/worker/poll", headers=_auth("ceo-key"), json=poll)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "idle"

    monkeypatch.setattr(api_main.settings, "worker_principal", "ga_runner")
    assert client.post(
        "/v1/worker/poll", headers=_auth("ceo-key"), json=poll
    ).status_code == 403
    assert client.post(
        "/v1/worker/poll", headers=_auth("runner-key"), json=poll
    ).status_code == 200


def test_prd_and_web_app_builds_require_target_project_execution_grant(
    client, engine, scoped_operator, monkeypatch,
):
    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id="ga_runner",
            display_name="GA runner", member_type="human", role_keys=[],
            permissions=["workflow_execute"], actor="ceo",
        )

    calls = []

    def fake_plan(_settings, _prd, *, max_tasks):
        calls.append(("plan", max_tasks))
        return [{"title": "Reviewed task"}]

    def fake_decompose(_engine, _settings, _jobs, **kwargs):
        calls.append(("decompose", kwargs["project_id"]))
        return {"started": True, "first_run_id": "crun_reviewed"}

    def fake_web_app(_engine, _settings, _jobs, **kwargs):
        calls.append(("web_app", kwargs["project_id"]))
        return {"started": True, "run_id": "crun_web_reviewed"}

    monkeypatch.setattr(prd_decomposition, "decompose_prd_into_tasks", fake_plan)
    monkeypatch.setattr(prd_decomposition, "start_decomposed_build", fake_decompose)
    monkeypatch.setattr(prd_decomposition, "start_web_app_build", fake_web_app)

    cases = [
        ("/v1/prd/decompose", {"prd": "Build a small app", "plan_only": True}),
        ("/v1/prd/decompose", {"prd": "Build a small app"}),
        ("/v1/web-app", {"directive": "Build a small app"}),
    ]
    for path, body in cases:
        denied = client.post(
            path, headers=_auth("coo-key"),
            json={"project_id": PROJECT, "actor": "coo", **body},
        )
        assert denied.status_code == 403, denied.text
        spoofed = client.post(
            path, headers=_auth("coo-key"),
            json={"project_id": PROJECT, "actor": "ceo", **body},
        )
        assert spoofed.status_code == 403, spoofed.text
        cross_project = client.post(
            path, headers=_auth("runner-key"),
            json={"project_id": scoped_operator, "actor": "ga_runner", **body},
        )
        assert cross_project.status_code == 403, cross_project.text
    assert calls == []

    for path, body in cases:
        allowed = client.post(
            path, headers=_auth("ceo-key"),
            json={"project_id": PROJECT, "actor": "ceo", **body},
        )
        assert allowed.status_code == 201, allowed.text
    assert calls == [("plan", 6), ("decompose", PROJECT), ("web_app", PROJECT)]


def test_code_task_step_uses_stored_project_for_execution_authority(
    client, engine, scoped_operator, monkeypatch,
):
    local_run = f"crun_ga_local_{uuid4().hex[:8]}"
    foreign_run = f"crun_ga_foreign_{uuid4().hex[:8]}"
    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id="ga_runner",
            display_name="GA runner", member_type="human", role_keys=[],
            permissions=["workflow_execute"], actor="ceo",
        )
        connection.execute(
            text(
                "INSERT INTO code_task_runs (id, project_id, title) "
                "VALUES (:local, :project, 'Local run'), "
                "(:foreign, :other, 'Foreign run')"
            ),
            {
                "local": local_run,
                "project": PROJECT,
                "foreign": foreign_run,
                "other": scoped_operator,
            },
        )

    calls = []

    def fake_run(_engine, _settings, _jobs, run_id, *, fencing_token=None):
        calls.append((run_id, fencing_token))
        return {"status": "running", "run_id": run_id}

    monkeypatch.setattr(code_task_service, "run_code_task", fake_run)
    path = "/v1/code-tasks/step"
    assert client.post(
        path, headers=_auth("coo-key"),
        json={"run_id": local_run, "actor": "coo"},
    ).status_code == 403
    assert client.post(
        path, headers=_auth("coo-key"),
        json={"run_id": local_run, "actor": "ceo"},
    ).status_code == 403
    assert client.post(
        path, headers=_auth("runner-key"),
        json={"run_id": foreign_run, "actor": "ga_runner", "project_id": PROJECT},
    ).status_code == 403
    assert calls == []

    allowed = client.post(
        path, headers=_auth("ceo-key"),
        json={"run_id": local_run, "fencing_token": "lease-demo"},
    )
    assert allowed.status_code == 200, allowed.text
    assert calls == [(local_run, "lease-demo")]
