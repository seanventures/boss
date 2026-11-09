"""CREATE AND START NEVER STRANDS A PROJECT; THE PRD IS YOURS ONLY WHEN IT EXISTS (BOSSAPP-946).

Finding 4. Create commits the project before the start, and the start may need the person's
consent to the agents (a grant names the project it is for, so it cannot be asked before the
project exists). An unanswered or cancelled consent sheet, or a reload, left a half-made project
holding a staging port that nothing could resume. Chosen: RESUME, not roll back — rolling back
would throw away the local repository and the staging target the person picked and make them
type it all again; the kept project resumes with ONE Start on its page, which sends the kept
start again (phase 2 only; never a second project, never a second repository).

Finding 5. The PRD card said "waiting for you" with Approve while the panel was still meeting,
and went back to "drafting" after the approval: the meeting step is both the panel and the PRD's
hold. The card now reads the PRD's own state: drafting → waiting (PRD exists, meeting done,
approval pending) → approved.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.app.routers import add_project as route
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.services.project_experience import add_project

WORDS = "A help desk: customers open tickets, agents answer them."
CONSENT = {"error": "consent_required", "task": "Plan the help desk", "task_run_id": "pending:x",
           "agents": [{"id": "agt_pm", "name": "PM"}], "permissions": ["workflow_run:start"],
           "projects": [], "durations": [{"kind": "standing", "label": "30 days", "days": 30}]}


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("kp-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def _owner(world):
    return world.acting_as(world.ctx(world.owner, mfa=True))


def test_an_unanswered_consent_leaves_a_project_one_start_away(client, world, engine, monkeypatch):
    sent: list[dict] = []

    def needs_consent(_request, body, pid, _org, run_input, _actor):
        sent.append({"pid": pid, "workflow_key": body.workflow_key, "run_input": run_input})
        raise HTTPException(status_code=409, detail={**CONSENT, "projects": [pid]})

    monkeypatch.setattr(route, "_start", needs_consent)
    name = f"Desk {uuid.uuid4().hex[:6]}"
    body = {"context_project_id": world.project_a, "kind": "idea", "name": name, "words": WORDS,
            "inputs": {"notify_channel": "email:lead@example.com"},
            "connector_secrets": {"SOME_KEY": "never-kept"}}
    with _owner(world):
        first = client.post("/v1/add-project", json=body)
    if first.json().get("error") == "not_ready":   # what this install lacks: started anyway
        missing = [m["key"] for m in (first.json().get("readiness") or {}).get("missing") or []]
        body["start_anyway"] = missing
        with _owner(world):
            first = client.post("/v1/add-project", json=body)
    assert first.status_code == 409, first.text
    said = first.json()
    assert said["error"] == "consent_required" and said["created"] is True
    pid = said["project_id"]

    # The person cancels the sheet, or reloads: the project page says what it is waiting for.
    with _owner(world):
        view = client.get(f"/v1/projects/{pid}/add-project/started").json()
    assert view["run"] is None
    assert view["not_started"]["resumable"] is True
    assert view["not_started"]["reason"] == "consent_required"
    assert "consent" in view["not_started"]["words"]
    with engine.connect() as db:
        kept = add_project.seed_of(db, pid)["picks"]["start_request"]
    assert kept["words"] == WORDS and kept["inputs"] == {"notify_channel": "email:lead@example.com"}
    assert "connector_secrets" not in kept and "project_id" not in kept

    # Start, still without consent: the same 409 for the page's consent sheet, nothing new made.
    with _owner(world):
        again = client.post(f"/v1/projects/{pid}/add-project/start", json={})
    assert again.status_code == 409 and again.json()["error"] == "consent_required"

    # Consent given: one Start sends the kept start — on THIS project, with the same words.
    monkeypatch.setattr(route, "_start", lambda _r, b, p, _o, ri, _a: (
        sent.append({"pid": p, "workflow_key": b.workflow_key, "run_input": ri}) or {"run_id": "wfr_k"}))
    with _owner(world):
        done = client.post(f"/v1/projects/{pid}/add-project/start", json={})
    assert done.status_code == 200, done.text
    assert done.json()["project_id"] == pid and done.json()["started"]["run_id"] == "wfr_k"
    assert {s["pid"] for s in sent} == {pid}
    assert sent[-1]["run_input"]["directive"].startswith(WORDS[:20])
    assert sent[-1]["run_input"]["notify_channel"] == "email:lead@example.com"
    with engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM projects WHERE name = :n"),
                          {"n": name}).scalar() == 1
        picks = add_project.seed_of(db, pid)["picks"]
    assert "start_refused" not in picks


def test_a_project_made_another_way_has_nothing_to_start(client, world):
    with _owner(world):
        pid = client.post("/v1/projects", json={"name": f"Plain {uuid.uuid4().hex[:6]}"}).json()["id"]
        r = client.post(f"/v1/projects/{pid}/add-project/start", json={})
    assert r.status_code == 409 and r.json()["error"] == "nothing_to_start"


# ── finding 5: the PRD is the person's only once it exists and the meeting is done ───────────

def _run_with_meeting(engine, pid: str) -> tuple[str, str]:
    with engine.begin() as db:
        d = db.execute(text("SELECT id, active_version_id FROM workflow_definitions WHERE "
                            "project_id = :p AND workflow_key = 'standard_product_delivery'"),
                       {"p": pid}).mappings().one()
        run_id, mid = f"wfr_{uuid.uuid4().hex[:12]}", f"mrun_{uuid.uuid4().hex[:12]}"
        db.execute(text("INSERT INTO workflow_runs (id, project_id, workflow_definition_id, "
                        "workflow_version_id, actor, status) VALUES (:i, :p, :d, :v, 'test', "
                        "'running')"), {"i": run_id, "p": pid, "d": d["id"],
                                        "v": d["active_version_id"]})
        db.execute(text("INSERT INTO meeting_runs (id, project_id, directive, status) VALUES "
                        "(:i, :p, 'the help desk', 'running')"), {"i": mid, "p": pid})
        for step, status, out in (("meeting", "waiting", {"await": {"kind": "meeting", "id": mid}}),
                                  ("spec_review", "pending", {})):
            db.execute(text("INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, "
                            "step_id, step_kind, status, output) VALUES (:i, :p, :r, :s, "
                            "'capability', :st, CAST(:o AS JSONB))"),
                       {"i": f"wsr_{uuid.uuid4().hex[:12]}", "p": pid, "r": run_id, "s": step,
                        "st": status, "o": json.dumps(out)})
        add_project.save_seed(db, pid, kind="idea", words=WORDS, brief={}, picks={},
                              actor="test")
    return run_id, mid


def _node(view, key):
    return next((n for n in view["nodes"] if n["key"] == key), None)


def test_the_prd_waits_for_you_only_once_the_panel_is_done(client, world, engine, monkeypatch):
    from boss_engine.services import workflow_capabilities as caps

    monkeypatch.setattr(caps, "prd_auto_approved", lambda settings=None: False)
    with _owner(world):
        pid = client.post("/v1/projects", json={"name": f"Prd {uuid.uuid4().hex[:6]}"}).json()["id"]
    run_id, mid = _run_with_meeting(engine, pid)

    # The panel is meeting: drafting, and the approval is not the person's yet.
    with engine.begin() as db:
        view = add_project.started(db, pid)
    assert view["prd"]["state"] == "drafting"
    assert _node(view, "approve")["state"] != "you"
    assert not (view["now"] or {}).get("you")

    # The meeting finished and its PRD waits for approval: now it is the person's.
    did = f"dec_{uuid.uuid4().hex[:12]}"
    with engine.begin() as db:
        db.execute(text("INSERT INTO decisions (id, project_id, question, recommendation, "
                        "risk_tier, default_action, status) VALUES (:i, :p, 'Approve the PRD?', "
                        "'{}'::jsonb, 2, 'hold', 'pending')"), {"i": did, "p": pid})
        db.execute(text("UPDATE meeting_runs SET status = 'completed', decision_id = :d "
                        "WHERE id = :m"), {"d": did, "m": mid})
        view = add_project.started(db, pid)
    assert view["prd"] == {"state": "waiting", "decision_id": did, "meeting_id": mid}
    assert _node(view, "approve")["state"] == "you" and _node(view, "plan")["state"] == "done"

    # Approved: done, never "drafting" again.
    with engine.begin() as db:
        db.execute(text("UPDATE decisions SET status = 'approved' WHERE id = :i"), {"i": did})
        db.execute(text("UPDATE workflow_step_runs SET status = 'succeeded' WHERE "
                        "workflow_run_id = :r"), {"r": run_id})
    with _owner(world):
        view = client.get(f"/v1/projects/{pid}/add-project/started").json()
    assert view["prd"]["state"] == "approved"
    assert _node(view, "approve")["state"] == "done"
