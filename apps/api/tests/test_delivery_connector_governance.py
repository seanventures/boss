"""Direct deployment connectors cannot bypass ChangeSet and receipt governance."""

from uuid import uuid4

from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.services import delivery_pipeline, deployment_connectors
from boss_engine.services.budget import BudgetGovernor


PROJECT = "boss-dogfood"


def _target(engine, suffix: str) -> str:
    target_id = f"deptgt_guard_{suffix}"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO deployment_targets "
                "(id, project_id, app_name, repo, environment, target_kind, "
                "target_ref, status, created_by) VALUES "
                "(:id, :project, :app, 'example/app', 'staging', 'ssh', "
                "'root@203.0.113.25', 'active', 'ceo')"
            ),
            {"id": target_id, "project": PROJECT, "app": f"guard-{suffix}"},
        )
    return target_id


def _run(engine, target_id: str, suffix: str, *, changeset_id: str | None = None) -> str:
    run_id = f"deprun_guard_{suffix}"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO deployment_runs "
                "(id, project_id, deployment_target_id, changeset_id, app_name, "
                "environment, repo, target_kind, target_ref, status, created_by) "
                "VALUES (:id, :project, :target, :changeset, 'guard-app', "
                "'staging', 'example/app', 'ssh', 'root@203.0.113.25', "
                "'created', 'ceo')"
            ),
            {"id": run_id, "project": PROJECT, "target": target_id,
             "changeset": changeset_id},
        )
    return run_id


def _auth(monkeypatch, suffix: str) -> dict[str, str]:
    key = f"deploy-guard-key-{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{key}:ceo")
    return {"Authorization": f"Bearer {key}"}


def test_enterprise_manual_host_deploy_records_block_without_worker(
    client, engine, monkeypatch
):
    suffix = uuid4().hex[:10]
    target_id = _target(engine, suffix)
    headers = _auth(monkeypatch, suffix)
    monkeypatch.setattr(api_main.settings, "boss_edition", "enterprise_self_hosted")
    started = []
    monkeypatch.setattr(
        api_main, "_start_deployment_in_background",
        lambda *_args, **_kwargs: started.append("worker"),
    )

    response = client.post(
        "/v1/delivery/deploy", headers=headers,
        json={"target_id": target_id, "actor": "ceo", "ref": "main", "wait": True},
    )
    assert response.status_code == 409, response.text
    assert "Direct host deployment" in response.text
    with engine.begin() as connection:
        run = connection.execute(
            text("SELECT status, decision_id, changeset_id, failure_reason "
                 "FROM deployment_runs WHERE deployment_target_id = :target"),
            {"target": target_id},
        ).mappings().one()
        event = connection.execute(
            text("SELECT id FROM events WHERE event_type = 'deployment_run.blocked' "
                 "AND project_id = :project ORDER BY created_at DESC LIMIT 1"),
            {"project": PROJECT},
        ).first()
    assert run["status"] == "failed"
    assert run["decision_id"] and run["changeset_id"] is None
    assert "ResourceReceipt" in run["failure_reason"]
    assert event and not started


