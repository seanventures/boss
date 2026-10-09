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
