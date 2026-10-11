"""Multi-repo phases 1–2 on the real API under enforce: who may read and change the registry, a
project's repositories and an app's components, and that every refusal is a sentence.

GitHub is never called: the registry's prober and lister are stand-ins."""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.multirepo import registry

OK = {"ok": True, "state": "working"}


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.people = {}
    for role in ("owner", "admin", "operator", "viewer", "auditor"):
        p = w.person(f"mr-{role}")
        w.bind(p, role, project=None if role in ("owner", "admin", "auditor") else w.project_a)
        w.people[role] = p
    outsider = w.person("mr-outsider")
    w.bind(outsider, "operator", project=w.project_b)
    w.people["outsider"] = outsider
    return w


@pytest.fixture(autouse=True)
def github(monkeypatch, world, engine):
    """A registry of four repositories (the org's App reaches them) and a project connection."""
    def prober(db, settings, org_id, project_id, full_name):
        return {"state": "working", "checks": {"read": OK, "write": dict(OK), "pull_requests": dict(OK)}}

    monkeypatch.setattr(registry, "default_prober", prober)
    with engine.begin() as db:
        for i, (name, lang, allowed) in enumerate((("acme/contracts", "Protobuf", True), ("acme/api", "Go", True),
                                                   ("acme/web", "TypeScript", True), ("acme/mobile", "Kotlin", False))):
            db.execute(text("INSERT INTO github_repos (id, org_id, full_name, language, allowed, checks, checked_at) "
                            "VALUES (:i, :o, :f, :l, :a, CAST(:c AS JSONB), now())"),
                       {"i": f"ghr_{name.split('/')[1]}", "o": world.org, "f": name, "l": lang, "a": allowed,
                        "c": json.dumps({"read": OK, "write": OK, "pull_requests": OK}) if allowed else "{}"})
        for pid in (world.project_a, world.project_b):
            db.execute(text("INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
                            "credential_ref, status, created_by) VALUES (:c, :p, 'token', 'personal_access_token', "
                            "'secret:none', 'active', 'test') ON CONFLICT DO NOTHING"),
                       {"c": f"ghc_{pid}", "p": pid})


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def _as(world, who, *, mfa=True):
    return world.acting_as(world.ctx(world.people[who], mfa=mfa))


def _bind(engine, world, full_name, pid=None):
    bid = f"ghb_{uuid.uuid4().hex[:10]}"
    pid = pid or world.project_a
    owner, name = full_name.split("/")
    with engine.begin() as db:
        db.execute(text("INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, full_name, "
                        "role, default_branch, checks, checked_at, created_by, registry_repo_id) VALUES (:i, :p, :c, :o, "
                        ":n, :f, 'source', 'main', CAST(:chk AS JSONB), now(), 'test', :r)"),
                   {"i": bid, "p": pid, "c": f"ghc_{pid}", "o": owner, "n": name, "f": full_name,
                    "chk": json.dumps({"read": OK, "write": OK, "pull_requests": OK}), "r": f"ghr_{name}"})
    return bid


@pytest.mark.parametrize("role, status", [("owner", 200), ("admin", 200), ("viewer", 200), ("auditor", 200),
                                          ("operator", 200), ("outsider", 403)])
def test_who_may_read_the_registry(client, world, role, status):
    with _as(world, role):
        r = client.get("/v1/github/repos", params={"project_id": world.project_a})
    assert r.status_code == status, r.text
    if status == 200:
        body = r.json()
        assert body["counts"] == {"all": 4, "allowed": 3, "needs_fix": 0, "not_allowed": 1}
        assert "ghs_" not in json.dumps(body) and "ghp_" not in json.dumps(body)   # never a credential


@pytest.mark.parametrize("role, status", [("owner", 200), ("admin", 200), ("operator", 403), ("viewer", 403),
                                          ("auditor", 403), ("outsider", 403)])
def test_who_may_tick_a_repository(client, world, role, status):
    with _as(world, role):
        r = client.patch("/v1/github/repos/ghr_mobile", json={"project_id": world.project_a, "allowed": True})
    assert r.status_code == status, r.text
    if status == 200:
        assert r.json()["state"] == "working"


def test_ticking_without_a_fresh_second_factor_asks_for_one(client, world):
    with _as(world, "owner", mfa=False):
        r = client.patch("/v1/github/repos/ghr_mobile", json={"project_id": world.project_a, "allowed": True})
    assert r.status_code in (401, 403) and "step_up" in r.text


