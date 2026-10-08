"""C-1 / C-2 / C-3 (id-sec-final2): the side doors around the approval rule and the job-kind map.

C-1: POST /v1/changesets/{id}/approve flipped the ChangeSet's tier-3 decision with a raw UPDATE —
no seat, no second person. Every approval now goes through decisions.transition ->
approvals.require_approval; the static scan below fails on any other writer of
`decisions.status = 'approved'`.

C-2: a dead-letter requeue / retry / remediation re-created a job of ANY kind with
`deadletter:manage` alone (and skipped step-up). Every path that queues a caller-chosen kind now
runs `job_permissions.require_job_kind`; the static scan fails on a new path that does not. An
event subscription with a job sink is the same door and is checked too.

C-3: the first of two tier-3 approvals was recorded before the content-hash check, and counted at
completion even when its person had since lost the seat.
"""
from __future__ import annotations

import ast
import json
import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import identity_guard
from apps.api.app import main as api_main
from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld

ROOT = Path(__file__).resolve().parents[3]
H = {"sec-fetch-site": "same-origin"}


@pytest.fixture
def world(engine, monkeypatch) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.claimed()
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(identity_guard, "presents_identity_credential", lambda conn: True)
    return w


@pytest.fixture
def cast(world):
    owner = world.person("Owner")
    world.bind(owner, "owner")
    alice, bob, carol = world.person("Alice"), world.person("Bob"), world.person("Carol")
    for p in (alice, bob, carol):
        world.bind(p, "operator", project=world.project_a)
    mallory = world.person("Mallory")
    world.bind(mallory, "admin", project=world.project_a)
    with world.engine.begin() as db:
        for seat, p in (("ceo", alice), ("cao", bob)):
            db.execute(text("INSERT INTO identity_approval_seats (org_id, seat_key, principal_id, assigned_by) "
                            "VALUES (:o, :s, :p, 'test')"), {"o": world.org, "s": seat, "p": p.id})
    return dict(owner=owner, alice=alice, bob=bob, carol=carol, mallory=mallory)


def _client():
    return TestClient(app, raise_server_exceptions=False)


def _post(world, ctx, path, body):
    with world.acting_as(ctx):
        return _client().post(path, json=body, headers=H)


def _one(world, sql, **params):
    with world.engine.connect() as db:
        return db.execute(text(sql), params).first()


def _decision(world, tier=3, preview_hash=None):
    did = f"dec_{uuid.uuid4().hex[:10]}"
    rec = {"action_preview": {"content_hash": preview_hash, "text": "send A"}} if preview_hash else None
    with world.engine.begin() as db:
        db.execute(text("INSERT INTO decisions (id, project_id, question, risk_tier, status, default_action, "
                        "recommendation) VALUES (:d, :p, 'Ship it?', :t, 'pending', 'hold', CAST(:r AS JSONB))"),
                   {"d": did, "p": world.project_a, "t": tier, "r": json.dumps(rec or {})})
    return did


def _set_preview(world, did, h):
    with world.engine.begin() as db:
        db.execute(text("UPDATE decisions SET recommendation = CAST(:r AS JSONB) WHERE id = :d"),
                   {"d": did, "r": json.dumps({"action_preview": {"content_hash": h, "text": h}})})


def _approvals(world, did):
    with world.engine.connect() as db:
        return {r[0]: r[1] for r in db.execute(text(
            "SELECT principal_id, content_hash FROM identity_decision_approvals WHERE decision_id = :d"),
            {"d": did}).all()}


# ---- C-1 -----------------------------------------------------------------------------------------

def _tier3_changeset(world, ctx, actor):
    created = _post(world, ctx, "/v1/changesets", {
        "project_id": world.project_a, "provider_account_id": None, "title": "t", "summary": "s",
        "operations": [{"skill_id": "cloudflare.dns.record.upsert", "action": "upsert",
                        "resource_type": "dns_record", "resource_ref": "www.example.com",
                        "desired_state": {"name": "www", "type": "A", "content": "203.0.113.66"},
                        "risk_tier": 3, "destructive": False, "production_affecting": True}],
        "expected_cost_usd_micros": 0, "risk_tier": 3, "approval_required": True,
        "rollback_plan": ["r"], "actor": actor})
    assert created.status_code < 300, created.text
    cs = created.json()["id"]
    did = _one(world, "SELECT approval_decision_id FROM changesets WHERE id = :c", c=cs)[0]
    return cs, did


