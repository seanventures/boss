"""/v1/security/reviews* on the real app under enforce (APP_TEMPLATES_AND_SECURITY_LOOP.md §4.3, §4.6).

A security_review run, its checklist (the recommendations) and the person's choice: every role
reads a review of its project; only a role holding `security_finding:triage` records the choice;
another project's person sees nothing; the choice is checked against the review (unknown
recommendation, accepting a finding chosen for fixing) and is an audited event.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.security_scan import review
from boss_engine.services import workflow_catalogue, workflows

FPS = ["1" * 64, "2" * 64, "3" * 64]


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


def _person(world, role, project=None):
    p = world.person(f"rev-{role}-{uuid.uuid4().hex[:6]}")
    world.bind(p, role, project=project or world.project_a)
    return p


def _client():
    return TestClient(main.app, raise_server_exceptions=False)


def _review(engine, project: str) -> tuple[str, dict[str, str]]:
    """A security_review run of `project` with a scan of three findings and two recommendations."""
    app_key = f"app-{uuid.uuid4().hex[:6]}"
    with engine.begin() as c:
        workflow_catalogue.seed(c, project)
        version = c.execute(text(
            "SELECT v.id FROM workflow_versions v JOIN workflow_definitions d ON d.id = v.workflow_definition_id "
            "WHERE d.project_id = :p AND d.workflow_key = 'security_review' ORDER BY v.created_at DESC LIMIT 1"),
            {"p": project}).scalar()
        run = workflows.start_run(c, project_id=project, actor="test", input_data={"app_key": app_key},
                                  dedupe_key=f"dk_{uuid.uuid4().hex[:10]}", workflow_version_id=version)
        scan_id = f"scan_{uuid.uuid4().hex[:10]}"
        c.execute(text("INSERT INTO security_scans (id, project_id, app_key, status) VALUES (:i, :p, :a, 'done')"),
                  {"i": scan_id, "p": project, "a": app_key})
        finding_ids = {}
        for n, fp in enumerate(FPS):
            fid = f"sf_{uuid.uuid4().hex[:10]}"
            finding_ids[fp] = fid
            c.execute(text(
                "INSERT INTO security_findings (id, scan_id, fingerprint, tool, rule_id, title, severity, location) "
                "VALUES (:i, :s, :fp, 'semgrep', :r, :t, 'high', CAST(:l AS JSONB))"),
                {"i": fid, "s": scan_id, "fp": fp, "r": f"rule{n}", "t": f"Finding {n}",
                 "l": json.dumps({"file": "app.js", "line": n, "function": f"f{n}"})})
        recs = review.store_recommendations(c, project_id=project, run_id=run["id"], scan_id=scan_id,
                                            meeting_id=None, recs=[
            {"title": "Fix 0 and 1", "fix": "x", "fingerprints": FPS[:2], "priority": "P0", "effort": "S",
             "source": "panel"},
            {"title": "Fix 2", "fix": "y", "fingerprints": FPS[2:], "priority": "P3", "effort": "S",
             "source": "derived"}])
        # The review's steps as the console reads them: the scan names its scan.
        c.execute(text("UPDATE workflow_step_runs SET output = CAST(:o AS JSONB) "
                       "WHERE workflow_run_id = :r AND step_id = 'scan'"),
                  {"o": json.dumps({"scan_id": scan_id}), "r": run["id"]})
    return run["id"], {"p0": recs[0]["id"], "p3": recs[1]["id"], "finding2": finding_ids[FPS[2]],
                       "finding0": finding_ids[FPS[0]], "app_key": app_key}


@pytest.mark.parametrize("role,can_choose", [
    ("owner", True), ("admin", True), ("operator", True), ("viewer", False), ("auditor", False),
])
def test_every_role_reads_a_review_and_only_a_triager_chooses(world, engine, role, can_choose):
    run_id, ids = _review(engine, world.project_a)
    who = _person(world, role)
    client = _client()
    with world.acting_as(world.ctx(who)):
        listed = client.get("/v1/security/reviews", params={"project_id": world.project_a,
                                                            "app_key": ids["app_key"]})
        assert listed.status_code == 200, listed.text
        assert [r["run_id"] for r in listed.json()["reviews"]] == [run_id]
        one = client.get(f"/v1/security/reviews/{run_id}")
        assert one.status_code == 200, one.text
        body = one.json()
        assert [r["title"] for r in body["recommendations"]] == ["Fix 0 and 1", "Fix 2"]
        assert [r["selected"] for r in body["recommendations"]] == [True, False]   # P0 pre-ticked
        assert body["findings"][FPS[0]]["title"] == "Finding 0"
        chose = client.post(f"/v1/security/reviews/{run_id}/selection",
                            json={"selected": [ids["p3"]],
                                  "accept": [{"finding_id": ids["finding0"], "reason": "behind admin auth"}]})
        assert (chose.status_code == 200) is can_choose, chose.text
        if not can_choose:
            assert chose.status_code in (403, 404)
    with engine.begin() as c:
        recs = review.list_recommendations(c, run_id)
        status = c.execute(text("SELECT status, triaged_by FROM security_findings WHERE id = :i"),
                           {"i": ids["finding0"]}).mappings().first()
        events = c.execute(text("SELECT count(*) FROM events WHERE event_type = 'security_review.selection_recorded' "
                                "AND entity_id = :r"), {"r": run_id}).scalar()
    if can_choose:
        assert [r["selected"] for r in recs] == [False, True]
        assert recs[1]["decided_by"] == who.id
        assert status["status"] == "accepted" and status["triaged_by"] == who.id
        assert events == 1
    else:
        assert [r["selected"] for r in recs] == [True, False] and status["status"] == "open"


def test_another_project_sees_no_review(world, engine):
    run_id, _ids = _review(engine, world.project_a)
    outsider = _person(world, "owner", project=world.project_b)
    client = _client()
    with world.acting_as(world.ctx(outsider)):
        assert client.get(f"/v1/security/reviews/{run_id}").status_code == 404
        assert client.post(f"/v1/security/reviews/{run_id}/selection", json={"selected": []}).status_code == 404
        assert client.get("/v1/security/reviews", params={"project_id": world.project_a}).status_code == 403


def test_the_choice_is_checked_against_the_review(world, engine):
    run_id, ids = _review(engine, world.project_a)
    op = _person(world, "operator")
    client = _client()
    with world.acting_as(world.ctx(op)):
        r = client.post(f"/v1/security/reviews/{run_id}/selection", json={"selected": ["srec_nope"]})
        assert (r.status_code, r.json()["error"]) == (422, "unknown_recommendation")
        r = client.post(f"/v1/security/reviews/{run_id}/selection",
                        json={"selected": [ids["p0"]],
                              "accept": [{"finding_id": ids["finding0"], "reason": "fine really"}]})
        assert (r.status_code, r.json()["error"]) == (422, "finding_is_targeted")
        r = client.post(f"/v1/security/reviews/{run_id}/selection",
                        json={"selected": [], "accept": [{"finding_id": ids["finding2"], "reason": "x"}]})
        assert r.status_code == 422                                   # a reason of 3+ characters
        r = client.post(f"/v1/security/reviews/{run_id}/selection",
                        json={"selected": [ids["p0"]], "smuggled": True})
        assert r.status_code == 422
    with engine.begin() as c:
        assert [x["selected"] for x in review.list_recommendations(c, run_id)] == [True, False]


# ---- "Fix selected": the choice and the gate's approval are ONE action -------------------------

def _waiting(engine, run_id: str, project: str) -> str:
    """Put the review at its decide step, waiting on a real decision."""
    from boss_engine.services.core import create_blocking_decision

    with engine.begin() as c:
        decision_id = create_blocking_decision(c, project, None, "Choose the security fixes",
                                               {"action": "security_review_choose", "workflow_run_id": run_id},
                                               {}, "security_review_choice", risk_tier=1)
        c.execute(text("UPDATE workflow_step_runs SET status = 'waiting', decision_id = :d "
                       "WHERE workflow_run_id = :r AND step_id = 'decide'"), {"d": decision_id, "r": run_id})
    return decision_id


def _recorded(engine, run_id: str) -> int:
    with engine.begin() as c:
        return c.execute(text("SELECT count(*) FROM events WHERE event_type = 'security_review.selection_recorded' "
                              "AND entity_id = :r"), {"r": run_id}).scalar()


def test_fix_selected_records_and_approves_in_one_commit(world, engine, monkeypatch):
    # The decision's own actor checks are the approve route's (legacy org membership): the owner
    # is a member there too, as a claimed install's owner is.
    monkeypatch.setattr(main, "require_decision_actor_binding", lambda *_a, **_k: None)
    monkeypatch.setattr(main, "_decision_route_actor", lambda _c, _d, actor: actor)
    run_id, ids = _review(engine, world.project_a)
    decision_id = _waiting(engine, run_id, world.project_a)
    who = _person(world, "owner")
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO org_members (id, org_id, member_id, display_name, member_type, role_keys, permissions, "
            "status, created_by) VALUES (:i, :o, :m, 'Review owner', 'human', '[\"owner\"]'::jsonb, "
            "'[\"decision_resolve\", \"decision:approve\"]'::jsonb, 'active', 'test')"),
            {"i": f"om_{who.id}", "o": world.org, "m": who.id})
    with world.acting_as(world.ctx(who)):
        r = _client().post(f"/v1/security/reviews/{run_id}/selection",
                           json={"selected": [ids["p0"]], "approve": True, "note": "fix the P0"})
    assert r.status_code == 200, r.text
    assert r.json()["approved"] is True and r.json()["decision_id"] == decision_id
    assert _recorded(engine, run_id) == 1
    with engine.begin() as c:
        assert c.execute(text("SELECT status FROM decisions WHERE id = :d"), {"d": decision_id}).scalar() == "approved"


def test_a_refused_approval_records_nothing(world, engine, monkeypatch):
    """The approval refused AFTER the choice was written in the same transaction: both roll back."""
    from fastapi import HTTPException

    run_id, ids = _review(engine, world.project_a)
    decision_id = _waiting(engine, run_id, world.project_a)

    def refuse(*_a, **_k):
        raise HTTPException(status_code=409, detail="a second person must approve this")

    monkeypatch.setattr(main, "approve_decision_in", refuse)
    who = _person(world, "owner")
    with world.acting_as(world.ctx(who)):
        r = _client().post(f"/v1/security/reviews/{run_id}/selection",
                           json={"selected": [ids["p3"]], "approve": True,
                                 "accept": [{"finding_id": ids["finding0"], "reason": "behind admin auth"}]})
    assert r.status_code == 409, r.text
    assert _recorded(engine, run_id) == 0
    with engine.begin() as c:
        recs = review.list_recommendations(c, run_id)
        finding = c.execute(text("SELECT status FROM security_findings WHERE id = :i"), {"i": ids["finding0"]}).scalar()
        decision = c.execute(text("SELECT status FROM decisions WHERE id = :d"), {"d": decision_id}).scalar()
    assert [x["selected"] for x in recs] == [True, False] and finding == "open" and decision != "approved"


def test_the_right_to_approve_is_checked_before_anything_is_written(world, engine, monkeypatch):
    from fastapi import HTTPException

    from apps.api.app.routers import identity_access

    run_id, ids = _review(engine, world.project_a)
    _waiting(engine, run_id, world.project_a)
    seen = []

    def deny(request, permission, project_id, label=""):
        seen.append(permission)
        if permission == "decision:approve":
            raise HTTPException(status_code=403, detail={"error": "forbidden", "needs": permission})

    monkeypatch.setattr(identity_access, "require_permission", deny)
    monkeypatch.setattr(review, "record_selection", lambda *a, **k: pytest.fail("the choice was written first"))
    who = _person(world, "operator")
    with world.acting_as(world.ctx(who)):
        r = _client().post(f"/v1/security/reviews/{run_id}/selection", json={"selected": [ids["p0"]], "approve": True})
    assert r.status_code == 403 and seen == ["decision:approve"]
    assert _recorded(engine, run_id) == 0


def test_approve_when_the_review_is_not_waiting_is_refused_and_records_nothing(world, engine):
    run_id, ids = _review(engine, world.project_a)
    who = _person(world, "owner")
    with world.acting_as(world.ctx(who)):
        r = _client().post(f"/v1/security/reviews/{run_id}/selection", json={"selected": [ids["p0"]], "approve": True})
    assert r.status_code == 409 and r.json()["error"] == "not_awaiting_choice"
    assert _recorded(engine, run_id) == 0
