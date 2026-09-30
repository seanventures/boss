"""Confirmed Community routes record a local receipt and do not call a provider."""

from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.mcp.api_surface import operation_alias


PROJECT = "boss-dogfood"
PLAN = ["restore the previous local record", "do not call a provider"]


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _setup(client, engine, monkeypatch):
    suffix = uuid4().hex[:10]
    mcp_key, human_key = f"mcp-{suffix}", f"human-{suffix}"
    mcp_member = f"mcp_member_{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", f"{mcp_key},{human_key}")
    monkeypatch.setattr(
        api_main.settings, "api_key_principals",
        f"{mcp_key}:{mcp_member},{human_key}:ceo",
    )
    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=mcp_member,
            display_name="Community unhold caller", member_type="agent",
            role_keys=[],
            permissions=[
                "task_create", "providerops_manage", "infra_provision",
                "integration_manage", "enterprise_manage", "role_review",
                "decision_resolve", "workflow_execute",
            ],
            actor="ceo",
        )
        reserved = api_main.budget.reserve(
            connection, PROJECT, "infra", 1_000_000,
            "community unhold route", 120,
        )
    assert reserved["status"] == "reserved", reserved
    return {
        "client": client, "engine": engine, "suffix": suffix,
        "mcp_key": mcp_key, "human_key": human_key, "actor": mcp_member,
        "reservation_id": reserved["reservation_id"],
    }


def _confirm(ctx, method, template, path, *, body=None, query=None):
    client = ctx["client"]
    alias = operation_alias(method, template)
    requested = {
        "project_id": PROJECT, "operation": alias, "method": method,
        "path": path, "query": query or {}, "body": {} if body is None else body,
    }
    kwargs = {"headers": _headers(ctx["mcp_key"])}
    if query:
        kwargs["params"] = query
    kwargs["json"] = requested["body"]
    bare = client.request(method, path, **kwargs)
    assert bare.status_code == 409, bare.text
    assert bare.json()["detail"] == "mcp_confirmation_required"
    proposal = client.post(
        "/v1/mcp-confirmations", headers=_headers(ctx["mcp_key"]), json=requested,
    )
    assert proposal.status_code == 201, proposal.text
    confirmation_id = proposal.json()["id"]
    approved = client.post(
        f"/v1/mcp-confirmations/{confirmation_id}/approve",
        headers=_headers(ctx["human_key"]),
        json={
            "project_id": PROJECT,
            "reviewed_digest": proposal.json()["request_digest"],
            "reviewed_request": requested,
        },
    )
    assert approved.status_code == 200, approved.text
    effected = client.request(method, path, headers={
        **_headers(ctx["mcp_key"]),
        "X-BOSS-MCP-Operation": alias,
        "X-BOSS-MCP-Confirmation": confirmation_id,
        "X-BOSS-MCP-Project": PROJECT,
    }, params=query, json=requested["body"])
    assert effected.status_code < 300, effected.text
    return effected


def _receipt(engine, effect_kind, *, project_id=PROJECT, plan=None, reserved=False, leased=False):
    # Some product paths already record the same effect_kind with a local pool
    # marker. The governed row is the one that closed a lease and names no provider.
    governed = ""
    if leased:
        governed = """
            AND effect_scope->>'job_id' IS NOT NULL
            AND effect_scope->>'provider_called' = 'false'
        """
    with engine.begin() as connection:
        row = connection.execute(text(f"""
            SELECT dedupe_key, status, provider_ref, effect_scope
            FROM receipts
            WHERE project_id = :project_id AND effect_kind = :effect_kind
            {governed}
            ORDER BY created_at DESC LIMIT 1
        """), {"project_id": project_id, "effect_kind": effect_kind}).mappings().first()
    assert row, effect_kind
    assert row["dedupe_key"]
    assert row["status"] == "succeeded"
    assert row["provider_ref"] is None
    scope = row["effect_scope"]
    if isinstance(scope, str):
        scope = json.loads(scope)
    if plan is not None:
        assert scope["rollback_plan"] == plan
    if reserved:
        assert scope["reservation_id"]
    if leased:
        assert scope["job_id"]
        with engine.begin() as connection:
            job = connection.execute(text("""
                SELECT status, lease_owner, lease_token_hash, lease_expires_at, heartbeat_at
                FROM jobs WHERE id = :job_id
            """), {"job_id": scope["job_id"]}).mappings().one()
        assert job["status"] == "succeeded"
        assert job["lease_owner"] and job["lease_token_hash"]
        assert job["lease_expires_at"] and job["heartbeat_at"]