def test_save_allowed_and_an_unknown_id_is_a_sentence(client, world, engine):
    with _as(world, "admin"):
        r = client.put("/v1/github/repos/allowed", json={"project_id": world.project_a,
                                                          "allowed_ids": ["ghr_api", "ghr_web"]})
        assert r.status_code == 200, r.text
        assert sorted(i["full_name"] for i in r.json()["items"] if i["allowed"]) == ["acme/api", "acme/web"]
        bad = client.put("/v1/github/repos/allowed", json={"project_id": world.project_a, "allowed_ids": ["ghr_x"]})
    assert bad.status_code == 404 and bad.json()["message"]


def test_a_project_lists_and_removes_its_repositories(client, world, engine):
    _bind(engine, world, "acme/api")
    with _as(world, "viewer"):
        listed = client.get(f"/v1/projects/{world.project_a}/repos")
    assert listed.status_code == 200
    body = listed.json()
    assert [i["full_name"] for i in body["items"]] == ["acme/api"]
    assert {a["full_name"] for a in body["addable"]} == {"acme/contracts", "acme/web"}
    with _as(world, "operator"):
        assert client.delete(f"/v1/projects/{world.project_a}/repos/ghr_api").status_code == 403
    with _as(world, "admin"):
        r = client.delete(f"/v1/projects/{world.project_a}/repos/ghr_api")
    assert r.status_code == 200 and r.json()["items"] == []


def test_adding_a_repository_that_is_not_allowed_is_refused(client, world):
    with _as(world, "admin"):
        r = client.put(f"/v1/projects/{world.project_a}/repos/ghr_mobile", json={"role_hint": "frontend"})
    assert r.status_code == 409 and r.json()["error"] == "not_allowed"


def _components(engine, world):
    b = {n: _bind(engine, world, f"acme/{n}") for n in ("contracts", "api", "web")}
    return b, [{"name": "Contracts", "binding_id": b["contracts"]},
               {"name": "Backend", "binding_id": b["api"], "depends_on": ["Contracts"]},
               {"name": "Frontend", "binding_id": b["web"], "depends_on": ["Contracts", "Backend"]}]


def test_components_put_and_get_with_role_gating(client, world, engine):
    _b, comps = _components(engine, world)
    body = {"project_id": world.project_a, "app_name": "Storefront", "components": comps}
    for role in ("operator", "viewer"):
        with _as(world, role):
            assert client.put("/v1/apps/Storefront/components", json=body).status_code == 403
    with _as(world, "admin"):
        r = client.put("/v1/apps/Storefront/components", json=body)
    assert r.status_code == 200, r.text
    assert [c["name"] for c in r.json()["components"]] == ["Contracts", "Backend", "Frontend"]
    with _as(world, "viewer"):
        got = client.get("/v1/apps/storefront/components", params={"project_id": world.project_a}).json()
    assert got["repo_mode"] == "multi" and got["waves"] == [["Contracts"], ["Backend"], ["Frontend"]]
    with _as(world, "viewer"):
        consent = client.get("/v1/apps/storefront/consent", params={"project_id": world.project_a}).json()
    assert [r["full_name"] for r in consent["repos"]] == ["acme/api", "acme/contracts", "acme/web"]
    assert consent["unreachable"] == ["acme/mobile"]


def test_a_cycle_is_refused_in_words(client, world, engine):
    _b, comps = _components(engine, world)
    comps[0]["depends_on"] = ["Frontend"]
    with _as(world, "admin"):
        r = client.put("/v1/apps/Storefront/components",
                       json={"project_id": world.project_a, "components": comps})
    assert r.status_code == 422 and r.json()["error"] == "cycle" and "circle" in r.json()["message"]


def test_a_component_in_another_projects_repository_is_refused(client, world, engine):
    other = _bind(engine, world, "acme/web", pid=world.project_b)
    with _as(world, "admin"):
        r = client.put("/v1/apps/Storefront/components",
                       json={"project_id": world.project_a, "components": [{"name": "Web", "binding_id": other}]})
    # The guard confines ids to the project (404, no existence leak) before the engine's own 422.
    assert r.status_code in (404, 422), r.text
    if r.status_code == 422:
        assert r.json()["error"] == "repository_not_in_project"


def test_suggestions_for_a_viewer(client, world, engine):
    b, _ = _components(engine, world)
    with _as(world, "viewer"):
        r = client.post(f"/v1/projects/{world.project_a}/suggest-components",
                        json={"binding_ids": [b["web"], b["api"], b["contracts"]]})
    assert r.status_code == 200
    assert [c["name"] for c in r.json()["components"]] == ["Contracts", "Backend", "Frontend"]


def test_several_to_one_is_refused_while_an_app_uses_several(client, world, engine):
    _b, comps = _components(engine, world)
    with _as(world, "admin"):
        client.put("/v1/apps/Storefront/components", json={"project_id": world.project_a, "components": comps})
        r = client.put(f"/v1/projects/{world.project_a}/repo-mode", json={"repo_mode": "single"})
    assert r.status_code == 409 and r.json()["error"] == "apps_use_several"
