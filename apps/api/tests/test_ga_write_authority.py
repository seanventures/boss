"""Community write routes must honor the caller's project grants before effects."""

from uuid import uuid4

import pytest
from boss_engine.services import (
    code_task_service,
    meeting_service,
    plane_sync,
    prd_decomposition,
    secrets_service,
    setup_service,
)
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


def test_factory_and_infra_effect_routes_require_project_grants(
    client, engine, scoped_operator, monkeypatch,
):
    calls = []

    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id="ga_operator",
            display_name="Scoped GA operator", member_type="human", role_keys=[],
            permissions=["github_manage", "infra_provision"], actor="ceo",
        )

    def fake_effect(*args, **kwargs):
        calls.append("effect")
        return {"status": "blocked"}

    def fake_job(_connection, **kwargs):
        calls.append("job")
        return {
            "id": f"job_ga_{len(calls)}", "project_id": kwargs["project_id"],
            "kind": kwargs["kind"], "status": "queued",
            "priority": kwargs["priority"], "max_attempts": kwargs["max_attempts"],
            "attempt": 0, "run_after": "2026-01-01T00:00:00+00:00",
        }

    monkeypatch.setattr(api_main.software_factory, "propose_pull_request", fake_effect)
    monkeypatch.setattr(api_main.software_factory, "merge_pull_request", fake_effect)
    monkeypatch.setattr(api_main.software_factory, "create_deployment", fake_effect)
    monkeypatch.setattr(api_main.infrastructure, "create_droplet", fake_effect)
    monkeypatch.setattr(api_main.jobs, "create_job", fake_job)
    monkeypatch.setattr(api_main.budget, "attach_job", lambda *_args: None)

    proposal = {
        "project_id": PROJECT, "reservation_id": "res_ga", "dedupe_key": "ga_proposal",
        "title": "Guarded PR", "branch_name": "boss/guarded-pr", "summary": "Review",
        "files": [{"path": "README.md", "content": "safe"}],
    }
    merge = {
        "project_id": PROJECT, "decision_id": "dec_ga", "dedupe_key": "ga_merge",
        "pull_request_number": 42,
        "pull_request_url": "https://github.com/example/boss/pull/42", "title": "Guarded merge",
    }
    deploy = {
        "project_id": PROJECT, "decision_id": "dec_ga", "dedupe_key": "ga_deploy",
        "merged_sha": "abc123", "pull_request_url": merge["pull_request_url"],
        "title": "Guarded deploy", "environment": "staging",
    }
    droplet = {
        "project_id": PROJECT, "decision_id": "dec_ga", "reservation_id": "res_ga",
        "dedupe_key": "ga_droplet", "title": "Guarded droplet",
        "region": "nyc1", "size": "s-1vcpu-1gb", "ttl_minutes": 60,
    }
    cases = [
        ("/v1/software-factory/proposals", proposal),
        ("/v1/software-factory/proposals/queue", proposal),
        ("/v1/software-factory/merges", merge),
        ("/v1/software-factory/merges/queue", merge),
        ("/v1/software-factory/deployments", deploy),
        ("/v1/software-factory/deployments/queue", deploy),
        ("/v1/infra/droplets", droplet),
    ]
    for path, body in cases:
        assert client.post(path, headers=_auth("coo-key"), json=body).status_code == 403
        assert client.post(
            path, headers=_auth("operator-key"),
            json={**body, "project_id": scoped_operator},
        ).status_code == 403
    assert calls == []

    for path, body in cases:
        accepted = client.post(path, headers=_auth("operator-key"), json=body)
        assert accepted.status_code in {200, 201}, (path, accepted.text)
    assert calls == ["effect", "job", "effect", "job", "effect", "job", "effect"]