def test_changeset_approve_cannot_skip_the_seat_or_the_second_person(world, cast):
    m = world.ctx(cast["mallory"])
    cs, did = _tier3_changeset(world, m, cast["mallory"].id)
    assert _post(world, m, f"/v1/decisions/{did}/approve", {"note": "ok"}).status_code == 403   # control
    ap = _post(world, m, f"/v1/changesets/{cs}/approve", {"actor": cast["mallory"].id})
    assert ap.status_code == 403 and ap.json()["error"] == "seat_required", ap.text
    assert _one(world, "SELECT status FROM decisions WHERE id = :d", d=did)[0] == "pending"
    assert _one(world, "SELECT status FROM changesets WHERE id = :c", c=cs)[0] != "approved"
    assert _approvals(world, did) == {}
    # the decision approved by two seated people the proper way; then the ChangeSet goes through
    assert _post(world, world.ctx(cast["alice"]), f"/v1/decisions/{did}/approve", {"note": "ok"}).status_code == 202
    assert _post(world, world.ctx(cast["bob"]), f"/v1/decisions/{did}/approve", {"note": "ok"}).status_code == 200
    ok = _post(world, m, f"/v1/changesets/{cs}/approve", {"actor": cast["mallory"].id})
    assert ok.status_code == 200, ok.text
    assert _one(world, "SELECT status FROM changesets WHERE id = :c", c=cs)[0] == "approved"


def _approved_writers() -> set[tuple[str, str]]:
    """(file, function) of every SQL string that sets decisions.status to 'approved' (or to a bound
    value) or inserts a decision in any status but 'pending'."""
    found: set[tuple[str, str]] = set()
    upd = re.compile(r"UPDATE\s+decisions\b(?P<body>.*?)(WHERE|$)", re.I | re.S)
    for base in ("engine/boss_engine", "apps/api/app", "apps/worker"):
        for path in (ROOT / base).rglob("*.py"):
            src = path.read_text()
            if "decisions" not in src:
                continue
            for fn in ast.walk(ast.parse(src)):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(fn):
                    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                        continue
                    sql = node.value
                    for m in upd.finditer(sql):
                        body = m.group("body")
                        if re.search(r"\bstatus\s*=\s*('approved'|:\w+)", body, re.I) and \
                                not re.search(r"\bstatus\s*=\s*:(withdrawn)\b", body):
                            found.add((str(path.relative_to(ROOT)), fn.name))
                    ins = re.search(r"INSERT\s+INTO\s+decisions\s*\((?P<cols>[^)]*)\)\s*VALUES\s*\((?P<vals>.*?)\)\s*(RETURNING|ON|$)",
                                    sql, re.I | re.S)
                    if ins and "'pending'" not in ins.group("vals"):
                        found.add((str(path.relative_to(ROOT)), fn.name))
    return found


def test_no_code_approves_a_decision_except_through_transition():
    """Static: the ONLY writers of an approved decision are decisions.transition (seat, human,
    two-person rule via approvals.require_approval) and decisions.approve_by_policy (a setting,
    refused for any tier needing a seat or a second person)."""
    assert _approved_writers() == {
        ("engine/boss_engine/services/decisions.py", "transition"),
        ("engine/boss_engine/services/decisions.py", "approve_by_policy"),
    }


def test_a_policy_never_approves_a_tier_that_needs_a_seat(world, cast):
    from boss_engine.services.decisions import approve_by_policy

    for tier, expect in ((1, True), (2, False), (3, False)):
        did = _decision(world, tier=tier)
        with world.engine.begin() as db:
            assert approve_by_policy(db, did, project_id=world.project_a, actor="autonomy:x", note="n") is expect


# ---- C-2 -----------------------------------------------------------------------------------------

