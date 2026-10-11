"""Real PostgreSQL transitions and enforced identity. Delivery records are owned fixtures;
no provider was contacted and these fixtures are not qualification of a useful delivery.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity.principal import PrincipalKind
from boss_engine.services import deliverable_review as reviews

HEAD = "a" * 40


@pytest.fixture()
def delivery(engine):
    world = IdentityWorld.create(engine)
    owner = world.person("result owner")
    world.bind(owner, "owner", project=world.project_a)
    ids = {name: f"{name}_{uuid4().hex}" for name in ("run", "decision", "receipt", "artifact", "reserve")}
    url = "https://example.test/owned/result/pull/1"
    rec = {"action": "review_pull_request", "run_id": ids["run"], "head_sha": HEAD,
           "pull_request_url": url, "receipt_id": ids["receipt"],
           "test_report_artifact_id": ids["artifact"]}
    with engine.begin() as c:
        c.execute(text("INSERT INTO budget_reservations (id,project_id,category,amount_usd_micros,expires_at) "
                       "VALUES (:id,:p,'test',80000,now()-interval '1 day')"),
                  {"id": ids["reserve"], "p": world.project_a})
        c.execute(text("INSERT INTO receipts (id,org_id,workspace_id,project_id,dedupe_key,effect_kind,payload_hash,status,result) "
                       "VALUES (:id,:o,:w,:p,:id,'github.pull_request.create','fixture','succeeded',CAST(:result AS JSONB))"),
                  {"id": ids["receipt"], "o": world.org, "w": world.workspace, "p": world.project_a,
                   "result": json.dumps({"decision_id": ids["decision"], "pull_request_url": url})})
        c.execute(text("INSERT INTO artifacts(id,project_id,artifact_type,title,content) "
                       "VALUES(:id,:p,'test_report','Owned evidence fixture',CAST(:body AS JSONB))"),
                  {"id": ids["artifact"], "p": world.project_a,
                   "body": json.dumps({"head_sha": HEAD, "status": "passed", "tests_added_or_run": ["pytest owned"]})})
        c.execute(text("INSERT INTO decisions(id,project_id,question,recommendation) "
                       "VALUES(:id,:p,'Owned fixture merge decision',CAST(:rec AS JSONB))"),
                  {"id": ids["decision"], "p": world.project_a, "rec": json.dumps(rec)})
        c.execute(text("INSERT INTO code_task_runs(id,project_id,title,status,phase,decision_id,pull_request_url,spec,reservation_id) "
                       "VALUES(:id,:p,'Owned fixture result','completed','done',:d,:url,CAST(:spec AS JSONB),:b)"),
                  {"id": ids["run"], "p": world.project_a, "d": ids["decision"], "url": url,
                   "b": ids["reserve"], "spec": json.dumps({"acceptance_criteria": ["Export the selected records"]})})
    return world, owner, ids


def view(engine, world, ids):
    with engine.begin() as c:
        return reviews.snapshot(c, world.project_a, ids["run"])


def body(snapshot, **overrides):
    return {"snapshot_hash": snapshot["snapshot_hash"], "outcome": "accepted", "note": "Useful owned fixture",
            "submission_id": uuid4().hex, **overrides}


def test_human_review_is_durable_idempotent_and_changes_no_authority(client, engine, delivery):
    world, owner, ids = delivery
    path = f"/v1/code-tasks/{ids['run']}/result-review"
    with world.acting_as(world.ctx(owner)):
        got = client.get(path)
        assert got.status_code == 200, got.text
        payload = body(got.json())
        first = client.post(path, json=payload)
        assert first.status_code == 200, first.text
        again = client.post(path, json=payload)
        assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
        saved = client.get(path).json()["history"]
    assert len(saved) == 1 and saved[0]["actor"] == owner.id
    assert saved[0]["snapshot"]["delivered_head"] == HEAD
    with engine.begin() as c:
        assert c.execute(text("SELECT status FROM decisions WHERE id=:id"), {"id": ids["decision"]}).scalar() == "pending"
        assert c.execute(text("SELECT status FROM budget_reservations WHERE id=:id"), {"id": ids["reserve"]}).scalar() == "active"
        assert c.execute(text("SELECT count(*) FROM jobs WHERE project_id=:p"), {"p": world.project_a}).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM receipts WHERE project_id=:p"), {"p": world.project_a}).scalar() == 1
        assert c.execute(text("SELECT count(*) FROM events WHERE project_id=:p AND event_type='code_task.result_reviewed'"), {"p": world.project_a}).scalar() == 1


def test_changes_requested_keeps_history_without_starting_work(client, engine, delivery):
    world, owner, ids = delivery
    path = f"/v1/code-tasks/{ids['run']}/result-review"
    with world.acting_as(world.ctx(owner)):
        original = client.get(path).json()
        accepted = client.post(path, json=body(original))
        changed = client.post(path, json=body(original, outcome="changes_requested", note="Only add a CSV header"))
        assert accepted.status_code == changed.status_code == 200
        history = client.get(path).json()["history"]
        refused = client.post(path, json=body(original, outcome="changes_requested", note="  "))
        assert refused.status_code == 422
    assert [r["outcome"] for r in history] == ["changes_requested", "accepted"]
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM jobs WHERE project_id=:p"), {"p": world.project_a}).scalar() == 0


@pytest.mark.parametrize("mutation", ["head", "checks", "check_details", "cost"])
def test_stale_recorded_revision_checks_or_cost_cannot_be_accepted(client, engine, delivery, mutation):
    world, owner, ids = delivery
    old = view(engine, world, ids)
    with engine.begin() as c:
        if mutation == "head":
            c.execute(text("UPDATE decisions SET recommendation=jsonb_set(recommendation,'{head_sha}',to_jsonb(CAST(:h AS TEXT))) WHERE id=:id"), {"id": ids["decision"], "h": "b" * 40})
        elif mutation == "checks":
            c.execute(text("UPDATE artifacts SET content=jsonb_set(content,'{status}','\"failed\"') WHERE id=:id"), {"id": ids["artifact"]})
        elif mutation == "check_details":
            c.execute(text("UPDATE artifacts SET content=content || '{\"checks\":[{\"name\":\"unit\",\"conclusion\":\"failure\"}]}' WHERE id=:id"), {"id": ids["artifact"]})
        else:
            c.execute(text("UPDATE budget_reservations SET amount_usd_micros=90000 WHERE id=:id"), {"id": ids["reserve"]})
    with world.acting_as(world.ctx(owner)):
        got = client.post(f"/v1/code-tasks/{ids['run']}/result-review", json=body(old))
    assert got.status_code == 409, got.text
    assert "Reload" in got.json()["detail"]


@pytest.mark.parametrize("broken", ["head", "receipt", "receipt_kind", "foreign_artifact", "not_completed"])
def test_missing_or_foreign_evidence_stays_honest(engine, delivery, broken):
    world, _, ids = delivery
    with engine.begin() as c:
        if broken == "head":
            c.execute(text("UPDATE decisions SET recommendation=recommendation-'head_sha' WHERE id=:id"), {"id": ids["decision"]})
        elif broken == "receipt":
            c.execute(text("UPDATE receipts SET status='in_progress' WHERE id=:id"), {"id": ids["receipt"]})
        elif broken == "receipt_kind":
            c.execute(text("UPDATE receipts SET effect_kind='owned_wrong_kind' WHERE id=:id"), {"id": ids["receipt"]})
        elif broken == "foreign_artifact":
            c.execute(text("UPDATE artifacts SET project_id=:p WHERE id=:id"), {"id": ids["artifact"], "p": world.project_b})
        else:
            c.execute(text("UPDATE code_task_runs SET status='running' WHERE id=:id"), {"id": ids["run"]})
    got = view(engine, world, ids)
    if broken == "foreign_artifact":
        assert got["snapshot"]["checks"]["state"] == "not_recorded"
        assert got["snapshot"]["checks"]["artifact_id"] is None
    else:
        assert not got["reviewable"]
        with engine.begin() as c, pytest.raises(reviews.ReviewConflict):
            reviews.record(c, project_id=world.project_a, run_id=ids["run"], actor="owned-human", **body(got))


def test_check_head_mismatch_is_invalidated_and_absent_cost_is_unknown(engine, delivery):
    world, _, ids = delivery
    with engine.begin() as c:
        c.execute(text("UPDATE artifacts SET content=jsonb_set(content,'{head_sha}',to_jsonb(CAST(:h AS TEXT))) WHERE id=:id"), {"id": ids["artifact"], "h": "b" * 40})
    got = view(engine, world, ids)["snapshot"]
    assert got["checks"]["state"] == "invalidated"
    assert got["checks"]["checked_head"] != got["delivered_head"]
    assert got["cost"]["observed_usd_micros"] is None
    assert got["cost"]["held_usd_micros"] == 80000  # expiry does not release exposure
    assert got["cost"]["confidence"] == "incomplete"


def test_cost_estimate_actual_and_outstanding_hold_are_separate(engine, delivery):
    world, _, ids = delivery
    with engine.begin() as c:
        c.execute(text("INSERT INTO cost_receipts(id,project_id,reservation_id,category,skill_or_model_id,estimated_usd_micros,actual_usd_micros,status) "
                       "VALUES(:id,:p,:b,'model','owned-fixture',70000,30000,'actual')"),
                  {"id": uuid4().hex, "p": world.project_a, "b": ids["reserve"]})
    cost = view(engine, world, ids)["snapshot"]["cost"]
    assert (cost["observed_usd_micros"], cost["estimated_usd_micros"], cost["held_usd_micros"]) == (30000, 70000, 80000)
    assert cost["confidence"] == "incomplete"



def test_legacy_included_cost_is_not_cash_and_equivalent_value_is_preserved(engine, delivery):
    world, _, ids = delivery
    with engine.begin() as c:
        # Historical/backfilled included rows kept the API value in actual; newer rows
        # store zero cash and an explicit equivalent value. Neither is paid money.
        for actual, equivalent, included in [(30000, None, True), (0, 40000, True), (5000, None, False)]:
            c.execute(text("INSERT INTO cost_receipts(id,project_id,reservation_id,category,skill_or_model_id,"
                           "estimated_usd_micros,actual_usd_micros,equivalent_usd_micros,included_in_subscription,status) "
                           "VALUES(:id,:p,:b,'model','owned-fixture',60000,:actual,:equivalent,:included,'actual')"),
                      {"id": uuid4().hex, "p": world.project_a, "b": ids["reserve"],
                       "actual": actual, "equivalent": equivalent, "included": included})
    cost = view(engine, world, ids)["snapshot"]["cost"]
    assert cost["observed_usd_micros"] == 5000
    assert cost["estimated_usd_micros"] == 60000
    assert cost["included_equivalent_usd_micros"] == 70000
    assert cost["included_calls"] == 2
    assert cost["held_usd_micros"] == 80000


def test_estimated_unknown_cost_is_not_zero_and_job_holds_are_included(engine, delivery):
    world, _, ids = delivery
    job = uuid4().hex
    reservation = uuid4().hex
    with engine.begin() as c:
        c.execute(text("INSERT INTO jobs(id,project_id,kind,payload) VALUES(:id,:p,'owned_fixture',CAST(:payload AS JSONB))"),
                  {"id": job, "p": world.project_a, "payload": json.dumps({"run_id": ids["run"]})})
        c.execute(text("INSERT INTO budget_reservations(id,project_id,job_id,category,amount_usd_micros,expires_at) "
                       "VALUES(:id,:p,:j,'model',9000,now()+interval '1 hour')"),
                  {"id": reservation, "p": world.project_a, "j": job})
        c.execute(text("INSERT INTO cost_receipts(id,project_id,reservation_id,category,skill_or_model_id,estimated_usd_micros) "
                       "VALUES(:id,:p,:b,'model','owned-fixture',9000)"),
                  {"id": uuid4().hex, "p": world.project_a, "b": reservation})
    cost = view(engine, world, ids)["snapshot"]["cost"]
    assert cost["observed_usd_micros"] is None
    assert cost["estimated_usd_micros"] == 9000 and cost["held_usd_micros"] == 89000
    assert cost["unresolved_records"] == 1


@pytest.mark.parametrize("status", ["not_run", "skipped", "failed"])
def test_human_usefulness_does_not_relabel_technical_checks(engine, delivery, status):
    world, owner, ids = delivery
    with engine.begin() as c:
        c.execute(text("UPDATE artifacts SET content=jsonb_set(content,'{status}',to_jsonb(CAST(:s AS TEXT))) WHERE id=:id"),
                  {"id": ids["artifact"], "s": status})
    current = view(engine, world, ids)
    assert current["snapshot"]["checks"]["state"] == status
    with engine.begin() as c:
        reviews.record(c, project_id=world.project_a, run_id=ids["run"], actor=owner.id, **body(current))
        saved = reviews.history(c, world.project_a, ids["run"])
    assert saved[0]["snapshot"]["checks"]["state"] == status


def test_review_and_audit_commit_atomically(engine, delivery, monkeypatch):
    world, owner, ids = delivery
    current = view(engine, world, ids)

    def refuse_audit(*args, **kwargs):
        raise RuntimeError("owned audit failure injection")

    monkeypatch.setattr(reviews, "emit_event", refuse_audit)
    with pytest.raises(RuntimeError, match="owned audit failure"), engine.begin() as c:
        reviews.record(c, project_id=world.project_a, run_id=ids["run"], actor=owner.id, **body(current))
    with engine.begin() as c:
        assert reviews.history(c, world.project_a, ids["run"]) == []


@pytest.mark.parametrize("kind", [PrincipalKind.SERVICE_ACCOUNT, PrincipalKind.AGENT, PrincipalKind.SYSTEM])
def test_machine_with_permission_cannot_supply_human_acceptance(client, engine, delivery, kind):
    world, _, ids = delivery
    machine = world.principal("owned machine", kind=kind)
    world.bind(machine, "owner", project=world.project_a)
    with world.acting_as(world.ctx(machine)):
        got = client.post(f"/v1/code-tasks/{ids['run']}/result-review", json=body(view(engine, world, ids)))
    assert got.status_code == 403, got.text
    if kind == PrincipalKind.SERVICE_ACCOUNT:
        assert "Sign in as a person" in got.text


def test_foreign_tenant_viewer_and_claimed_actor_cannot_review(client, engine, delivery):
    world, owner, ids = delivery
    outsider = world.person("other project")
    world.bind(outsider, "owner", project=world.project_b)
    reader = world.person("reader")
    world.bind(reader, "viewer", project=world.project_a)
    path = f"/v1/code-tasks/{ids['run']}/result-review"
    payload = body(view(engine, world, ids))
    with world.acting_as(world.ctx(outsider)):
        assert client.get(path).status_code in (403, 404)
        assert client.post(path, json=payload).status_code in (403, 404)
    with world.acting_as(world.ctx(reader)):
        assert client.get(path).status_code == 200
        assert client.post(path, json=payload).status_code == 403
    with world.acting_as(world.ctx(owner)):
        assert client.post(path, json={**payload, "actor": outsider.id}).status_code == 422


def test_parallel_duplicate_records_one_review_and_one_event(engine, delivery):
    world, owner, ids = delivery
    payload = body(view(engine, world, ids))
    barrier = threading.Barrier(2)

    def attempt():
        with engine.begin() as c:
            barrier.wait(timeout=10)
            return reviews.record(c, project_id=world.project_a, run_id=ids["run"], actor=owner.id, **payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: attempt(), range(2)))
    assert first["id"] == second["id"] and first["reused"] != second["reused"]
    with engine.begin() as c:
        assert len(reviews.history(c, world.project_a, ids["run"])) == 1
        with pytest.raises(reviews.ReviewConflict):
            reviews.record(c, project_id=world.project_a, run_id=ids["run"], actor=owner.id,
                           **{**payload, "note": "conflicting duplicate"})