def test_provider_manual_deploy_returns_reviewable_changeset_without_worker(
    client, engine, monkeypatch
):
    suffix = uuid4().hex[:10]
    headers = _auth(monkeypatch, suffix)
    monkeypatch.setattr(api_main.settings, "boss_edition", "enterprise_self_hosted")
    started = []
    monkeypatch.setattr(
        api_main, "_start_deployment_in_background",
        lambda *_args, **_kwargs: started.append("worker"),
    )
    provider_response = client.post(
        "/v1/providers", headers=headers,
        json={"project_id": PROJECT, "provider": "digitalocean",
              "account_label": f"deploy-{suffix}",
              "scopes": ["read", "write", "deploy"],
              "read_only": False, "deploy_capable": True,
              "destructive_actions_disabled": False, "actor": "ceo"},
    )
    assert provider_response.status_code == 201, provider_response.text
    provider_id = provider_response.json()["id"]
    target_response = client.post(
        "/v1/deployments", headers=headers,
        json={"project_id": PROJECT, "app_name": f"provider-{suffix}",
              "repo": "example/app", "environment": "staging",
              "provider_account_id": provider_id,
              "target_kind": "digitalocean_app", "target_ref": "app-123",
              "domains": ["staging.example.test"],
              "health_checks": [{"name": "health", "url": "https://staging.example.test/health",
                                 "expected_status": 200}],
              "rollback_points": [{"ref": "previous-app-spec"}], "actor": "ceo"},
    )
    assert target_response.status_code == 201, target_response.text
    target_id = target_response.json()["id"]
    policy_response = client.post(
        "/v1/deployment-environment-policies", headers=headers,
        json={"project_id": PROJECT, "environment": "staging",
              "runtime_mode": "staging_allowed",
              "allowed_target_kinds": ["digitalocean_app"],
              "requires_change_set": True, "requires_budget_reservation": True,
              "requires_approval": True, "requires_health_check": False,
              "requires_rollback_plan": True, "actor": "ceo"},
    )
    assert policy_response.status_code == 201, policy_response.text
    denied = client.post(
        "/v1/delivery/deploy", headers=headers,
        json={"target_id": target_id, "actor": "ceo", "ref": "main",
              "expected_cost_usd_micros": 500},
    )
    assert denied.status_code == 409, denied.text
    with engine.begin() as connection:
        blocked_run = connection.execute(
            text("SELECT status, decision_id FROM deployment_runs "
                 "WHERE deployment_target_id = :target ORDER BY created_at DESC LIMIT 1"),
            {"target": target_id},
        ).mappings().one()
    assert blocked_run["status"] == "failed" and blocked_run["decision_id"]
    reservation_id = f"bres_guard_{suffix}"
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO budget_reservations "
                 "(id, project_id, category, amount_usd_micros, expires_at) "
                 "VALUES (:id, :project, 'provider', 500, now() + interval '1 hour')"),
            {"id": reservation_id, "project": PROJECT},
        )

    response = client.post(
        "/v1/delivery/deploy", headers=headers,
        json={"target_id": target_id, "actor": "ceo", "ref": "main", "wait": True,
              "expected_cost_usd_micros": 500, "reservation_id": reservation_id},
    )
    assert response.status_code == 200, response.text
    planned = response.json()
    assert planned["status"] == "planned"
    assert planned["changeset_id"] and planned["deployment_run_id"]
    assert planned["next_action"] == "approve_and_apply_changeset"
    with engine.begin() as connection:
        run = connection.execute(
            text("SELECT status, changeset_id, rollback_plan, release_receipt_ids "
                 "FROM deployment_runs WHERE id = :id"),
            {"id": planned["deployment_run_id"]},
        ).mappings().one()
        changeset = connection.execute(
            text("SELECT status, expected_receipts, expected_cost_usd_micros, "
                 "reservation_id FROM changesets WHERE id = :id"),
            {"id": planned["changeset_id"]},
        ).mappings().one()
        receipts = connection.execute(
            text("SELECT COUNT(*) FROM resource_receipts WHERE changeset_id = :id"),
            {"id": planned["changeset_id"]},
        ).scalar_one()
    assert run["status"] == "created"
    assert run["changeset_id"] == planned["changeset_id"]
    assert run["rollback_plan"] and run["release_receipt_ids"]
    assert changeset["status"] == "pending_approval" and changeset["expected_receipts"]
    assert changeset["reservation_id"] == reservation_id
    assert changeset["expected_cost_usd_micros"] == 500
    assert receipts == 0 and started == []


def test_historical_direct_run_blocks_before_connector_preflight(
    client, engine, monkeypatch
):
    suffix = uuid4().hex[:10]
    target_id = _target(engine, suffix)
    run_id = _run(engine, target_id, suffix)
    headers = _auth(monkeypatch, suffix)
    started = []
    monkeypatch.setattr(
        api_main, "_start_deployment_in_background",
        lambda *_args, **_kwargs: started.append("worker"),
    )
    monkeypatch.setattr(
        deployment_connectors, "preflight",
        lambda *_args, **_kwargs: started.append("preflight"),
    )

    response = client.post(
        f"/v1/delivery/deployments/{run_id}/execute",
        headers=headers, json={"actor": "ceo"},
    )
    assert response.status_code == 409, response.text
    assert "no ChangeSet" in response.text
    with engine.begin() as connection:
        run = connection.execute(
            text("SELECT status, decision_id, failure_reason FROM deployment_runs "
                 "WHERE id = :id"), {"id": run_id},
        ).mappings().one()
        events_before_replay = connection.execute(
            text("SELECT COUNT(*) FROM events WHERE event_type = "
                 "'deployment_run.execute_blocked' AND entity_id = :id"),
            {"id": run_id},
        ).scalar_one()
    assert run["status"] == "blocked" and run["decision_id"]
    assert "no ChangeSet" in run["failure_reason"]
    assert events_before_replay == 1

    replay = delivery_pipeline.execute_deployment(
        engine, api_main.settings, run_id, actor="ceo"
    )
    assert replay["status"] == "blocked"
    with engine.begin() as connection:
        events_after_replay = connection.execute(
            text("SELECT COUNT(*) FROM events WHERE event_type = "
                 "'deployment_run.execute_blocked' AND entity_id = :id"),
            {"id": run_id},
        ).scalar_one()
    assert events_after_replay == 1
    assert started == []


