"""BOSS-483: real authority/lineage on a caller-supplied disposable Postgres.

Set BOSS_ARCHITECTURE_TEST_DATABASE_URL to an owned database with db/schema.sql.
Only the provider response is scripted. No provider/network effects are made.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from threading import Event
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import architecture_docs as arch
from boss_engine.services.architecture_spend import ArchitectureSpend
from boss_engine.services.model_routing import ModelChoice
from boss_engine.services.typed_decision_spend import (
    TypedDecisionReconciliationRequired, TypedDecisionSpendError,
)

URL = os.environ.get("BOSS_ARCHITECTURE_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="requires an owned disposable Postgres")
PRD = "# PRD\n\nBuild a tenant-isolated helpdesk."


@pytest.fixture(scope="module")
def database():
    engine = create_engine(URL, connect_args={"options": "-c statement_timeout=8000"})
    yield engine
    engine.dispose()


@pytest.fixture()
def scope(database):
    p = "archqual_" + uuid.uuid4().hex[:12]
    settings = Settings().model_copy(update={"local_model_enabled": True, "local_model_id": "owned-fixture"})
    # No real secret: the included local source is identified, and only our
    # Scripted adapter is called. Registry/runtime/capacity policy remain real.
    with database.begin() as c:
        c.execute(text("INSERT INTO orgs(id,name) VALUES(:p,:p)"), {"p": p})
        c.execute(text("INSERT INTO workspaces(id,org_id,name) VALUES(:p,:p,:p)"), {"p": p})
        c.execute(text("INSERT INTO projects(id,workspace_id,name,slug) VALUES(:p,:p,:p,:p)"), {"p": p})
        c.execute(text("INSERT INTO budget_policies(id,scope_type,scope_id,daily_cap_usd_micros,"
                       "monthly_cap_usd_micros,burst_cap_usd_micros_per_min) "
                       "VALUES(:p,'project',:p,100000,100000,0)"), {"p": p})
        from boss_engine.services.entitlements import EntitlementBrokerService
        EntitlementBrokerService(settings).seed_default_sources(c, p)
    return database, p, settings


class Scripted:
    _boss_choice = ModelChoice("ollama", "owned fixture", "approved_local_model_host")

    def __init__(self, call=None, usage=None):
        self.calls = 0
        self.call = call
        self.usage = usage if usage is not None else {"model": "owned-scripted", "cost_usd_micros": 3000}

    def create_text_response(self, prompt, system_prompt=None):
        self.calls += 1
        if self.call:
            self.call()
        return "owned-ref", "## Context\nActual scripted response", self.usage


def spender(scope, *, step="arch", run="run-owned"):
    engine, p, settings = scope
    return ArchitectureSpend.for_runner(
        settings, engine=engine, project_id=p, run_id=run, step_id=step, attempt=1,
        actor="usr_owned_owner", worker_id="owned-worker",
    )


def generate(spend, settings, model, *, version=1, check=lambda: None):
    return spend.generate(settings, model, prompt="Canonical approved PRD prompt",
                          system_prompt="Architect", version=version, check_work=check)


def test_authority_commits_before_provider_and_replays_after_caller_rollback(scope):
    engine, p, settings = scope

    def proof():
        with engine.connect() as observer:
            row = observer.execute(text("SELECT j.status AS job_status, j.lease_owner, "
                "l.status AS lease_status, i.status AS invocation_status, r.status AS receipt_status, "
                "b.status AS reservation_status, r.result->>'workflow_actor' AS actor "
                "FROM model_gateway_invocations i JOIN model_gateway_leases l ON l.id=i.model_gateway_lease_id "
                "JOIN jobs j ON j.id=i.job_id JOIN receipts r ON r.id=i.receipt_id "
                "JOIN budget_reservations b ON b.id=l.budget_reservation_id WHERE i.project_id=:p"), {"p": p}).mappings().one()
            assert row["job_status"] == "running" and row["lease_owner"] == "owned-worker"
            assert row["lease_status"] == "active" and row["invocation_status"] == "in_progress"
            assert row["receipt_status"] == "in_progress" and row["reservation_status"] == "active"
            assert row["actor"] == "usr_owned_owner"

    model = Scripted(proof)
    spend = spender(scope)
    # The caller loses its independent transaction. The model receipt persists.
    with engine.connect() as outer:
        transaction = outer.begin()
        first = generate(spend, settings, model)
        transaction.rollback()
    assert generate(spender(scope), settings, model) == first
    assert model.calls == 1
    with engine.connect() as c:
        result = c.execute(text("SELECT result FROM receipts WHERE project_id=:p "
                               "AND effect_kind='model.invoke'"), {"p": p}).scalar_one()
        assert result["workflow_actor"] == "usr_owned_owner"
        assert result["architecture_version"] == 1
        assert c.execute(text("SELECT count(*) FROM cost_receipts WHERE project_id=:p"), {"p": p}).scalar_one() == 1


def test_each_revision_has_its_own_receipt_and_same_revision_replays(scope):
    engine, p, settings = scope
    model = Scripted()
    for version in (1, 2, 2):
        generate(spender(scope), settings, model, version=version)
    assert model.calls == 2
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM model_gateway_invocations WHERE project_id=:p"), {"p": p}).scalar_one() == 2


def test_spent_cap_refuses_included_source_without_provider_call(scope):
    engine, p, settings = scope
    with engine.begin() as c:
        c.execute(text("INSERT INTO cost_receipts(id,project_id,category,skill_or_model_id,"
                       "estimated_usd_micros,actual_usd_micros,status) "
                       "VALUES(:p,:p,'fixture','fixture',100000,100000,'actual')"), {"p": p})
    model = Scripted()
    with pytest.raises(TypedDecisionSpendError):
        generate(spender(scope), settings, model)
    assert model.calls == 0


def test_unknown_provider_outcome_keeps_hold_and_never_retries(scope):
    engine, p, settings = scope
    # Treat this included source as metered for this owned fixture, so a positive
    # hold proves uncertain liability handling rather than merely a zero gate.
    with engine.begin() as c:
        c.execute(text("UPDATE entitlement_sources SET entitlement_kind='usage_based_api', "
                       "cost_mode='usage_based_api' WHERE project_id=:p AND source_id='approved_local_model_host'"), {"p": p})
    def timeout():
        raise TimeoutError("owned unknown acceptance")
    model = Scripted(timeout)
    for _ in range(2):
        with pytest.raises(TypedDecisionReconciliationRequired):
            generate(spender(scope), settings, model)
    assert model.calls == 1
    with engine.connect() as c:
        assert c.execute(text("SELECT status FROM budget_reservations WHERE project_id=:p"), {"p": p}).scalar_one() == "active"
        assert c.execute(text("SELECT amount_usd_micros FROM budget_reservations WHERE project_id=:p"), {"p": p}).scalar_one() > 0
        assert c.execute(text("SELECT status FROM receipts WHERE project_id=:p AND effect_kind='model.invoke'"), {"p": p}).scalar_one() == "in_progress"
        assert c.execute(text("SELECT count(*) FROM decisions WHERE project_id=:p"), {"p": p}).scalar_one() >= 1


def test_stop_between_authority_and_submission_releases_unused_hold(scope):
    engine, p, settings = scope
    model = Scripted()
    checks = []
    def stop():
        checks.append(1)
        if len(checks) == 2:
            raise arch.ArchitectureRefused("owned cancelled before submission")
    with pytest.raises(TypedDecisionSpendError):
        generate(spender(scope), settings, model, check=stop)
    assert model.calls == 0
    with engine.connect() as c:
        assert c.execute(text("SELECT status FROM budget_reservations WHERE project_id=:p"), {"p": p}).scalar_one() == "released"
        assert c.execute(text("SELECT status FROM receipts WHERE project_id=:p AND effect_kind='model.invoke'"), {"p": p}).scalar_one() == "failed"


def test_concurrent_same_version_never_submits_twice(scope):
    engine, p, settings = scope
    entered, finish = Event(), Event()
    def pause():
        entered.set()
        assert finish.wait(8)
    first = Scripted(pause)
    second = Scripted()
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(generate, spender(scope), settings, first)
        try:
            assert entered.wait(8)
            with pytest.raises(TypedDecisionReconciliationRequired):
                generate(spender(scope), settings, second)
        finally:
            finish.set()
        pending.result()
    assert first.calls == 1 and second.calls == 0


def panel(c, p, *, prd=PRD, status="approved", recommendation=None):
    suffix = uuid.uuid4().hex[:12]
    t, m, d = "task_" + suffix, "meeting_" + suffix, "decision_" + suffix
    c.execute(text("INSERT INTO tasks(id,project_id,title,status,dedupe_key) VALUES(:t,:p,'owned','open',:t)"), {"t": t, "p": p})
    c.execute(text("INSERT INTO meeting_runs(id,project_id,task_id,status,phase,directive,state,decision_id) "
                   "VALUES(:m,:p,:t,'completed','done','owned',CAST(:s AS JSONB),:d)"),
              {"m": m, "p": p, "t": t, "s": json.dumps({"prd_markdown": prd}), "d": d})
    rec = recommendation if recommendation is not None else {"action": "execute_agent_tool", "tool": "publish_document", "args": {"markdown": prd}}
    c.execute(text("INSERT INTO decisions(id,project_id,task_id,question,status,approved_by,recommendation) "
                   "VALUES(:d,:p,:t,'owned',:status,'usr_owned_owner',CAST(:rec AS JSONB))"),
              {"d": d, "p": p, "t": t, "status": status, "rec": json.dumps(rec)})
    return {"input": {"adopt_meeting_run_id": m, "adopt_decision_id": d}}, m, d


def test_current_canonical_prd_is_accepted(scope):
    engine, p, _ = scope
    with engine.begin() as c:
        context, _, d = panel(c, p)
        result = arch.approved_prd(c, p, context)
        assert result["markdown"] == PRD and result["decision_id"] == d
        assert result["approved_by"] == "usr_owned_owner" and result["prd_sha256"]


@pytest.mark.parametrize("case", ["foreign_meeting", "foreign_decision", "unrelated_type", "changed_prd", "forged_spec_output", "wrong_task"])
def test_foreign_unrelated_or_stale_approval_cannot_certify_prd(scope, case):
    engine, p, _ = scope
    with engine.begin() as c:
        context, m, d = panel(c, p, status="pending")
        if case in {"foreign_meeting", "foreign_decision"}:
            other = p + "_other"
            c.execute(text("INSERT INTO projects(id,workspace_id,name,slug) VALUES(:q,:p,:q,:q)"),
                      {"q": other, "p": p})
            _, foreign_meeting, foreign_decision = panel(c, other)
            if case == "foreign_meeting":
                context["input"]["adopt_meeting_run_id"] = foreign_meeting
            else:
                context["input"]["adopt_decision_id"] = foreign_decision
        elif case == "unrelated_type":
            c.execute(text("UPDATE decisions SET status='approved', recommendation='{}'::jsonb WHERE id=:d"), {"d": d})
        elif case == "changed_prd":
            c.execute(text("UPDATE decisions SET status='approved' WHERE id=:d"), {"d": d})
            c.execute(text("UPDATE meeting_runs SET state=CAST(:s AS JSONB) WHERE id=:m"), {"m": m, "s": json.dumps({"prd_markdown": PRD + "\nCHANGED"})})
        elif case == "forged_spec_output":
            context["steps"] = {"spec": {"outcome": "approved", "decision_id": d}}
        else:
            context["steps"] = {"panel": {"prd_markdown": PRD, "meeting_run_id": m, "task_id": "wrong"}}
        with pytest.raises(arch.ArchitectureRefused):
            arch.approved_prd(c, p, context, {"spec": {"binding": {"capability": "spec_review"}}})


def test_pending_current_prd_cannot_adopt_older_approved_revision(scope):
    engine, p, _ = scope
    with engine.begin() as c:
        context, m, d = panel(c, p)
        c.execute(text("INSERT INTO decisions(id,project_id,task_id,question,status,recommendation,created_at) "
                       "SELECT id||'_pending',project_id,task_id,'new PRD','pending',recommendation,created_at+interval '1 second' FROM decisions WHERE id=:d"), {"d": d})
        with pytest.raises(arch.ArchitectureRefused):
            arch.approved_prd(c, p, context)


def metered(scope):
    engine, p, _ = scope
    with engine.begin() as c:
        c.execute(text("UPDATE entitlement_sources SET entitlement_kind='usage_based_api', "
                       "cost_mode='usage_based_api' WHERE project_id=:p AND source_id='approved_local_model_host'"), {"p": p})


@pytest.mark.parametrize("case", ["missing_usage", "unpriced_usage", "expired_model_lease", "expired_worker_lease", "stopped_after_call"])
def test_received_reply_cannot_free_unknown_or_expired_liability(scope, case):
    engine, p, settings = scope
    metered(scope)
    checks = []
    def check():
        checks.append(1)
        if case == "stopped_after_call" and len(checks) == 3:
            raise arch.ArchitectureRefused("owned cancellation after submission")
    def call():
        if case == "expired_model_lease":
            with engine.begin() as c:
                c.execute(text("UPDATE model_gateway_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE project_id=:p"), {"p": p})
        elif case == "expired_worker_lease":
            with engine.begin() as c:
                c.execute(text("UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE project_id=:p"), {"p": p})
    usage = {} if case == "missing_usage" else ({"model": "unpriced-owned-model",
             "prompt_tokens": 2, "completion_tokens": 3} if case == "unpriced_usage" else None)
    model = Scripted(call, usage=usage)
    with pytest.raises(TypedDecisionReconciliationRequired):
        generate(spender(scope), settings, model, check=check)
    with pytest.raises(TypedDecisionReconciliationRequired):
        generate(spender(scope), settings, model)
    assert model.calls == 1
    with engine.connect() as c:
        receipt = c.execute(text("SELECT status,result FROM receipts WHERE project_id=:p AND effect_kind='model.invoke'"), {"p": p}).mappings().one()
        assert receipt["status"] == "in_progress"
        assert receipt["result"]["unsettled_architecture_reply"]["text"].startswith("## Context")
        assert c.execute(text("SELECT status FROM budget_reservations WHERE project_id=:p"), {"p": p}).scalar_one() == "active"
        assert c.execute(text("SELECT count(*) FROM cost_receipts WHERE project_id=:p"), {"p": p}).scalar_one() == 0


def test_provider_error_credentials_do_not_reach_durable_diagnostics(scope):
    engine, p, settings = scope
    marker = "ownedCredentialMarker1234567890"
    def fail():
        raise RuntimeError("owned provider body Authorization: Bearer " + marker + " api_key=" + marker)
    with pytest.raises(TypedDecisionReconciliationRequired):
        generate(spender(scope), settings, Scripted(fail))
    with engine.connect() as c:
        for table, column in (("decisions", "cost_summary"), ("decisions", "recommendation"), ("events", "data"), ("receipts", "result"), ("receipts", "error")):
            values = c.execute(text(f"SELECT {column} FROM {table} WHERE project_id=:p"), {"p": p}).scalars().all()
            assert marker not in json.dumps(values, default=str)


@pytest.mark.parametrize("cost", [False, True, "0", "3000", -1, 1.5])
def test_invalid_paid_explicit_cost_retains_unresolved_liability(scope, cost):
    engine, p, settings = scope
    metered(scope)
    model = Scripted(usage={"cost_usd_micros": cost})
    with pytest.raises(TypedDecisionReconciliationRequired):
        generate(spender(scope), settings, model)
    assert model.calls == 1
    with engine.connect() as c:
        assert c.execute(text("SELECT status FROM budget_reservations WHERE project_id=:p"), {"p": p}).scalar_one() == "active"
        assert c.execute(text("SELECT count(*) FROM cost_receipts WHERE project_id=:p"), {"p": p}).scalar_one() == 0
        assert c.execute(text("SELECT status FROM receipts WHERE project_id=:p AND effect_kind='model.invoke'"), {"p": p}).scalar_one() == "in_progress"


@pytest.mark.parametrize("cost", [0, 3000])
def test_typed_nonnegative_paid_cost_settles(scope, cost):
    engine, p, settings = scope
    metered(scope)
    generate(spender(scope), settings, Scripted(usage={"cost_usd_micros": cost}))
    with engine.connect() as c:
        assert c.execute(text("SELECT actual_usd_micros FROM cost_receipts WHERE project_id=:p"), {"p": p}).scalar_one() == cost


@pytest.mark.parametrize("cost", [False, "0", -1])
def test_invalid_explicit_cost_cannot_override_valid_priced_tokens(scope, cost):
    from boss_engine.services.openai_adapter import estimate_cost_usd_micros
    engine, p, settings = scope
    metered(scope)
    tokens = {"model": "gpt-4o", "prompt_tokens": 2000, "completion_tokens": 1000}
    expected = estimate_cost_usd_micros(tokens)
    assert expected > 0
    generate(spender(scope), settings, Scripted(usage={**tokens, "cost_usd_micros": cost}))
    with engine.connect() as c:
        assert c.execute(text("SELECT actual_usd_micros FROM cost_receipts WHERE project_id=:p"), {"p": p}).scalar_one() == expected


@pytest.mark.parametrize("expired", ["model", "job"])
def test_pre_dispatch_expiry_releases_only_known_unused_liability(scope, expired):
    engine, p, settings = scope
    metered(scope)
    model = Scripted()
    checks = []
    def check():
        checks.append(1)
        if len(checks) == 2:
            with engine.begin() as c:
                table, field = ("model_gateway_leases", "expires_at") if expired == "model" else ("jobs", "lease_expires_at")
                c.execute(text(f"UPDATE {table} SET {field}=clock_timestamp()-interval '1 second' WHERE project_id=:p"), {"p": p})
    with pytest.raises(TypedDecisionSpendError):
        generate(spender(scope), settings, model, check=check)
    assert model.calls == 0
    with engine.connect() as c:
        assert c.execute(text("SELECT status FROM budget_reservations WHERE project_id=:p"), {"p": p}).scalar_one() == "released"
        assert c.execute(text("SELECT status FROM receipts WHERE project_id=:p AND effect_kind='model.invoke'"), {"p": p}).scalar_one() == "failed"


@pytest.mark.parametrize("changed", ["worker", "model_token"])
def test_pre_dispatch_cleanup_refuses_reclaimed_authority(scope, changed):
    engine, p, settings = scope
    metered(scope)
    model = Scripted()
    checks = []
    def check():
        checks.append(1)
        if len(checks) == 2:
            with engine.begin() as c:
                c.execute(text("UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE project_id=:p"), {"p": p})
                if changed == "worker":
                    c.execute(text("UPDATE jobs SET lease_owner='other-worker' WHERE project_id=:p"), {"p": p})
                else:
                    c.execute(text("UPDATE model_gateway_leases SET lease_token_hash='changed' WHERE project_id=:p"), {"p": p})
    with pytest.raises(TypedDecisionReconciliationRequired):
        generate(spender(scope), settings, model, check=check)
    assert model.calls == 0
    with engine.connect() as c:
        assert c.execute(text("SELECT status FROM budget_reservations WHERE project_id=:p"), {"p": p}).scalar_one() == "active"
        assert c.execute(text("SELECT status FROM receipts WHERE project_id=:p AND effect_kind='model.invoke'"), {"p": p}).scalar_one() == "in_progress"


def workflow(c, p, *, actor="usr_owned_owner"):
    from .test_workflow_capability_runner import _publish, _start
    key = "arch_owned_" + uuid.uuid4().hex[:10]
    _, v = _publish(c, p, key, [{"id": "arch", "kind": "capability", "next": [],
        "binding": {"capability": "architecture", "inputs": {}}}], "arch")
    r = _start(c, p, v["id"])
    c.execute(text("UPDATE workflow_runs SET status='running',actor=:a WHERE id=:r"), {"r": r["id"], "a": actor})
    c.execute(text("UPDATE workflow_step_runs SET status='leased',lease_owner='owned-runner', "
                   "lease_expires_at=clock_timestamp()+interval '120 seconds',attempt=1 WHERE workflow_run_id=:r"), {"r": r["id"]})
    return r["id"]


@pytest.mark.parametrize("case", ["cancelled", "foreign_project", "expired_workflow_lease", "missing_owner", "unclaimed", "revision_without_person"])
def test_workflow_authority_rejects_stopped_foreign_or_unleased_work(scope, case):
    engine, p, _ = scope
    with engine.begin() as c:
        r = workflow(c, p)
        assert arch._draft_authority(c, p, r, "arch", version=1)["actor"] == "usr_owned_owner"
        version = 1
        if case == "cancelled":
            c.execute(text("UPDATE workflow_runs SET status='cancelled' WHERE id=:r"), {"r": r})
        elif case == "foreign_project":
            p = "other-project"
        elif case == "expired_workflow_lease":
            c.execute(text("UPDATE workflow_step_runs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE workflow_run_id=:r"), {"r": r})
        elif case == "missing_owner":
            c.execute(text("UPDATE workflow_step_runs SET lease_owner=NULL WHERE workflow_run_id=:r"), {"r": r})
        elif case == "unclaimed":
            c.execute(text("UPDATE workflow_step_runs SET status='ready' WHERE workflow_run_id=:r"), {"r": r})
        else:
            version = 2
            c.execute(text("UPDATE workflow_step_runs SET status='waiting' WHERE workflow_run_id=:r"), {"r": r})
        with pytest.raises(arch.ArchitectureRefused):
            arch._draft_authority(c, p, r, "arch", version=version, review_id="unowned-review")


def test_production_draft_binds_real_workflow_principal_and_job_owner(scope, monkeypatch):
    engine, p, settings = scope
    with engine.begin() as c:
        r = workflow(c, p)
        context, _, _ = panel(c, p)
        c.execute(text("UPDATE workflow_runs SET input=CAST(:i AS JSONB) WHERE id=:r"),
                  {"r": r, "i": json.dumps(context["input"])})
    def proof_owner():
        with engine.connect() as observer:
            assert observer.execute(text("SELECT lease_owner FROM jobs WHERE project_id=:p"),
                                    {"p": p}).scalar_one() == "owned-runner"
    model = Scripted(proof_owner)
    monkeypatch.setattr(arch, "architect_adapter", lambda *a, **k: model)
    with engine.connect() as c:
        result = arch.draft(c, settings, p, architect="chief_architect", run_id=r,
                            step_id="arch", prd=PRD, title="Owned architecture")
        assert "Context" in result["markdown"]
    with engine.connect() as c:
        receipt = c.execute(text("SELECT result FROM receipts WHERE project_id=:p AND effect_kind='model.invoke'"), {"p": p}).scalar_one()
        assert receipt["workflow_actor"] == "usr_owned_owner"
        job = c.execute(text("SELECT payload,lease_owner,status FROM jobs WHERE project_id=:p"), {"p": p}).mappings().one()
        assert job["payload"]["actor"] == "usr_owned_owner" and job["lease_owner"] is None
        assert job["payload"]["workflow_run_id"] == r and job["payload"]["agent_id"] == "chief_architect"
        assert job["status"] == "succeeded"


def test_run_context_cannot_replace_canonical_panel_with_forged_output(scope):
    engine, p, _ = scope
    with engine.begin() as c:
        context, _, _ = panel(c, p)
        r = workflow(c, p)
        # The supplied context points to a real approved PRD, but this run never
        # adopted it and no succeeded step produced it. Input isn't authority.
        with pytest.raises(arch.ArchitectureRefused):
            arch.approved_prd(c, p, context, run_id=r)


def test_router_failover_cannot_submit_second_source_on_one_receipt(scope):
    from boss_engine.services.model_router.failover import FailoverChatAdapter
    engine, p, settings = scope
    metered(scope)
    primary = Scripted(lambda: (_ for _ in ()).throw(TimeoutError("owned timeout")))
    fallback = Scripted()
    reroutes = []
    def reroute(*args):
        reroutes.append(1)
        return fallback, None
    wrapper = FailoverChatAdapter(primary, SimpleNamespace(engine="ollama", model="owned", id="owned", mode="smart"),
                                  reroute, stage="architecture", record=False)
    wrapper._boss_choice = primary._boss_choice
    with pytest.raises(TypedDecisionReconciliationRequired):
        generate(spender(scope), settings, wrapper)
    assert primary.calls == 1 and fallback.calls == 0 and not reroutes


def test_prd_change_during_adapter_construction_refuses_before_submission(scope, monkeypatch):
    engine, p, settings = scope
    with engine.begin() as c:
        r = workflow(c, p)
        context, m, _ = panel(c, p)
        c.execute(text("UPDATE workflow_runs SET input=CAST(:i AS JSONB) WHERE id=:r"),
                  {"r": r, "i": json.dumps(context["input"])})
    model = Scripted()
    with engine.begin() as c:
        def route(*args, **kwargs):
            c.execute(text("UPDATE meeting_runs SET state=CAST(:s AS JSONB) WHERE id=:m"),
                      {"m": m, "s": json.dumps({"prd_markdown": PRD + "\nNEW REVISION"})})
            return model
        monkeypatch.setattr(arch, "architect_adapter", route)
        with pytest.raises(arch.ArchitectureDraftError):
            arch.draft(c, settings, p, architect="chief_architect", run_id=r,
                       step_id="arch", prd=PRD, title="Owned architecture")
        assert model.calls == 0
        assert c.execute(text("SELECT count(*) FROM model_gateway_invocations WHERE project_id=:p"), {"p": p}).scalar_one() == 0