def test_plane_sync_uses_stored_run_project_and_merge_receipt(
    client, engine, scoped_operator, monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        plane_sync, "on_event",
        lambda _engine, _settings, run_id, event: calls.append((run_id, event))
        or {"synced": True},
    )
    home_run = f"crun_plane_ga_{uuid4().hex[:8]}"
    foreign_run = f"crun_plane_other_{uuid4().hex[:8]}"
    pr_url = "https://github.com/example/boss/pull/42"
    with engine.begin() as connection:
        for run_id, project_id in [(home_run, PROJECT), (foreign_run, scoped_operator)]:
            connection.execute(
                text(
                    "INSERT INTO code_task_runs "
                    "(id, project_id, title, status, phase, finalize_ctx, pull_request_url) "
                    "VALUES (:id, :project_id, 'Plane sync authority', 'running', 'coding', "
                    "'{\"source\":\"plane\",\"plane_issue_id\":\"issue-1\","
                    "\"plane_project_id\":\"plane-1\"}'::jsonb, :pr_url)"
                ),
                {"id": run_id, "project_id": project_id, "pr_url": pr_url},
            )
    path = "/v1/integrations/plane/sync"
    assert client.post(
        path, headers=_auth("coo-key"),
        json={"run_id": home_run, "event": "code_task.started"},
    ).status_code == 403
    assert client.post(
        path, headers=_auth("operator-key"),
        json={"run_id": foreign_run, "event": "code_task.started", "actor": "ceo"},
    ).status_code == 403
    assert calls == []

    started = client.post(
        path, headers=_auth("operator-key"),
        json={"run_id": home_run, "event": "code_task.started"},
    )
    assert started.status_code == 200, started.text
    assert calls == [(home_run, "code_task.started")]

    forged_merge = client.post(
        path, headers=_auth("operator-key"),
        json={"run_id": home_run, "event": "software_factory.pull_request_merged"},
    )
    assert forged_merge.status_code == 409, forged_merge.text
    forged_completion = client.post(
        path, headers=_auth("operator-key"),
        json={"run_id": home_run, "event": "code_task.completed_no_pr"},
    )
    assert forged_completion.status_code == 409, forged_completion.text
    assert len(calls) == 1

    with engine.begin() as connection:
        scope = connection.execute(text(
            "SELECT p.workspace_id, w.org_id FROM projects p "
            "JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :project_id"
        ), {"project_id": PROJECT}).mappings().one()
        connection.execute(text(
            "INSERT INTO receipts (id, org_id, workspace_id, project_id, dedupe_key, "
            "effect_kind, payload_hash, status, result) VALUES "
            "(:id, :org_id, :workspace_id, :project_id, :dedupe_key, "
            "'github.pull_request.merge', 'test-hash', 'succeeded', "
            "CAST(:result AS jsonb))"
        ), {
            "id": f"rcpt_plane_ga_{uuid4().hex[:8]}",
            "org_id": scope["org_id"], "workspace_id": scope["workspace_id"],
            "project_id": PROJECT, "dedupe_key": f"plane-ga-{uuid4().hex[:8]}",
            "result": '{"pull_request_url":"https://github.com/example/boss/pull/42"}',
        })
    merged = client.post(
        path, headers=_auth("operator-key"),
        json={"run_id": home_run, "event": "software_factory.pull_request_merged"},
    )
    assert merged.status_code == 200, merged.text
    assert calls[-1] == (home_run, "software_factory.pull_request_merged")