def _host(engine, ctx):
    host_id = f"host_{ctx['suffix']}_{uuid4().hex[:6]}"
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO deployment_hosts (id, project_id, name, address, created_by)
            VALUES (:id, :project_id, :name, '127.0.0.1', :actor)
        """), {"id": host_id, "project_id": PROJECT, "name": host_id, "actor": ctx["actor"]})
    return host_id


def _insert_target(ctx, *, environment, status="created", app_name=None, repo=None):
    name = app_name or f"app-{uuid4().hex[:8]}"
    repository = repo or f"example/{name}"
    with ctx["engine"].begin() as connection:
        target = api_main.resource_plane.create_deployment_target(
            connection,
            project_id=PROJECT,
            app_name=name,
            repo=repository,
            environment=environment,
            provider_account_id=None,
            target_kind="local",
            target_ref=f"ref-{name}-{environment}",
            domains=[],
            required_resources=[],
            secret_refs=[],
            health_checks=[],
            rollback_points=[{"step": "restore-previous-release"}],
            actor=ctx["actor"],
        )
        if status != "created":
            connection.execute(
                text("UPDATE deployment_targets SET status = :status WHERE id = :id"),
                {"status": status, "id": target["id"]},
            )
    return target


def _run(ctx, target, *, environment=None, status="created", commit_ref="abc123"):
    with ctx["engine"].begin() as connection:
        run = api_main.resource_plane.plan_deployment_run(
            connection,
            project_id=PROJECT,
            deployment_target_id=target["id"],
            commit_ref=commit_ref,
            release_packet={"source": "unhold"},
            expected_cost_usd_micros=0,
            reservation_id=None,
            actor=ctx["actor"],
        )
        if environment or status != "created":
            connection.execute(text("""
                UPDATE deployment_runs
                SET environment = COALESCE(:environment, environment),
                    status = :status,
                    commit_ref = :commit_ref
                WHERE id = :id
            """), {
                "environment": environment, "status": status,
                "commit_ref": commit_ref, "id": run["id"],
            })
    return run


def _envelope(ctx, *, spend=False):
    body = {"actor": ctx["actor"], "rollback_plan": PLAN}
    if spend:
        body["reservation_id"] = ctx["reservation_id"]
        body["expected_cost_usd_micros"] = 1
    return body


def test_unheld_community_routes_reject_without_confirmation_and_receipt_the_effect(
    client, engine, monkeypatch,
):
    ctx = _setup(client, engine, monkeypatch)
    actor = ctx["actor"]

    role_key = f"unhold_{ctx['suffix']}"
    _confirm(ctx, "POST", "/v1/roles", "/v1/roles", body={
        "project_id": PROJECT, "role_key": role_key, "display_name": "Unhold role",
    })
    _receipt(engine, "role.register")

    _confirm(ctx, "POST", "/v1/console/v2/autonomy", "/v1/console/v2/autonomy", body={
        "project_id": PROJECT, "actor": actor, "mode": "supervised",
    })
    _receipt(engine, "autonomy.policy_set")
    _confirm(ctx, "POST", "/v1/console/v2/autonomy/apply", "/v1/console/v2/autonomy/apply", body={
        "project_id": PROJECT, "actor": actor,
    })
    _receipt(engine, "autonomy.policy_applied")

    decision_id = f"dec_unhold_{ctx['suffix']}"
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO decisions (
                id, project_id, question, recommendation, options,
                risk_tier, default_action, status
            ) VALUES (
                :id, :project_id, 'Unhold approval',
                '{"action":"manual_test"}'::jsonb,
                '[{"label":"Review","action":"review"}]'::jsonb,
                1, 'hold', 'pending'
            )
        """), {"id": decision_id, "project_id": PROJECT})
    approved = _confirm(
        ctx, "POST", "/v1/decisions/{decision_id}/approve",
        f"/v1/decisions/{decision_id}/approve", body={"actor": actor},
    )
    assert approved.json()["status"] == "approved"
    assert approved.json()["approved_by"] == "ceo"
    _receipt(engine, "decision.approve")

    _confirm(ctx, "POST", "/v1/enterprise/identity-providers",
             "/v1/enterprise/identity-providers", body={
                 "project_id": PROJECT, "provider_key": f"okta-{ctx['suffix']}",
                 "provider_type": "oidc", "display_name": "Unhold IdP",
                 "issuer": "https://example.test/oauth",
                 "jwks_url": "https://example.test/keys",
                 "audience": "boss-api", "status": "planned", "actor": actor,
             })
    _receipt(engine, "enterprise.identity_provider.upsert")

    created_org = _confirm(ctx, "POST", "/v1/orgs", "/v1/orgs", body={
        "name": f"Unhold {ctx['suffix']}",
    })
    _receipt(engine, "org.onboard", project_id=created_org.json()["project_id"])

    host_id = _host(engine, ctx)
    for tail, template, kind, spend in (
        ("key", "/v1/deployment-hosts/{host_id}/key", "deployment_host.key", False),
        ("adopt-key", "/v1/deployment-hosts/{host_id}/adopt-key", "deployment_host.adopt_key", False),
        ("install-key", "/v1/deployment-hosts/{host_id}/install-key", "deployment_host.install_key", False),
        ("provision", "/v1/deployment-hosts/{host_id}/provision", "deployment_host.provision", True),
        ("retire", "/v1/deployment-hosts/{host_id}/retire", "deployment_host.retire", False),
    ):
        _confirm(
            ctx, "POST", template, f"/v1/deployment-hosts/{host_id}/{tail}",
            body=_envelope(ctx, spend=spend),
        )
        _receipt(engine, kind, plan=PLAN, reserved=spend, leased=True)

    retired = _insert_target(ctx, environment="preview", status="retired", app_name=f"purge-{ctx['suffix']}")
    _confirm(
        ctx, "POST", "/v1/deployment-targets/{target_id}/purge",
        f"/v1/deployment-targets/{retired['id']}/purge",
        body={**_envelope(ctx), "confirm": retired["app_name"]},
    )
    _receipt(engine, "deployment_target.purge", plan=PLAN, leased=True)

    torn = _insert_target(ctx, environment="staging", app_name=f"down-{ctx['suffix']}")
    _confirm(
        ctx, "POST", "/v1/deployment-targets/{target_id}/teardown",
        f"/v1/deployment-targets/{torn['id']}/teardown",
        body={**_envelope(ctx), "confirm": torn["app_name"]},
    )
    _receipt(engine, "deployment_target.teardown", plan=PLAN, leased=True)

    boot = _insert_target(ctx, environment="preview", app_name=f"boot-{ctx['suffix']}")
    _confirm(
        ctx, "POST", "/v1/deployment-targets/{target_id}/bootstrap",
        f"/v1/deployment-targets/{boot['id']}/bootstrap",
        body=_envelope(ctx, spend=True),
    )
    _receipt(engine, "deployment_target.bootstrap", plan=PLAN, reserved=True, leased=True)

    flag = _insert_target(ctx, environment="production", app_name=f"flag-{ctx['suffix']}")
    _confirm(
        ctx, "POST", "/v1/deployment-targets/{target_id}/auto-deploy",
        f"/v1/deployment-targets/{flag['id']}/auto-deploy",
        body={"actor": actor, "auto_deploy": False, "paused": True},
    )
    _receipt(engine, "deployment_target.auto_deploy")

    staging = _insert_target(ctx, environment="staging", app_name=f"prom-{ctx['suffix']}")
    _insert_target(
        ctx, environment="production", status="active",
        app_name=staging["app_name"], repo=staging["repo"],
    )
    staged_run = _run(ctx, staging, environment="staging", status="succeeded")
    _confirm(
        ctx, "POST", "/v1/deployments/{deployment_run_id}/promote",
        f"/v1/deployments/{staged_run['id']}/promote",
        body=_envelope(ctx, spend=True),
    )
    _receipt(engine, "deployment.promote", plan=PLAN, reserved=True, leased=True)

    executed = _run(ctx, staging)
    _confirm(
        ctx, "POST", "/v1/delivery/deployments/{deployment_run_id}/execute",
        f"/v1/delivery/deployments/{executed['id']}/execute",
        body=_envelope(ctx, spend=True),
    )
    _receipt(engine, "delivery.execute", plan=PLAN, reserved=True, leased=True)

    rolled = _run(ctx, staging)
    started = client.post(
        f"/v1/deployment-runs/{rolled['id']}/rollout/start",
        headers=_headers(ctx["mcp_key"]),
        json={"actor": actor, "stages": ["canary", "full"]},
    )
    assert started.status_code == 200, started.text
    _confirm(
        ctx, "POST", "/v1/deployment-runs/{deployment_run_id}/rollout/rollback",
        f"/v1/deployment-runs/{rolled['id']}/rollout/rollback",
        body={"actor": actor, "reason": "recorded local rollback"},
    )
    _receipt(engine, "deployment_rollout.rollback", plan=["recorded local rollback"], leased=True)

    run_id = f"crun_{ctx['suffix']}"
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO code_task_runs (id, project_id, title)
            VALUES (:id, :project_id, 'unhold deploy')
        """), {"id": run_id, "project_id": PROJECT})
    _confirm(
        ctx, "POST", "/v1/code-tasks/{run_id}/deploy",
        f"/v1/code-tasks/{run_id}/deploy",
        body={**_envelope(ctx, spend=True), "mode": "ssh"},
    )
    _receipt(engine, "code_task.deploy", plan=PLAN, reserved=True, leased=True)

    _confirm(
        ctx, "POST", "/v1/observability/stack/deploy", "/v1/observability/stack/deploy",
        body={
            **_envelope(ctx, spend=True),
            "project_id": PROJECT, "host_target_id": boot["id"],
        },
    )
    _receipt(engine, "observability.stack.deploy", plan=PLAN, reserved=True, leased=True)

    discovery_id = f"disc_{ctx['suffix']}"
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO discovery_runs (id, project_id, title, status, phase, state)
            VALUES (
                :id, :project_id, 'unhold', 'completed', 'done',
                CAST(:state AS JSONB)
            )
        """), {
            "id": discovery_id, "project_id": PROJECT,
            "state": json.dumps({"candidate_outcomes": [{"outcome": "local note"}]}),
        })
    convened = _confirm(
        ctx, "POST", "/v1/discoveries/{run_id}/promote",
        f"/v1/discoveries/{discovery_id}/promote",
        body={"actor": actor, "outcome_index": 0},
    )
    assert convened.json()["status"] == "convened"
    _receipt(engine, "discovery.promote", reserved=True)

    branch = f"boss/unhold-{ctx['suffix']}"
    preview_id = f"prev_{ctx['suffix']}"
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO app_previews (
                id, project_id, run_id, repo, branch, status, url, mode
            ) VALUES (
                :id, :project_id, :run_id, 'acme/demo-app', :branch, 'running',
                'http://127.0.0.1:9/', 'process'
            )
        """), {"id": preview_id, "project_id": PROJECT, "run_id": run_id, "branch": branch})
        connection.execute(text("""
            INSERT INTO artifacts (
                id, project_id, artifact_type, title, content, content_hash
            ) VALUES (
                :id, :project_id, 'test_report', 'Test report',
                CAST(:content AS JSONB), :content_hash
            )
        """), {
            "id": f"art_{preview_id}", "project_id": PROJECT,
            "content": json.dumps({"status": "passed", "summary": "local", "tests_added_or_run": ["local"]}),
            "content_hash": f"hash-{preview_id}",
        })
        connection.execute(text("""
            INSERT INTO decisions (
                id, project_id, question, recommendation, options, risk_tier,
                default_action, status
            ) VALUES (
                :id, :project_id, 'Review and approve sandbox PR',
                CAST(:recommendation AS JSONB), '[]'::jsonb, 2, 'hold', 'pending'
            )
        """), {
            "id": f"dec_{preview_id}", "project_id": PROJECT,
            "recommendation": json.dumps({
                "action": "review_pull_request",
                "test_report_artifact_id": f"art_{preview_id}",
                "pull_request_url": "https://example.test/acme/demo-app/pull/1",
                "branch_name": branch,
                "title": "Unhold preview",
            }),
        })
    previous_merge = api_main.decisions.settings.enable_sandbox_merge
    api_main.decisions.settings.enable_sandbox_merge = False
    try:
        _confirm(
            ctx, "POST", "/v1/previews/{preview_id}/approve",
            f"/v1/previews/{preview_id}/approve",
            body={"actor": actor, "note": "local approval"},
        )
    finally:
        api_main.decisions.settings.enable_sandbox_merge = previous_merge
    _receipt(engine, "preview.approve")

    sandbox_id = f"sbx_{ctx['suffix']}"
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO code_task_runs (id, project_id, title, finalize_ctx)
            VALUES (:id, :project_id, 'sandbox', CAST(:ctx AS JSONB))
        """), {
            "id": f"crun_sbx_{ctx['suffix']}", "project_id": PROJECT,
            "ctx": json.dumps({"sandbox_task_id": sandbox_id, "rollback_plan": PLAN}),
        })
    _confirm(ctx, "DELETE", "/v1/sandboxes/{sandbox_id}", f"/v1/sandboxes/{sandbox_id}")
    _receipt(engine, "sandbox.delete", plan=PLAN, leased=True)

    preview_sandbox_id = f"psb_{ctx['suffix']}"
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO preview_sandboxes (
                id, project_id, app_id, simulate, status, reserved_by
            ) VALUES (:id, :project_id, :app_id, true, 'running', :actor)
        """), {
            "id": preview_sandbox_id, "project_id": PROJECT,
            "app_id": f"app-{ctx['suffix']}", "actor": actor,
        })
    _confirm(
        ctx, "DELETE", "/v1/preview-sandboxes/{preview_id}",
        f"/v1/preview-sandboxes/{preview_sandbox_id}",
        body={"project_id": PROJECT, "actor": actor},
    )
    _receipt(
        engine, "preview_sandbox.delete",
        plan=[
            "restore the preview sandbox row from this receipt",
            "no host rootfs is deleted by this governed record",
        ],
        leased=True,
    )

    client.put(
        "/v1/channels/youtube/client",
        headers=_headers(ctx["mcp_key"]),
        json={
            "project_id": PROJECT, "actor": actor,
            "client_id": "1234567890-abcdefghijklmnop.apps.googleusercontent.com",
            "client_secret": "local-test-secret",
            "redirect_uri": "https://example.test/v1/channels/youtube/callback",
        },
    )
    connected = client.post(
        "/v1/channels/youtube/connect",
        headers=_headers(ctx["mcp_key"]),
        json={"project_id": PROJECT, "actor": actor, "connection_key": f"yt_{ctx['suffix']}"},
    )
    assert connected.status_code < 300, connected.text
    _confirm(
        ctx, "DELETE", "/v1/channels/{provider}", "/v1/channels/youtube",
        query={
            "project_id": PROJECT,
            "connection_key": f"yt_{ctx['suffix']}",
            "actor": actor,
        },
    )
    _receipt(engine, "channel.disconnect")

    policy = client.put(
        "/v1/worker-autoscaling/policies",
        headers=_headers(ctx["mcp_key"]),
        json={
            "project_id": PROJECT, "actor": actor, "pool_key": f"pool-{ctx['suffix']}",
            "enabled": True, "min_workers": 0, "max_workers": 4,
            "scale_up_threshold": 1, "cooldown_seconds": 0,
            "max_queue_depth_per_worker": 2, "kind": "local",
        },
    )
    assert policy.status_code == 200, policy.text
    for index in range(5):
        created = client.post(
            "/v1/jobs", headers=_headers(ctx["mcp_key"]),
            json={"project_id": PROJECT, "kind": "plan.command",
                  "payload": {"index": index}, "priority": 50},
        )
        assert created.status_code == 201, created.text
    applied = _confirm(
        ctx, "POST", "/v1/worker-autoscaling/apply", "/v1/worker-autoscaling/apply",
        body={
            "project_id": PROJECT, "actor": actor, "pool_key": f"pool-{ctx['suffix']}",
            "auto_reconcile": True, "reason": "local scale",
        },
    )
    assert applied.json()["results"][0]["receipt_id"]
    _receipt(engine, "worker_autoscaling.apply", leased=True)