def _dead_job(world, kind="infra.droplet.delete"):
    jid, dlid = f"job_{uuid.uuid4().hex[:10]}", f"dl_{uuid.uuid4().hex[:10]}"
    with world.engine.begin() as db:
        db.execute(text("INSERT INTO jobs (id, project_id, kind, status, payload) VALUES "
                        "(:j, :p, :k, 'deadlettered', CAST(:pl AS JSONB))"),
                   {"j": jid, "p": world.project_a, "k": kind, "pl": json.dumps({"droplet_id": "victim-prod-db"})})
        db.execute(text("INSERT INTO deadletters (id, project_id, source_kind, source_id, reason) VALUES "
                        "(:d, :p, 'job', :j, 'provider timeout')"), {"d": dlid, "p": world.project_a, "j": jid})
    return jid, dlid


def _new_jobs(world, jid, kind="infra.droplet.delete"):
    with world.engine.connect() as db:
        return db.execute(text("SELECT count(*) FROM jobs WHERE project_id = :p AND id <> :j AND kind = :k"),
                          {"p": world.project_a, "j": jid, "k": kind}).scalar()


@pytest.mark.parametrize("path,body", [
    ("/v1/deadletters/{dl}/requeue", {"reason": "retry"}),
    ("/v1/deadletters/{dl}/retry", {"reason": "retry"}),
    ("/v1/deadletters/{dl}/remediations", {"action": "retry", "reason": "retry"}),
])
def test_deadletter_requeue_needs_the_job_kinds_permission(world, cast, path, body):
    rid = world.custom_role("dlq", ("deadletter:manage", "project:read", "job:read"))
    dave = world.person("Dave")
    world.bind(dave, rid, project=world.project_a)
    jid, dlid = _dead_job(world)
    d = world.ctx(dave)
    assert _post(world, d, "/v1/jobs", {"project_id": world.project_a, "kind": "infra.droplet.delete",
                                        "payload": {}}).status_code == 403                    # control
    r = _post(world, d, path.format(dl=dlid), {**body, "actor": dave.id})
    assert r.status_code == 403, r.text
    assert _new_jobs(world, jid) == 0


@pytest.mark.parametrize("path", ["/v1/deadletters/{dl}/requeue", "/v1/deadletters/{dl}/retry"])
def test_deadletter_requeue_of_a_critical_kind_needs_step_up(world, cast, path):
    jid, dlid = _dead_job(world)
    stale = world.ctx(cast["mallory"], mfa=False)
    assert _post(world, stale, "/v1/jobs", {"project_id": world.project_a, "kind": "infra.droplet.delete",
                                            "payload": {}}).status_code == 401              # control
    r = _post(world, stale, path.format(dl=dlid), {"actor": cast["mallory"].id, "reason": "r"})
    assert r.status_code == 401 and "step_up" in r.text, r.text
    assert _new_jobs(world, jid) == 0
    fresh = world.ctx(cast["mallory"])
    ok = _post(world, fresh, path.format(dl=dlid), {"actor": cast["mallory"].id, "reason": "r"})
    assert ok.status_code in (200, 201), ok.text
    assert _new_jobs(world, jid) == 1


def test_a_job_sink_subscription_needs_the_job_kinds_permission(world, cast):
    rid = world.custom_role("integ", ("integration:manage", "project:read"))
    erin = world.person("Erin")
    world.bind(erin, rid, project=world.project_a)
    r = _post(world, world.ctx(erin), "/v1/event-subscriptions", {
        "project_id": world.project_a, "event_type": "meeting.completed", "sink_kind": "job",
        "sink_ref": "infra.droplet.delete", "actor": erin.id})
    assert r.status_code == 403, r.text
    assert _one(world, "SELECT count(*) FROM event_subscriptions WHERE project_id = :p AND sink_ref = "
                "'infra.droplet.delete'", p=world.project_a)[0] == 0