def test_redeploy_host_target_records_new_blocked_run_without_worker(
    client, engine, monkeypatch
):
    suffix = uuid4().hex[:10]
    target_id = _target(engine, suffix)
    prior_id = _run(engine, target_id, suffix)
    headers = _auth(monkeypatch, suffix)
    monkeypatch.setattr(api_main.settings, "boss_edition", "enterprise_self_hosted")
    started = []
    monkeypatch.setattr(
        api_main, "_start_deployment_in_background",
        lambda *_args, **_kwargs: started.append("worker"),
    )

    response = client.post(
        f"/v1/delivery/deployments/{prior_id}/redeploy",
        headers=headers, json={"actor": "ceo"},
    )
    assert response.status_code == 409, response.text
    with engine.begin() as connection:
        new_run = connection.execute(
            text("SELECT id, status, decision_id, changeset_id FROM deployment_runs "
                 "WHERE deployment_target_id = :target AND id <> :prior"),
            {"target": target_id, "prior": prior_id},
        ).mappings().one()
        event = connection.execute(
            text("SELECT id FROM events WHERE event_type = 'deployment_run.blocked' "
                 "AND entity_id = :id"),
            {"id": new_run["decision_id"]},
        ).first()
    assert new_run["status"] == "failed"
    assert new_run["decision_id"] and new_run["changeset_id"] is None
    assert event and started == []


def test_linked_changeset_prerequisites_do_not_authorize_unreceipted_connector(
    engine, monkeypatch
):
    suffix = uuid4().hex[:10]
    target_id = _target(engine, suffix)
    changeset_id = f"chg_guard_{suffix}"
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO changesets "
                 "(id, project_id, title, summary, status, created_by) "
                 "VALUES (:id, :project, 'Guard', 'No provider effect', "
                 "'pending_approval', 'ceo')"),
            {"id": changeset_id, "project": PROJECT},
        )
    run_id = _run(engine, target_id, suffix, changeset_id=changeset_id)
    effects = []
    monkeypatch.setattr(
        deployment_connectors, "preflight",
        lambda *_args, **_kwargs: effects.append("preflight"),
    )

    def attempt() -> str:
        result = delivery_pipeline.execute_deployment(
            engine, api_main.settings, run_id, actor="ceo"
        )
        assert result["status"] == "blocked"
        return result["reason_code"]

    assert attempt() == "deployment_changeset_not_approved"
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE changesets SET status = 'approved' WHERE id = :id"),
            {"id": changeset_id},
        )
    assert attempt() == "deployment_missing_rollback_plan"
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE changesets SET rollback_plan = '[\"revert release\"]'::jsonb, "
                 "expected_cost_usd_micros = 500 WHERE id = :id"),
            {"id": changeset_id},
        )
        connection.execute(
            text("UPDATE deployment_runs SET rollback_plan = "
                 "'[\"revert release\"]'::jsonb WHERE id = :id"),
            {"id": run_id},
        )
    assert attempt() == "deployment_missing_budget_reservation"
    reservation_id = f"bres_guard_{suffix}"
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO budget_reservations "
                 "(id, project_id, category, amount_usd_micros, expires_at) "
                 "VALUES (:id, :project, 'provider', 500, now() - interval '1 hour')"),
            {"id": reservation_id, "project": PROJECT},
        )
        connection.execute(
            text("UPDATE changesets SET reservation_id = :reservation "
                 "WHERE id = :id"),
            {"id": changeset_id, "reservation": reservation_id},
        )
    assert attempt() == "deployment_missing_budget_reservation"
    with engine.begin() as connection:
        reservation_status = connection.execute(
            text("SELECT status FROM budget_reservations WHERE id = :id"),
            {"id": reservation_id},
        ).scalar_one()
        connection.execute(
            text("UPDATE budget_reservations SET status = 'active', "
                 "expires_at = now() + interval '1 hour' WHERE id = :id"),
            {"id": reservation_id},
        )
    assert reservation_status == "expired"
    with monkeypatch.context() as patcher:
        def reject_reservation(*_args, **_kwargs):
            raise ValueError("reservation rejected")

        patcher.setattr(BudgetGovernor, "assert_active_reservation", reject_reservation)
        assert attempt() == "deployment_missing_budget_reservation"
    assert attempt() == "deployment_missing_expected_receipts"
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE changesets SET expected_receipts = "
                 "'[\"receipt-key\"]'::jsonb WHERE id = :id"),
            {"id": changeset_id},
        )
        connection.execute(
            text("UPDATE deployment_runs SET release_receipt_ids = "
                 "'[\"receipt-key\"]'::jsonb WHERE id = :id"),
            {"id": run_id},
        )
    assert attempt() == "deployment_connector_receipt_boundary_missing"
    with engine.begin() as connection:
        run_status = connection.execute(
            text("SELECT status FROM deployment_runs WHERE id = :id"),
            {"id": run_id},
        ).scalar_one()
        resource_receipts = connection.execute(
            text("SELECT COUNT(*) FROM resource_receipts WHERE changeset_id = :id"),
            {"id": changeset_id},
        ).scalar_one()
    assert run_status == "created"  # Safe ProviderOps apply may still complete it.
    assert resource_receipts == 0 and effects == []
