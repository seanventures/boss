"""Multi-repo change sets on the real API under enforce: who may read a change, stop it, merge it,
deploy it and roll it back; a change of another project is invisible; the merge refuses anything
not approved. GitHub is never called (the sets here are never approved)."""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.people = {}
    for role in ("owner", "admin", "operator", "viewer", "auditor"):
        p = w.person(f"cs-{role}")
        w.bind(p, role, project=None if role in ("owner", "admin", "auditor") else w.project_a)
        w.people[role] = p
    outsider = w.person("cs-outsider")
    w.bind(outsider, "operator", project=w.project_b)
    w.people["outsider"] = outsider
    return w


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def _as(world, who):
    return world.acting_as(world.ctx(world.people[who], mfa=True))


@pytest.fixture
def change(engine, world):
    """A three-repository change of project A, waiting at build (no GitHub needed to read it)."""
    cs = f"chs_{uuid.uuid4().hex[:12]}"
    with engine.begin() as db:
        db.execute(text("INSERT INTO repo_change_sets (id, project_id, app_key, app_name, title, mode, state, plan) "
                        "VALUES (:i, :p, 'storefront', 'Storefront', 'Storefront: Add saved carts', 'multi', 'building', "
                        "CAST(:plan AS JSONB))"),
                   {"i": cs, "p": world.project_a, "plan": json.dumps({"components": [
                       {"name": "Contracts", "change": "Add SavedCart"}]})})
        for wave, full in enumerate(("acme/contracts", "acme/api", "acme/web"), start=1):
            db.execute(text("INSERT INTO change_set_prs (id, change_set_id, full_name, components, wave, lane_state) "
                            "VALUES (:i, :c, :f, :comps, :w, :s)"),
                       {"i": f"csp_{uuid.uuid4().hex[:10]}", "c": cs, "f": full, "comps": [full.split('/')[1]],
                        "w": wave, "s": "building" if wave == 1 else "waiting"})
        db.execute(text("INSERT INTO repo_run_grants (id, project_id, app_key, change_set_id, repos, consented_by) "
                        "VALUES (:i, :p, 'storefront', :c, CAST(:r AS JSONB), 'usr_x')"),
                   {"i": f"rgr_{uuid.uuid4().hex[:8]}", "p": world.project_a, "c": cs,
                    "r": json.dumps([{"full_name": f, "access": "write"} for f in ("acme/contracts", "acme/api",
                                                                                   "acme/web")])})
    return cs


@pytest.mark.parametrize("role, status", [("owner", 200), ("admin", 200), ("operator", 200), ("viewer", 200),
                                          ("auditor", 200), ("outsider", 403)])
def test_who_may_read_a_change(client, world, change, role, status):
    with _as(world, role):
        r = client.get(f"/v1/change-sets/{change}", params={"project_id": world.project_a})
    assert r.status_code == status, r.text
    if status == 200:
        body = r.json()
        assert [lane["full_name"] for lane in body["lanes"]] == ["acme/contracts", "acme/api", "acme/web"]
        assert body["stage"]["steps"][1]["label"] == "Build · 0 of 3"
        assert body["grant"]["repos"][0]["full_name"] == "acme/contracts"


def test_a_change_of_another_project_is_not_found(client, world, change):
    with _as(world, "outsider"):
        r = client.get(f"/v1/change-sets/{change}", params={"project_id": world.project_b})
    assert r.status_code == 404 and r.json()["error"] == "unknown_change_set"


@pytest.mark.parametrize("role, status", [("viewer", 403), ("auditor", 403), ("operator", 200)])
def test_who_may_stop_a_change(client, world, change, engine, role, status):
    with _as(world, role):
        r = client.post(f"/v1/change-sets/{change}/stop", json={"project_id": world.project_a})
    assert r.status_code == status, r.text
    with engine.begin() as db:
        state = db.execute(text("SELECT state FROM repo_change_sets WHERE id = :i"), {"i": change}).scalar()
    assert state == ("cancelled" if status == 200 else "building")


def test_nothing_merges_without_the_one_approval(client, world, change):
    with _as(world, "viewer"):
        assert client.post(f"/v1/change-sets/{change}/merge", json={"project_id": world.project_a}).status_code == 403
    with _as(world, "operator"):
        r = client.post(f"/v1/change-sets/{change}/merge", json={"project_id": world.project_a})
    assert r.status_code == 409 and r.json()["error"] == "not_approved"


def test_deploy_plan_is_readable_and_deploy_and_rollback_are_gated(client, world, change):
    with _as(world, "viewer"):
        plan = client.get(f"/v1/change-sets/{change}/deploy-plan", params={"project_id": world.project_a})
        assert plan.status_code == 200 and len(plan.json()["waves"]) == 3
        assert client.post(f"/v1/change-sets/{change}/deploy", json={"project_id": world.project_a}).status_code == 403
        assert client.post(f"/v1/change-sets/{change}/rollback",
                           json={"project_id": world.project_a}).status_code == 403
    with _as(world, "owner"):
        r = client.post(f"/v1/change-sets/{change}/deploy", json={"project_id": world.project_a})
        assert r.status_code == 409 and r.json()["error"] == "not_merged"
        r = client.post(f"/v1/change-sets/{change}/rollback", json={"project_id": world.project_a})
        assert r.status_code == 409 and r.json()["error"] == "nothing_to_roll_back"


def test_the_cross_check_waits_for_every_pr(client, world, change):
    with _as(world, "operator"):
        r = client.post(f"/v1/change-sets/{change}/cross-check", json={"project_id": world.project_a})
    assert r.status_code == 409 and r.json()["error"] == "not_ready"


def test_a_build_that_is_not_part_of_a_change_says_so(client, world):
    with _as(world, "viewer"):
        r = client.get("/v1/runs/crun_nothing/change-set", params={"project_id": world.project_a})
    assert r.status_code == 404 and r.json()["error"] == "no_change_set"


def test_the_list_of_changes(client, world, change):
    with _as(world, "viewer"):
        r = client.get(f"/v1/projects/{world.project_a}/change-sets", params={"state": "building,in_review"})
    assert r.status_code == 200 and [i["id"] for i in r.json()["items"]] == [change]