def test_every_path_that_queues_a_caller_chosen_kind_authorizes_it():
    """Static: a call to create_job/enqueue_job whose kind is not a literal (or a module constant)
    sits in a function that calls require_job_kind — or is listed here with why it is safe."""
    safe = {
        ("engine/boss_engine/services/jobs.py", "create_job"),           # the wrapper itself
        ("apps/api/app/governed_effect.py", "record_governed_effect"),   # f"governed.{code-chosen}"
        ("engine/boss_engine/services/orchestrator_next_move.py", "_advance_task"),  # a fixed map
        # sink checked at set-up AND at dispatch (job_sink_held, asserted below)
        ("engine/boss_engine/services/event_dispatch.py", "dispatch_event"),
    }
    flagged = set()
    for base in ("engine/boss_engine", "apps/api/app", "apps/worker"):
        for path in (ROOT / base).rglob("*.py"):
            src = path.read_text()
            if "create_job(" not in src and "enqueue_job(" not in src:
                continue
            tree = ast.parse(src)
            consts = {t.id for n in tree.body if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
                      for t in n.targets if isinstance(t, ast.Name)}
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                checks = any(isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None))
                             in ("require_job_kind", "_require_job_kind_permission") for n in ast.walk(fn))
                for n in ast.walk(fn):
                    if not isinstance(n, ast.Call):
                        continue
                    name = getattr(n.func, "attr", None) or getattr(n.func, "id", None)
                    if name not in ("create_job", "enqueue_job"):
                        continue
                    kind = next((k.value for k in n.keywords if k.arg == "kind"), None)
                    if kind is None and len(n.args) >= 3:
                        kind = n.args[2]
                    literal = (isinstance(kind, ast.Constant) and isinstance(kind.value, str)) or \
                        (isinstance(kind, ast.Name) and (kind.id in consts or kind.id.isupper()))
                    if not literal and not checks:
                        flagged.add((str(path.relative_to(ROOT)), fn.name))
    assert flagged <= safe, flagged - safe
    # The dispatch exemption holds only while dispatch re-checks every job sink before queuing.
    tree = ast.parse((ROOT / "engine/boss_engine/services/event_dispatch.py").read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "dispatch_event")
    assert any(isinstance(n, ast.Call) and getattr(n.func, "id", None) == "job_sink_held"
               for n in ast.walk(fn)), "dispatch_event must re-check job sinks (job_sink_held)"


def _legacy_job_sink(world, kind="infra.droplet.delete", event="meeting.completed"):
    """A job sink as one created before rc6 sits in the table: no kind_authorized_by stamp."""
    sid = "sub_legacy_" + uuid.uuid4().hex[:10]
    with world.engine.begin() as db:
        db.execute(text("INSERT INTO event_subscriptions (id, project_id, event_type, sink_kind, sink_ref) "
                        "VALUES (:i, :p, :e, 'job', :k)"), {"i": sid, "p": world.project_a, "e": event, "k": kind})
    from boss_engine.services.event_dispatch import invalidate_subscription_cache

    invalidate_subscription_cache()
    return sid


def _dispatch(world, ctx, event="meeting.completed"):
    from boss_engine.services.event_dispatch import dispatch_event

    with world.acting_as(ctx), world.engine.begin() as db:
        return dispatch_event(db, event_id="evt_" + uuid.uuid4().hex[:12], project_id=world.project_a,
                              event_type=event, actor="test", entity_type="meeting",
                              entity_id="m1", data={})


def _queued(world, kind):
    return _one(world, "SELECT count(*) FROM jobs WHERE project_id = :p AND kind = :k",
                p=world.project_a, k=kind)[0]


def test_a_job_sink_created_before_the_check_is_held_at_dispatch(world, cast):
    """rc6: a job sink created before C-2 was never checked; it is re-checked on its next use."""
    sid = _legacy_job_sink(world)
    out = _dispatch(world, world.ctx(cast["owner"]))
    held = [o for o in out if o.get("subscription_id") == sid]
    assert held and held[0]["held"] is True and "Re-save" in held[0]["reason"]
    assert _queued(world, "infra.droplet.delete") == 0