def test_setup_project_reads_and_decision_key_write_require_entitlement_grant(
    client, engine, scoped_operator, monkeypatch,
):
    calls = []
    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id="ga_operator",
            display_name="Scoped GA operator", member_type="human", role_keys=[],
            permissions=["entitlement_manage"], actor="ceo",
        )

    def fake_status(_connection, _settings, project_id):
        calls.append(("status", project_id))
        return {"project_id": project_id, "ready": True}

    def fake_decision_status(_connection, _settings, project_id):
        calls.append(("decision", project_id))
        return {"backend": "claude", "typesafe_key": False}

    def fake_agents(_settings):
        calls.append(("detect", None))
        return [{"name": "codex", "ready": True}]

    def fake_recorded(agents, _connection, project_id):
        calls.append(("recorded", project_id))
        agents[0]["connected"] = True
        return agents

    def fake_store(_connection, _settings, project_id, _key, **kwargs):
        calls.append(("store", project_id, kwargs["actor"]))
        return {"status": "connected"}

    monkeypatch.setattr(setup_service, "setup_status", fake_status)
    monkeypatch.setattr(setup_service, "decision_model_status", fake_decision_status)
    monkeypatch.setattr(setup_service, "list_coding_agents", fake_agents)
    monkeypatch.setattr(setup_service, "with_recorded_connections", fake_recorded)
    monkeypatch.setattr(setup_service, "store_decision_key", fake_store)

    reads = [
        "/v1/setup/status", "/v1/setup/coding-agents", "/v1/setup/decision-model",
    ]
    for path in reads:
        assert client.get(
            path, headers=_auth("coo-key"), params={"project_id": PROJECT},
        ).status_code == 403
        assert client.get(
            path, headers=_auth("operator-key"),
            params={"project_id": scoped_operator},
        ).status_code == 403
    body = {"project_id": PROJECT, "api_key": "test-decision-key"}
    assert client.post(
        "/v1/setup/decision-model/key", headers=_auth("coo-key"), json=body,
    ).status_code == 403
    assert client.post(
        "/v1/setup/decision-model/key", headers=_auth("operator-key"),
        json={**body, "project_id": scoped_operator},
    ).status_code == 403
    assert client.post(
        "/v1/setup/decision-model/key", headers=_auth("operator-key"),
        json={**body, "actor": "ceo"},
    ).status_code == 403
    assert calls == []

    for path in reads:
        accepted = client.get(
            path, headers=_auth("operator-key"), params={"project_id": PROJECT},
        )
        assert accepted.status_code == 200, (path, accepted.text)
    stored = client.post(
        "/v1/setup/decision-model/key", headers=_auth("operator-key"), json=body,
    )
    assert stored.status_code == 201, stored.text
    assert ("store", PROJECT, "ga_operator") in calls


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
    monkeypatch.setattr(api_main.settings, "worker_principal", "ga_runner")
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
        path, headers=_auth("ceo-key"), json={"run_id": local_run},
    ).status_code == 403
    assert client.post(
        path, headers=_auth("runner-key"),
        json={"run_id": foreign_run, "actor": "ga_runner", "project_id": PROJECT},
    ).status_code == 403
    assert calls == []

    allowed = client.post(
        path, headers=_auth("runner-key"),
        json={"run_id": local_run, "fencing_token": "lease-demo"},
    )
    assert allowed.status_code == 200, allowed.text
    assert calls == [(local_run, "lease-demo")]


def test_prd_meeting_and_step_require_stored_project_execution_grant(
    client, engine, scoped_operator, monkeypatch,
):
    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id="ga_operator",
            display_name="GA build operator", member_type="human", role_keys=[],
            permissions=["workflow_execute"], actor="ceo",
        )

    calls = []
    monkeypatch.setattr(
        api_main.intake, "create_task",
        lambda *_args, **_kwargs: calls.append("task") or {"task_id": "task_ga_prd"},
    )
    monkeypatch.setattr(
        api_main.budget, "reserve",
        lambda *_args, **_kwargs: calls.append("budget") or
        {"status": "reserved", "reservation_id": "bres_ga_prd"},
    )
    monkeypatch.setattr(
        meeting_service, "start_meeting_run",
        lambda *_args, **_kwargs: calls.append("meeting") or "mrun_ga_prd",
    )
    monkeypatch.setattr(
        api_main.jobs, "create_job",
        lambda *_args, **_kwargs: calls.append("job") or {"id": "job_ga_prd"},
    )

    path = "/v1/meetings/prd"
    body = {"project_id": PROJECT, "directive": "Draft a GA plan", "actor": "coo"}
    assert client.post(path, headers=_auth("coo-key"), json=body).status_code == 403
    assert client.post(
        path, headers=_auth("coo-key"), json={**body, "actor": "ceo"},
    ).status_code == 403
    assert client.post(
        path, headers=_auth("operator-key"),
        json={**body, "project_id": scoped_operator, "actor": "ga_operator"},
    ).status_code == 403
    assert calls == []

    allowed = client.post(
        path, headers=_auth("operator-key"), json={**body, "actor": "ga_operator"},
    )
    assert allowed.status_code == 201, allowed.text
    assert calls == ["task", "budget", "meeting", "job"]

    local_run = f"mrun_ga_local_{uuid4().hex[:8]}"
    foreign_run = f"mrun_ga_foreign_{uuid4().hex[:8]}"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO meeting_runs (id, project_id, directive) "
                "VALUES (:local, :project, 'Local meeting'), "
                "(:foreign, :other, 'Foreign meeting')"
            ),
            {
                "local": local_run,
                "project": PROJECT,
                "foreign": foreign_run,
                "other": scoped_operator,
            },
        )
    step_calls = []
    monkeypatch.setattr(
        meeting_service, "advance_meeting_run",
        lambda _engine, _settings, _jobs, run_id:
        step_calls.append(run_id) or {"status": "running"},
    )
    monkeypatch.setattr(api_main.settings, "worker_principal", "ga_operator")
    step_path = "/v1/meetings/step"
    assert client.post(
        step_path, headers=_auth("coo-key"),
        json={"run_id": local_run, "actor": "coo"},
    ).status_code == 403
    assert client.post(
        step_path, headers=_auth("ceo-key"), json={"run_id": local_run},
    ).status_code == 403
    assert client.post(
        step_path, headers=_auth("operator-key"),
        json={"run_id": foreign_run, "actor": "ga_operator", "project_id": PROJECT},
    ).status_code == 403
    assert step_calls == []
    assert client.post(
        step_path, headers=_auth("operator-key"),
        json={"run_id": local_run, "actor": "ga_operator"},
    ).status_code == 200
    assert step_calls == [local_run]


