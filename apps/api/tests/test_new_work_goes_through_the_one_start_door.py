"""New work on the Project page goes through the one start door (BOSSAPP-913, contract §3).

POST /v1/projects/{id}/work starts exactly what the project page's "What should we add?" box
starts (start_project.how_to_start → start_delivery_workflow), and a consent or readiness
refusal comes back unchanged, so the console's existing sheets answer it. Real Postgres; no
model, no GitHub.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.project_experience_seed import REPO, seed


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("nw-owner")
    w.bind(w.owner, "owner")
    w.viewer = w.person("nw-viewer")
    w.bind(w.viewer, "viewer")
    return w


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def project(world, client, engine) -> str:
    slug = f"nw-{uuid.uuid4().hex[:8]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "HelpDesk AI Demo", "slug": slug})
    assert made.status_code == 201, made.text
    with engine.begin() as db:
        seed(db, slug)
        # The org's registry knows the repository and that BOSS may write it (as Import does).
        db.execute(text(
            "INSERT INTO github_repos (id, org_id, full_name, language, default_branch, allowed, "
            "checks, checked_at) VALUES (:i, :o, :f, 'TypeScript', 'main', true, CAST(:c AS JSONB), "
            "now()) ON CONFLICT DO NOTHING"),
            {"i": f"ghr_{uuid.uuid4().hex[:8]}", "o": world.org, "f": REPO,
             "c": '{"read": {"ok": true}, "write": {"ok": true}, "pull_requests": {"ok": true}}'})
    return slug


def _consent(client, refused) -> bool:
    body = refused.json() if refused.status_code == 409 else {}
    if body.get("error") != "consent_required":
        return False
    grant = {"agents": [a["id"] for a in body["agents"]], "permissions": body["permissions"],
             "projects": body["projects"], "kind": "standing", "days": 30, "purpose": body["task"]}
    if body.get("github"):
        grant["github"] = {"repos": body["github"]["repos"], **body["github"]["permissions"]}
    given = client.post("/v1/identity/me/delegations", json=grant)
    assert given.status_code in (200, 201), given.text
    return True


def _missing(refused) -> list:
    body = refused.json() if refused.status_code == 409 else {}
    detail = body.get("detail") if isinstance(body.get("detail"), dict) else body
    ready = detail.get("readiness") if isinstance(detail.get("readiness"), dict) else detail
    return [m["key"] for m in (ready.get("missing") or [])] if isinstance(ready, dict) else []


def test_new_work_starts_the_projects_own_workflow_and_returns_its_row(world, client, engine,
                                                                       project, monkeypatch):
    from boss_engine.identity import grants

    # The confused-deputy guard asks GitHub about the person's own access on multi-person
    # installs; GitHub is never called here (its answer is the start door's business).
    monkeypatch.setattr(grants, "repo_guard_active", lambda *a, **k: False)
    body = {"kind": "bug", "text": "Fix the login 500 for agents.\nPOST /login answers 500."}
    with world.acting_as(world.ctx(world.owner)):
        r = client.post(f"/v1/projects/{project}/work", json=body)
        refusals = []
        for _ in range(3):
            if r.status_code == 200:
                break
            refusals.append(r.json())
            if _consent(client, r):
                r = client.post(f"/v1/projects/{project}/work", json=body)
                continue
            missing = _missing(r)
            assert missing, r.text
            body = {**body, "start_anyway": missing}
            r = client.post(f"/v1/projects/{project}/work", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["work"]["kind"] == "bug" and out["work"]["title"] == "Fix the login 500 for agents"
    assert out["work"]["state"] in ("planned", "running")
    assert out["href"] == f"?project_id={project}&area=project&tab=work&drawer=run%3A{out['run_id']}"
    with engine.connect() as db:
        row = db.execute(text(
            "SELECT d.workflow_key, r.input FROM workflow_runs r JOIN workflow_definitions d ON "
            "d.id = r.workflow_definition_id WHERE r.id = :r"), {"r": out["run_id"]}).one()
    assert row[0] == "minor_feature"
    assert row[1]["repository"] == REPO and row[1]["work_kind"] == "bug"
    assert row[1]["directive"].startswith("Fix the login 500")
    # Whatever refused on the way came back in the start door's own shape.
    for refused in refusals:
        assert refused.get("error") == "consent_required" or "readiness" in str(refused)


def test_a_consent_refusal_is_the_start_doors_own_answer(world, client, engine, project):
    """The console's consent sheet reads the same body from either route."""
    body = {"kind": "feature", "text": "Add CSV export"}
    with world.acting_as(world.ctx(world.owner)):
        mine = client.post(f"/v1/projects/{project}/work", json=body)
        door = client.post("/v1/delivery-workflows/start", json={
            "project_id": project, "workflow_key": "minor_feature",
            "input": {"directive": "Add CSV export", "title": "Add CSV export",
                      "repository": REPO}})
    assert mine.status_code == door.status_code
    if mine.status_code == 409 and mine.json().get("error") == "consent_required":
        assert set(mine.json()) == set(door.json())
        assert mine.json()["agents"] == door.json()["agents"]


def test_new_work_needs_words_a_known_kind_and_the_right_to_start_work(world, client, project):
    with world.acting_as(world.ctx(world.owner)):
        empty = client.post(f"/v1/projects/{project}/work", json={"kind": "bug", "text": "  "})
        odd = client.post(f"/v1/projects/{project}/work", json={"kind": "epic", "text": "x"})
    assert empty.status_code == 422 and empty.json()["detail"]["error"] == "text_required"
    assert odd.status_code == 422 and odd.json()["detail"]["error"] == "bad_kind"
    with world.acting_as(world.ctx(world.viewer)):
        refused = client.post(f"/v1/projects/{project}/work", json={"kind": "bug", "text": "x"})
    assert refused.status_code == 403