def test_a_held_job_sink_runs_again_once_someone_with_the_kinds_permission_re_saves_it(world, cast):
    sid = _legacy_job_sink(world)
    # Someone WITHOUT the kind's permission cannot revive it…
    rid = world.custom_role("integ2", ("integration:manage", "project:read"))
    erin = world.person("Erin")
    world.bind(erin, rid, project=world.project_a)
    with world.acting_as(world.ctx(erin)):
        r = _client().patch(f"/v1/event-subscriptions/{sid}", headers=H,
                            json={"project_id": world.project_a, "enabled": True, "actor": erin.id})
    assert r.status_code == 403, r.text
    assert _one(world, "SELECT kind_authorized_by FROM event_subscriptions WHERE id = :i", i=sid)[0] is None
    # …the Owner (who holds it) can, and the sink then queues on the next event.
    owner = world.ctx(cast["owner"])          # a fresh MFA session: step-up satisfied
    with world.acting_as(owner):
        r = _client().patch(f"/v1/event-subscriptions/{sid}", headers=H,
                            json={"project_id": world.project_a, "enabled": True,
                                  "actor": cast["owner"].id})
    assert r.status_code == 200, r.text
    assert _one(world, "SELECT kind_authorized_by FROM event_subscriptions WHERE id = :i", i=sid)[0]
    from boss_engine.services.event_dispatch import invalidate_subscription_cache

    invalidate_subscription_cache()
    out = _dispatch(world, owner)
    assert not any(o.get("held") for o in out if isinstance(o, dict))
    assert _queued(world, "infra.droplet.delete") == 1


def test_boss_own_default_job_sinks_are_not_held(world, cast):
    from boss_engine.services.event_subscription_service import job_sink_held

    sid = _legacy_job_sink(world, kind="github.triage", event="github.issue_opened")
    with world.acting_as(world.ctx(cast["owner"])), world.engine.begin() as db:
        sub = {"id": sid, "sink_kind": "job", "sink_ref": "github.triage", "event_type": "github.issue_opened"}
        assert job_sink_held(db, sub) is False
        # an unknown kind is always held, stamped or not
        assert job_sink_held(db, {**sub, "sink_ref": "no.such.kind"}) is True


# ---- C-3 -----------------------------------------------------------------------------------------

def test_a_stale_first_approval_is_refused_and_never_recorded(world, cast):
    did = _decision(world, preview_hash="h1")
    a = _post(world, world.ctx(cast["alice"]), f"/v1/decisions/{did}/approve", {"note": "ok", "content_hash": "h_STALE"})
    assert a.status_code == 409, a.text
    assert _approvals(world, did) == {}


def test_two_people_always_approve_the_same_content(world, cast):
    did = _decision(world, preview_hash="h1")
    a = _post(world, world.ctx(cast["alice"]), f"/v1/decisions/{did}/approve", {"note": "ok", "content_hash": "h1"})
    assert a.status_code == 202 and _approvals(world, did) == {cast["alice"].id: "h1"}
    _set_preview(world, did, "h2")          # an upstream step re-ran
    b = _post(world, world.ctx(cast["bob"]), f"/v1/decisions/{did}/approve", {"note": "ok", "content_hash": "h2"})
    assert b.status_code == 202, b.text     # alice's approval of h1 no longer counts
    assert _approvals(world, did) == {cast["bob"].id: "h2"}
    assert _one(world, "SELECT status FROM decisions WHERE id = :d", d=did)[0] == "pending"
    c = _post(world, world.ctx(cast["alice"]), f"/v1/decisions/{did}/approve", {"note": "ok", "content_hash": "h2"})
    assert c.status_code == 200, c.text
    assert tuple(_one(world, "SELECT status, recommendation->>'approved_content_hash' FROM decisions "
                             "WHERE id = :d", d=did)) == ("approved", "h2")


def test_a_first_approver_who_lost_the_seat_no_longer_counts(world, cast):
    did = _decision(world)
    assert _post(world, world.ctx(cast["alice"]), f"/v1/decisions/{did}/approve", {"note": "ok"}).status_code == 202
    with world.engine.begin() as db:
        db.execute(text("DELETE FROM identity_approval_seats WHERE principal_id = :p"), {"p": cast["alice"].id})
    b = _post(world, world.ctx(cast["bob"]), f"/v1/decisions/{did}/approve", {"note": "ok"})
    assert b.status_code == 202, b.text
    assert _one(world, "SELECT status FROM decisions WHERE id = :d", d=did)[0] == "pending"
    assert set(_approvals(world, did)) == {cast["bob"].id}
