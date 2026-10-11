"""Real authority on caller-owned disposable Postgres; only provider responses are scripted."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import time
from threading import Event
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services.connection_spend import ConnectionSpend, _actual_cost
from boss_engine.services.setup_service import governed_proof

URL = os.environ.get("BOSS_CONNECT_TEST_DATABASE_URL", "")


@pytest.fixture(scope="module")
def database():
    if not URL:
        pytest.skip("requires an owned disposable Postgres")
    engine = create_engine(URL, connect_args={"options": "-c statement_timeout=8000"})
    yield engine
    engine.dispose()


@pytest.fixture()
def scope(database):
    project = "connectqual_" + uuid.uuid4().hex[:12]
    settings = Settings()
    with database.begin() as c:
        c.execute(text("INSERT INTO orgs(id,name) VALUES(:p,:p)"), {"p": project})
        c.execute(text("INSERT INTO workspaces(id,org_id,name) VALUES(:p,:p,:p)"), {"p": project})
        c.execute(
            text("INSERT INTO projects(id,workspace_id,name,slug) VALUES(:p,:p,:p,:p)"),
            {"p": project},
        )
        c.execute(
            text(
                "INSERT INTO budget_policies(id,scope_type,scope_id,daily_cap_usd_micros,"
                "monthly_cap_usd_micros,burst_cap_usd_micros_per_min) "
                "VALUES(:p,'project',:p,1000000,1000000,0)"
            ),
            {"p": project},
        )
    return database, project, settings


def candidate(p, *, included=False):
    return {
        "provider": "openai",
        "source_kind": "local" if included else "api",
        "entitlement_kind": "local_capacity" if included else "api_key",
        "owner_type": "project",
        "owner_id": p,
        "cost_mode": "local_capacity" if included else "usage_based_api",
        "auth_method": "api_key",
        "connection_status": "connected",
        "status": "active",
        "runtime_supported": True,
        "observed_capacity": {"credential_present": included},
        "supported_models": ["gpt-4o-mini"],
        "workload_classes_allowed": ["owner_sandbox"],
        "risk_tier_max": 1,
        "limit_shape": {"max_concurrent": 1},
    }


def proof(
    scope,
    *,
    intent="owned-intent",
    callback=None,
    before=None,
    included=False,
    credential="owned-nonsecret-marker",
    group="owned-connection",
    actor="usr_owned_owner",
):
    e, p, s = scope
    return governed_proof(
        e,
        s,
        p,
        "setup-openai",
        actor,
        intent=intent,
        candidate=candidate(p, included=included),
        credential="" if included else credential,
        backend="database",
        capacity_group=group,
        bound_prove=callback
        or (lambda binding: {"ok": True, "state": "working", "cost_usd_micros": 1000}),
        before_call=before,
    )


def test_committed_authority_canonical_credential_actor_then_replay(scope):
    from boss_engine.services.llm_telemetry import get_llm_context

    e, p, _ = scope
    calls = []
    original_context = get_llm_context()

    def contact(binding):
        assert binding["credential"] == "owned-nonsecret-marker"
        assert get_llm_context()["project_id"] == p
        assert get_llm_context()["stage"] == "connection_proof"
        assert get_llm_context().get("route_id")
        with e.connect() as c:
            row = (
                c.execute(
                    text(
                        "SELECT j.status AS job, j.payload->>'actor' AS actor, l.status AS lease, "
                        "i.status AS invocation, r.status AS receipt, b.status AS budget "
                        "FROM model_gateway_invocations i JOIN jobs j ON j.id=i.job_id "
                        "JOIN model_gateway_leases l ON l.id=i.model_gateway_lease_id "
                        "JOIN receipts r ON r.id=i.receipt_id JOIN budget_reservations b ON b.id=l.budget_reservation_id "
                        "WHERE i.project_id=:p"
                    ),
                    {"p": p},
                )
                .mappings()
                .one()
            )
            assert dict(row) == {
                "job": "running",
                "actor": "usr_owned_owner",
                "lease": "active",
                "invocation": "in_progress",
                "receipt": "in_progress",
                "budget": "active",
            }
            assert (
                c.execute(
                    text(
                        "SELECT count(*) FROM secret_refs WHERE project_id=:p AND status='active'"
                    ),
                    {"p": p},
                ).scalar_one()
                == 1
            )
        calls.append(1)
        return {"ok": True, "state": "working", "cost_usd_micros": 1234}

    first = proof(scope, callback=contact)
    second = proof(scope, callback=lambda _: pytest.fail("durable intent replay called provider"))
    assert first == second and first["ok"] and len(calls) == 1
    assert get_llm_context() == original_context
    with e.connect() as c:
        assert (
            c.execute(
                text("SELECT actual_usd_micros FROM cost_receipts WHERE project_id=:p"), {"p": p}
            ).scalar_one()
            == 1234
        )
        assert (
            c.execute(
                text("SELECT status FROM secret_refs WHERE project_id=:p"), {"p": p}
            ).scalar_one()
            == "revoked"
        )
        assert (
            c.execute(
                text("SELECT status FROM entitlement_sources WHERE project_id=:p"), {"p": p}
            ).scalar_one()
            == "inactive"
        )


def test_same_intent_changed_credential_is_refused(scope):
    assert proof(scope)["ok"]
    changed = proof(
        scope,
        credential="different-owned-marker",
        callback=lambda _: pytest.fail("changed intent called"),
    )
    assert not changed["ok"] and "intent changed" in changed["error"].lower()


def test_changed_endpoint_after_proof_before_promotion_rollback_is_refused(scope):
    from boss_engine.services import setup_actions, setup_checks

    e, p, s = scope
    calls = []

    def contact(_method, url, **_kwargs):
        calls.append(url)
        return setup_checks.HttpAnswer(
            200,
            {},
            {"choices": [{"message": {"content": "OK"}}], "usage": {"cost_usd_micros": 1000}},
        )

    args = dict(
        api_key="owned-endpoint-marker",
        model="owned-model",
        actor="usr_owned_owner",
        http=contact,
        intent="owned-endpoint-intent",
        capacity_group="existing-fixed-capacity",
        request_target="existing-source",
        backend="database",
    )
    # The proof commits authority and its receipt independently. An interrupted
    # enclosing save leaves that receipt durable without a promoted source.
    with e.connect() as c:
        tx = c.begin()
        first = setup_actions._key_proof(
            c, s, p, "custom", base_url="https://first.example.test/v1", **args
        )
        assert first["ok"]
        tx.rollback()
    with e.begin() as c:
        assert (
            c.execute(
                text(
                    "SELECT result->>'connection_saved_source_id' FROM receipts WHERE project_id=:p"
                ),
                {"p": p},
            ).scalar_one()
            is None
        )
        same = setup_actions._key_proof(
            c, s, p, "custom", base_url="https://first.example.test/v1/", **args
        )
        assert same["ok"]
        changed = setup_actions._key_proof(
            c, s, p, "custom", base_url="https://second.example.test/v1", **args
        )
        assert changed["intent_conflict"] and not changed["ok"]
        assert (
            c.execute(
                text(
                    "SELECT count(*) FROM entitlement_sources WHERE project_id=:p "
                    "AND COALESCE(connection_metadata->>'proof_only','false')<>'true'"
                ),
                {"p": p},
            ).scalar_one()
            == 0
        )
    assert calls == ["https://first.example.test/v1/chat/completions"]


@pytest.mark.parametrize("status", [401, 403, 404, 422, 429])
def test_explicit_provider_rejection_settles_no_completion_and_replays(scope, status):
    e, p, _ = scope
    result = proof(
        scope,
        callback=lambda _: {
            "ok": False,
            "state": "key_rejected",
            "status": status,
            "error": "unsafe provider body",
        },
    )
    assert not result["ok"] and not result.get("reconciliation_required")
    assert proof(scope, callback=lambda _: pytest.fail("rejection replay called")) == result
    with e.connect() as c:
        assert (
            c.execute(
                text("SELECT status FROM budget_reservations WHERE project_id=:p"), {"p": p}
            ).scalar_one()
            == "released"
        )
        assert (
            c.execute(
                text("SELECT count(*) FROM cost_receipts WHERE project_id=:p"), {"p": p}
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize("failure", ["timeout", "5xx", "missing-cost", "boolean-cost", "bad-cost"])
def test_unknown_acceptance_keeps_positive_hold_and_does_not_retry(scope, failure):
    e, p, _ = scope

    def contact(_):
        if failure == "timeout":
            raise TimeoutError("echo owned-nonsecret-marker")
        if failure == "5xx":
            return {"ok": False, "status": 503, "state": "unreachable"}
        return {
            "ok": True,
            "state": "working",
            **(
                {"cost_usd_micros": False}
                if failure == "boolean-cost"
                else {"cost_usd_micros": -1}
                if failure == "bad-cost"
                else {}
            ),
        }

    first = proof(scope, callback=contact)
    assert first["reconciliation_required"]
    assert proof(scope, callback=lambda _: pytest.fail("uncertain intent recalled"))[
        "reconciliation_required"
    ]
    with e.connect() as c:
        row = (
            c.execute(
                text(
                    "SELECT status,amount_usd_micros FROM budget_reservations WHERE project_id=:p"
                ),
                {"p": p},
            )
            .mappings()
            .one()
        )
        assert row["status"] == "active" and row["amount_usd_micros"] == 50000
        assert (
            c.execute(
                text("SELECT count(*) FROM cost_receipts WHERE project_id=:p"), {"p": p}
            ).scalar_one()
            == 0
        )
        strings = (
            c.execute(
                text(
                    "SELECT result::text FROM receipts WHERE project_id=:p UNION ALL SELECT recommendation::text FROM decisions WHERE project_id=:p"
                ),
                {"p": p},
            )
            .scalars()
            .all()
        )
        assert "owned-nonsecret-marker" not in "".join(strings)


def test_exhausted_budget_makes_no_contact(scope):
    e, p, _ = scope
    with e.begin() as c:
        c.execute(
            text(
                "UPDATE budget_policies SET daily_cap_usd_micros=1,monthly_cap_usd_micros=1 WHERE scope_id=:p"
            ),
            {"p": p},
        )
    assert not proof(scope, callback=lambda _: pytest.fail("budget denial contacted"))["ok"]
    with e.connect() as c:
        assert (
            c.execute(
                text("SELECT count(*) FROM model_gateway_invocations WHERE project_id=:p"), {"p": p}
            ).scalar_one()
            == 0
        )
        assert (
            c.execute(
                text("SELECT count(*) FROM secret_refs WHERE project_id=:p AND status='active'"),
                {"p": p},
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize("which", ["job", "model"])
def test_expired_precontact_authority_releases_known_unused_hold(scope, which):
    e, p, _ = scope

    def expire():
        table = "jobs" if which == "job" else "model_gateway_leases"
        column = "lease_expires_at" if which == "job" else "expires_at"
        with e.begin() as c:
            c.execute(
                text(f"UPDATE {table} SET {column}=now()-interval '1 second' WHERE project_id=:p"),
                {"p": p},
            )

    result = proof(
        scope, before=expire, callback=lambda _: pytest.fail("expired authority contacted")
    )
    assert not result["ok"] and not result.get("reconciliation_required")
    with e.connect() as c:
        assert (
            c.execute(
                text("SELECT status FROM budget_reservations WHERE project_id=:p"), {"p": p}
            ).scalar_one()
            == "released"
        )


def test_two_intents_share_the_same_connection_capacity(scope):
    entered, release = Event(), Event()

    def blocked(_):
        entered.set()
        assert release.wait(6)
        return {"ok": True, "cost_usd_micros": 1000, "state": "working"}

    with ThreadPoolExecutor(max_workers=2) as pool:
        active = pool.submit(proof, scope, intent="first", callback=blocked)
        assert entered.wait(6)
        second = proof(
            scope,
            intent="second",
            callback=lambda _: pytest.fail("extra candidate capacity contacted"),
        )
        release.set()
        assert active.result()["ok"]
    assert not second["ok"]


def test_proof_candidate_is_never_an_ordinary_route_or_ready_model(scope):
    e, p, s = scope

    def contact(_):
        from boss_engine.services.entitlements import EntitlementBrokerService
        from boss_engine.services.setup_view import _models, now_utc

        with e.begin() as c:
            broker = EntitlementBrokerService(s)
            result = broker.simulate_model_route_decision(
                c,
                p,
                skill_id="architecture.draft",
                task_kind="architecture.draft",
                workload_class="owner_sandbox",
                risk_tier=1,
                preferred_capabilities=["reasoning"],
                data_boundary="internal",
                actor="usr_owned_owner",
            )
            assert result["selected_source_id"] is None
            assert _models(c, s, p, now_utc(), False) == []
        return {"ok": True, "cost_usd_micros": 1000}

    assert proof(scope, callback=contact)["ok"]


def ordinary_source(scope, *, name="ordinary-owned", group="owned-connection", limit=1):
    from boss_engine.services.entitlements import EntitlementBrokerService
    from boss_engine.services.model_gateway import ModelGatewayService

    e, p, s = scope
    row = {
        **candidate(p),
        "project_id": p,
        "source_id": name,
        "limit_shape": {"max_concurrent": limit},
        "connection_metadata": {"capacity_group": group},
    }
    with e.begin() as c:
        EntitlementBrokerService(s).upsert_source(c, row, actor="usr_owned_owner")
        ModelGatewayService(s).upload_credential(
            c, p, name, value="ordinary-owned-marker", backend="database", actor="usr_owned_owner"
        )
    return name


def ordinary_acquire(scope, source):
    from boss_engine.services.jobs import JobLeaseService
    from boss_engine.services.model_gateway import ModelGatewayService

    e, p, s = scope
    jobs = JobLeaseService(s)
    with e.begin() as c:
        job = jobs.create_job(
            c,
            p,
            "architecture.draft",
            payload={"agent_id": "owned-worker"},
            dedupe_key=uuid.uuid4().hex,
            workload_class="owner_sandbox",
        )
        leased = jobs.lease_owned_job(
            c, project_id=p, job_id=job["id"], worker_id="owned-worker", lease_ttl_seconds=60
        )
        acquired = ModelGatewayService(s).acquire(
            c,
            p,
            job_id=job["id"],
            worker_id="owned-worker",
            job_lease_token=leased["lease_token"],
            agent_id="owned-worker",
            skill_id="architecture.draft",
            preferred_capabilities=["reasoning"],
            risk_tier=1,
            consumer_owner_id=p,
            estimated_cost_usd_micros=50000,
            max_usage={},
            dedupe_key=uuid.uuid4().hex,
            ttl_seconds=60,
            source_id=source,
        )
        return {**acquired, "_job_lease_token": leased["lease_token"]}


def test_ordinary_active_work_prevents_extra_connection_proof_capacity(scope):
    from boss_engine.services.model_gateway import ModelGatewayService

    e, p, s = scope
    source = ordinary_source(scope)
    active = ordinary_acquire(scope, source)
    result = proof(scope, callback=lambda _: pytest.fail("proof bypassed ordinary capacity"))
    assert not result["ok"]
    with e.begin() as c:
        assert (
            c.execute(
                text("SELECT status FROM jobs WHERE project_id=:p AND kind='connection.proof'"),
                {"p": p},
            ).scalar_one()
            == "deadlettered"
        )
        assert (
            c.execute(
                text("SELECT count(*) FROM model_gateway_invocations WHERE project_id=:p"), {"p": p}
            ).scalar_one()
            == 0
        )
        ModelGatewayService(s).release(
            c,
            p,
            active["lease"]["id"],
            worker_id="owned-worker",
            lease_token=active["lease_token"],
            job_lease_token=active["_job_lease_token"],
            reason="Owned fixture releases ordinary capacity.",
        )
    assert proof(scope, intent="new-explicit-intent")["ok"]


def test_only_typed_safe_proof_fields_are_retained(scope):
    e, p, _ = scope
    result = proof(
        scope,
        callback=lambda _: {
            "ok": True,
            "cost_usd_micros": 1000,
            "state": "working",
            "response": "owned-nonsecret-marker",
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "provider_body": "owned-nonsecret-marker",
                "model": "owned-nonsecret-marker",
            },
        },
    )
    assert result["usage"]["model"] == "[REDACTED]"
    assert "provider_body" not in result["usage"] and "response" not in result
    with e.connect() as c:
        record = c.execute(
            text("SELECT result::text FROM receipts WHERE project_id=:p"), {"p": p}
        ).scalar_one()
    assert "owned-nonsecret-marker" not in record


def test_existing_authenticated_subscription_intent_replays_its_original_proof(scope, monkeypatch):
    from boss_engine.services import cli_harness, setup_actions

    e, p, s = scope
    monkeypatch.setattr(
        cli_harness,
        "detect",
        lambda *_a, **_k: {
            "installed": True,
            "authenticated": True,
            "ready": True,
            "version": "owned-fixture",
        },
    )
    calls = []

    def contact():
        calls.append(1)
        return {"ok": True, "cost_usd_micros": 0}

    with e.begin() as c:
        first = setup_actions.connect_model(
            c,
            s,
            p,
            provider="codex",
            kind="subscription",
            actor="usr_owned_owner",
            intent="owned-cli-intent",
            prove_with=contact,
        )
    with e.begin() as c:
        original_check = c.execute(
            text(
                "SELECT connection_metadata->'last_check' FROM "
                "entitlement_sources WHERE project_id=:p AND "
                "COALESCE(connection_metadata->>'proof_only','false')<>'true'"
            ),
            {"p": p},
        ).scalar_one()
        again = setup_actions.connect_model(
            c,
            s,
            p,
            provider="codex",
            kind="subscription",
            actor="usr_owned_owner",
            intent="owned-cli-intent",
            prove_with=lambda: pytest.fail("CLI replay contacted"),
        )
        replay_check = c.execute(
            text(
                "SELECT connection_metadata->'last_check' FROM "
                "entitlement_sources WHERE project_id=:p AND "
                "COALESCE(connection_metadata->>'proof_only','false')<>'true'"
            ),
            {"p": p},
        ).scalar_one()
    assert first == again and len(calls) == 1
    assert original_check["at"] == replay_check["at"]
    assert original_check["receipt_id"] == replay_check["receipt_id"]


def test_connection_proof_prevents_extra_ordinary_capacity(scope):
    from boss_engine.services.model_gateway import ModelCapacityDeniedError

    source = ordinary_source(scope)

    def contact(_):
        with pytest.raises(ModelCapacityDeniedError):
            ordinary_acquire(scope, source)
        return {"ok": True, "cost_usd_micros": 1000}

    assert proof(scope, callback=contact)["ok"]


def test_shared_group_never_increases_the_selected_sources_own_limit(scope):
    from boss_engine.services.model_gateway import ModelCapacityDeniedError

    ordinary_source(scope, name="capacity-parent", limit=2)
    narrow = ordinary_source(scope, name="capacity-child", limit=1)
    ordinary_acquire(scope, narrow)
    with pytest.raises(ModelCapacityDeniedError):
        ordinary_acquire(scope, narrow)


def test_same_intent_inflight_duplicate_never_contacts_or_revokes_active_candidate(scope):
    e, p, _ = scope
    entered, release = Event(), Event()

    def contact(_):
        entered.set()
        assert release.wait(6)
        return {"ok": True, "cost_usd_micros": 1000}

    with ThreadPoolExecutor(max_workers=2) as pool:
        active = pool.submit(proof, scope, callback=contact)
        assert entered.wait(6)
        duplicate = proof(scope, callback=lambda _: pytest.fail("duplicate submitted"))
        with e.begin() as c:
            assert (
                c.execute(
                    text(
                        "SELECT count(*) FROM secret_refs WHERE project_id=:p AND status='active'"
                    ),
                    {"p": p},
                ).scalar_one()
                == 1
            )
        release.set()
        assert active.result()["ok"]
    assert duplicate["reconciliation_required"]


def test_long_proof_heartbeats_both_real_leases(scope):
    e, p, s = scope
    spend = ConnectionSpend.for_runner(
        s,
        engine=e,
        project_id=p,
        run_id="owned-setup",
        step_id="owned",
        attempt=1,
        actor="usr_owned_owner",
        worker_id="owned-proof-worker",
    )
    spend.ttl_seconds = 5

    def contact(_):
        time.sleep(5.5)  # Longer than the original lease; actual heartbeat must renew both.
        with e.begin() as c:
            assert c.execute(
                text("SELECT bool_and(lease_expires_at>now()) FROM jobs WHERE project_id=:p"),
                {"p": p},
            ).scalar_one()
            assert c.execute(
                text(
                    "SELECT bool_and(expires_at>now()) FROM model_gateway_leases WHERE project_id=:p"
                ),
                {"p": p},
            ).scalar_one()
        return {"ok": True, "cost_usd_micros": 1000}

    assert spend.prove(
        s,
        provider="openai",
        intent="owned-long",
        candidate=candidate(p),
        credential="owned-marker",
        backend="database",
        call=contact,
    )["ok"]


@pytest.mark.parametrize("which", ["job", "model"])
def test_postcontact_expiry_preserves_unknown_liability(scope, which):
    e, p, _ = scope

    def contact(_):
        table = "jobs" if which == "job" else "model_gateway_leases"
        column = "lease_expires_at" if which == "job" else "expires_at"
        with e.begin() as c:
            c.execute(
                text(f"UPDATE {table} SET {column}=now()-interval '1 second' WHERE project_id=:p"),
                {"p": p},
            )
        return {"ok": True, "cost_usd_micros": 1000}

    result = proof(scope, callback=contact)
    assert result["reconciliation_required"]
    with e.begin() as c:
        assert (
            c.execute(
                text(
                    "SELECT amount_usd_micros FROM budget_reservations WHERE project_id=:p AND status='active'"
                ),
                {"p": p},
            ).scalar_one()
            == 50000
        )


def test_changed_lease_owner_precontact_refuses_cleanup_and_keeps_hold(scope):
    e, p, _ = scope

    def reassign():
        with e.begin() as c:
            c.execute(
                text("UPDATE jobs SET lease_owner='changed-owned-worker' WHERE project_id=:p"),
                {"p": p},
            )

    result = proof(
        scope, before=reassign, callback=lambda _: pytest.fail("changed owner contacted")
    )
    assert result["reconciliation_required"]
    with e.begin() as c:
        assert (
            c.execute(
                text("SELECT status FROM budget_reservations WHERE project_id=:p"), {"p": p}
            ).scalar_one()
            == "active"
        )


def test_environment_proof_uses_only_canonical_env_reference(scope, monkeypatch):
    e, p, s = scope
    name = "BOSS_TEST_CONNECT_OWNED"
    monkeypatch.setenv("BOSS_SECRET_REF_ENV_ALLOW", "BOSS_TEST_*")
    monkeypatch.setenv(name, "owned-environment-marker")
    record = {**candidate(p), "credential_env_name": name}
    spend = ConnectionSpend.for_runner(
        s,
        engine=e,
        project_id=p,
        run_id="owned-env",
        step_id="env",
        attempt=1,
        actor="usr_owned_owner",
        worker_id="owned-env-worker",
    )

    def contact(binding):
        assert binding["credential"] == "owned-environment-marker"
        with e.begin() as c:
            row = (
                c.execute(
                    text(
                        "SELECT ref_kind,ref_key,storage_backend FROM secret_refs WHERE project_id=:p"
                    ),
                    {"p": p},
                )
                .mappings()
                .one()
            )
            assert dict(row) == {
                "ref_kind": "env",
                "ref_key": name,
                "storage_backend": "environment",
            }
            assert (
                c.execute(
                    text(
                        "SELECT count(*) FROM secret_material m JOIN secret_refs r "
                        "ON r.ref_key=m.ref_key WHERE r.project_id=:p"
                    ),
                    {"p": p},
                ).scalar_one()
                == 0
            )
        return {"ok": True, "cost_usd_micros": 1000}

    assert spend.prove(
        s,
        provider="openai",
        intent="env-intent",
        candidate=record,
        credential="owned-environment-marker",
        call=contact,
    )["ok"]


@pytest.mark.parametrize("invalid", [None, False, True, -1, "1000"])
def test_paid_cost_typing_refuses_unpriced_missing_or_invalid_evidence(invalid):
    with pytest.raises(RuntimeError):
        _actual_cost({"cost_usd_micros": invalid}, included=False)


def test_valid_zero_and_priced_tokens_have_distinct_known_evidence():
    assert _actual_cost({"cost_usd_micros": 0}, included=False) == 0
    assert (
        _actual_cost(
            {
                "cost_usd_micros": False,
                "usage": {"model": "gpt-4o-mini", "prompt_tokens": 9, "completion_tokens": 1},
            },
            included=False,
        )
        > 0
    )
    assert _actual_cost({"ok": True}, included=True) == 0