def test_docs_update_enqueue_and_execute_require_github_grant(
    client, engine, scoped_operator, monkeypatch,
):
    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id="ga_operator",
            display_name="GA docs operator", member_type="human", role_keys=[],
            permissions=["github_manage"], actor="ceo",
        )

    calls = []

    def fake_enqueue(_connection, **kwargs):
        calls.append(("enqueue", kwargs["project_id"], kwargs["actor"]))
        return {
            "id": "job_ga_docs", "project_id": kwargs["project_id"],
            "kind": "docs.update", "status": "queued", "priority": 10,
            "max_attempts": 3, "attempt": 0, "run_after": "2026-09-28T00:00:00Z",
        }

    def fake_execute(_connection, **kwargs):
        calls.append(("execute", kwargs["project_id"]))
        return {"status": "succeeded"}

    monkeypatch.setattr(api_main.docs_updates, "enqueue", fake_enqueue)
    monkeypatch.setattr(api_main.docs_updates, "run_update", fake_execute)
    monkeypatch.setattr(api_main.settings, "worker_principal", "ga_operator")
    body = {
        "project_id": PROJECT, "event_id": "evt_ga_docs", "event_type": "code_task.pr_opened",
        "entity_type": "code_task_run", "entity_id": "crun_ga_docs", "actor": "coo",
    }
    for path in ("/v1/docs/update", "/v1/docs/update/execute"):
        assert client.post(
            path, headers=_auth("coo-key"), json=body,
        ).status_code == 403
        assert client.post(
            path, headers=_auth("coo-key"), json={**body, "actor": "ceo"},
        ).status_code == 403
        assert client.post(
            path, headers=_auth("operator-key"),
            json={**body, "project_id": scoped_operator, "actor": "ga_operator"},
        ).status_code == 403
    assert calls == []

    assert client.post(
        "/v1/docs/update/execute", headers=_auth("ceo-key"),
        json={**body, "actor": "ceo"},
    ).status_code == 403
    assert calls == []

    authorized_body = {**body, "actor": "ga_operator"}
    queued = client.post(
        "/v1/docs/update", headers=_auth("operator-key"), json=authorized_body,
    )
    assert queued.status_code == 201, queued.text
    executed = client.post(
        "/v1/docs/update/execute", headers=_auth("operator-key"),
        json={**authorized_body, "created_by_job_id": queued.json()["id"]},
    )
    assert executed.status_code == 200, executed.text
    assert calls == [
        ("enqueue", PROJECT, "ga_operator"),
        ("execute", PROJECT),
    ]
