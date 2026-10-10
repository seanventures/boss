"""Multi-repo phases 1–2: the org's registry, a project's repositories, an app's components.

Real Postgres; GitHub is a recording stand-in (the lister and the prober), so every refusal and
every write is the product's own code against the real schema."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.multirepo import components, project_repos, registry
from boss_engine.multirepo.common import MultiRepoError

from ._multirepo_support import ACME, bind, lister_for, make_engine, make_world, prober_for


@pytest.fixture(scope="module")
def eng():
    return make_engine()


@pytest.fixture()
def world(eng):
    return make_world(eng)


def _ids(db, org):
    return {r["full_name"]: r["id"] for r in db.execute(text("SELECT id, full_name FROM github_repos WHERE org_id = :o"),
                                                         {"o": org}).mappings()}


def _refresh(eng, world, items=ACME):
    with eng.begin() as db:
        return registry.refresh(db, get_settings(), world["projects"][0], actor="t", lister=lister_for(items))


# ── registry ─────────────────────────────────────────────────────────────────────────────────

def test_first_refresh_keeps_every_repository_a_project_already_uses(eng, world):
    bind(eng, world, world["projects"][0], "acme/web", role="primary")
    out = _refresh(eng, world)
    assert out["first"] is True and out["listed"] == 6
    with eng.begin() as db:
        reg = {i["full_name"]: i for i in registry.list_registry(db, world["projects"][0])["items"]}
    assert reg["acme/web"]["allowed"] is True                    # in use: never taken away
    assert reg["acme/api"]["allowed"] is False                   # new: the operator decides
    assert reg["acme/legacy-admin"]["state"] == "archived"
    assert reg["acme/web"]["used_by"] == [{"project_id": world["projects"][0], "name": "Acme Shop"}]


def test_an_archived_or_unreachable_repository_cannot_be_ticked(eng, world):
    _refresh(eng, world)
    _refresh(eng, world, [i for i in ACME if i["full_name"] != "acme/mobile"])     # gone from the App
    with eng.begin() as db:
        ids = _ids(db, world["org"])
        for name, code in (("acme/legacy-admin", "archived"), ("acme/mobile", "unreachable")):
            with pytest.raises(MultiRepoError) as err:
                registry.set_allowed(db, get_settings(), world["projects"][0], ids[name], True, prober=prober_for())
            assert err.value.code == code


def test_ticking_runs_the_three_checks_and_says_read_only(eng, world):
    _refresh(eng, world)
    calls: list[str] = []
    with eng.begin() as db:
        ids = _ids(db, world["org"])
        api = registry.set_allowed(db, get_settings(), world["projects"][0], ids["acme/api"], True,
                                   prober=prober_for(calls=calls))
        infra = registry.set_allowed(db, get_settings(), world["projects"][0], ids["acme/infra"], True,
                                     prober=prober_for({"acme/infra": "read_only"}, calls=calls))
    assert calls == ["acme/api", "acme/infra"]
    assert api["state"] == "working" and api["checks"]["pull_requests"]["ok"] is True
    assert infra["state"] == "read_only" and infra["checks"]["write"]["ok"] is False


def test_unticking_removes_the_repository_from_every_project(eng, world):
    a, b = world["projects"]
    bind(eng, world, a, "acme/api", role="primary")
    bind(eng, world, b, "acme/api", role="primary")
    _refresh(eng, world)
    with eng.begin() as db:
        ids = _ids(db, world["org"])
        registry.set_allowed(db, get_settings(), a, ids["acme/api"], False)
        active = db.execute(text("SELECT count(*) FROM github_repository_bindings WHERE lower(full_name) = 'acme/api' "
                                 "AND project_id = ANY(:p) AND status = 'active'"), {"p": [a, b]}).scalar()
    assert active == 0


def test_the_next_mint_refuses_an_unticked_repository_and_an_unused_registry_refuses_nothing(eng, world):
    with eng.begin() as db:
        assert registry.mint_allowed(db, world["org"], ["acme/api"]) is None          # registry never used
    _refresh(eng, world)
    with eng.begin() as db:
        ids = _ids(db, world["org"])
        registry.set_allowed(db, get_settings(), world["projects"][0], ids["acme/api"], True, prober=prober_for())
        assert registry.mint_allowed(db, world["org"], ["acme/api", "acme/mobile"]) == ["acme/mobile"]
        assert registry.mint_allowed(db, world["org"], ["acme/api"]) == []


def test_save_allowed_ticks_exactly_the_list(eng, world):
    _refresh(eng, world)
    with eng.begin() as db:
        ids = _ids(db, world["org"])
        out = registry.save_allowed(db, get_settings(), world["projects"][0],
                                    [ids["acme/api"], ids["acme/web"]], prober=prober_for())
        assert sorted(i["full_name"] for i in out["items"] if i["allowed"]) == ["acme/api", "acme/web"]
        with pytest.raises(MultiRepoError):
            registry.save_allowed(db, get_settings(), world["projects"][0], ["ghr_not_mine"], prober=prober_for())


# ── project repositories ─────────────────────────────────────────────────────────────────────

def _allowed_world(eng, world, names=("acme/contracts", "acme/api", "acme/web", "acme/infra")):
    _refresh(eng, world)
    with eng.begin() as db:
        ids = _ids(db, world["org"])
        for n in names:
            registry.set_allowed(db, get_settings(), world["projects"][0], ids[n], True,
                                 prober=prober_for({"acme/infra": "read_only"}))
    return ids


def _add(db, pid, rid, hint=None):
    return project_repos.add(db, get_settings(), pid, registry_repo_id=rid, role_hint=hint, check=False)


def test_a_project_adds_only_allowed_repositories(eng, world):
    ids = _allowed_world(eng, world)
    pid = world["projects"][0]
    with eng.begin() as db:
        with pytest.raises(MultiRepoError) as err:
            _add(db, pid, ids["acme/mobile"])
        assert err.value.code == "not_allowed"
        first = _add(db, pid, ids["acme/contracts"], "contracts")
        _add(db, pid, ids["acme/api"], "backend")
        listed = project_repos.list_project_repos(db, pid)
    assert first["role_hint"] == "contracts"
    by = {i["full_name"]: i for i in listed["items"]}
    assert by["acme/contracts"]["is_default"] is True and by["acme/api"]["is_default"] is False
    assert {a["full_name"] for a in listed["addable"]} == {"acme/web", "acme/infra"}
    assert listed["not_allowed_count"] == 2                     # mobile, legacy-admin


def test_bad_role_hints_and_branches_are_refused(eng, world):
    ids = _allowed_world(eng, world)
    pid = world["projects"][0]
    with eng.begin() as db:
        with pytest.raises(MultiRepoError):
            _add(db, pid, ids["acme/api"], "database")
        b = _add(db, pid, ids["acme/api"], "backend")["binding_id"]
        for branch in ("boss/r1-x", "a..b"):
            with pytest.raises(MultiRepoError):
                project_repos.update(db, pid, b, base_branch=branch)


def _storefront(db, pid, ids):
    b = {n: _add(db, pid, ids[n], h)["binding_id"] for n, h in
         (("acme/contracts", "contracts"), ("acme/api", "backend"), ("acme/web", "frontend"), ("acme/infra", "deploy_config"))}
    return b, [
        {"name": "Frontend", "binding_id": b["acme/web"], "path": "/apps/storefront", "depends_on": ["Contracts", "Backend"]},
        {"name": "Contracts", "binding_id": b["acme/contracts"]},
        {"name": "Backend", "binding_id": b["acme/api"], "depends_on": ["Contracts"]},
        {"name": "Deploy config", "binding_id": b["acme/infra"], "kind": "deploy_config", "path": "/envs/storefront"},
    ]


def test_components_go_in_dependency_order_and_the_app_becomes_several_repos(eng, world):
    ids = _allowed_world(eng, world)
    pid = world["projects"][0]
    with eng.begin() as db:
        b, comps = _storefront(db, pid, ids)
        db.execute(text("UPDATE github_repository_bindings SET checks = jsonb_set(checks, '{write}', "
                        "'{\"ok\": false}') WHERE id = :i"), {"i": b["acme/infra"]})
        out = components.put(db, pid, "storefront", comps, app_name="Storefront")
        assert [c["name"] for c in out["components"]] == ["Contracts", "Backend", "Frontend"]
        assert out["waves"] == [["Contracts"], ["Backend"], ["Frontend"]]
        assert out["deploy_config"][0]["access"] == "read"
        assert out["repo_mode"] == "multi" and components.is_multi(db, pid, "storefront")
        repos = components.app_repos(db, pid, "storefront")
        assert [(r["full_name"], r["access"]) for r in repos] == [
            ("acme/api", "write"), ("acme/contracts", "write"), ("acme/web", "write"), ("acme/infra", "read")]


@pytest.mark.parametrize("mutate,code", [
    (lambda c: c[1].update(depends_on=["Frontend"]), "cycle"),
    (lambda c: c[2].update(depends_on=["Nope"]), "unknown_dependency"),
    (lambda c: c[3].update(access="write"), "deploy_config_read_only"),
    (lambda c: c[0].update(access="read"), "read_only_build_target"),
    (lambda c: c[2].update(name="Contracts"), "duplicate_name"),
    (lambda c: c[0].update(path="/../etc"), "bad_path"),
    (lambda c: c[0].update(binding_id="ghb_somebody_elses"), "repository_not_in_project"),
])
def test_a_bad_component_list_is_refused_with_its_reason(eng, world, mutate, code):
    ids = _allowed_world(eng, world)
    pid = world["projects"][0]
    with eng.begin() as db:
        _b, comps = _storefront(db, pid, ids)
        mutate(comps)
        with pytest.raises(MultiRepoError) as err:
            components.put(db, pid, "storefront", comps)
        assert err.value.code == code


def test_a_read_only_repository_is_never_a_build_target(eng, world):
    ids = _allowed_world(eng, world)
    pid = world["projects"][0]
    with eng.begin() as db:
        b, comps = _storefront(db, pid, ids)
        db.execute(text("UPDATE github_repository_bindings SET checks = jsonb_set(checks, '{write}', "
                        "'{\"ok\": false}') WHERE id = :i"), {"i": b["acme/infra"]})
        comps[3].update(kind="code", access="write")
        with pytest.raises(MultiRepoError) as err:
            components.put(db, pid, "storefront", comps)
        assert err.value.code == "read_only_build_target"


def test_removing_a_repository_an_app_uses_is_refused_and_several_to_one_waits(eng, world):
    ids = _allowed_world(eng, world)
    pid = world["projects"][0]
    with eng.begin() as db:
        b, comps = _storefront(db, pid, ids)
        components.put(db, pid, "storefront", comps, app_name="Storefront")
        project_repos.set_mode(db, pid, "multi")
        with pytest.raises(MultiRepoError) as err:
            project_repos.remove(db, pid, b["acme/api"])
        assert err.value.code == "in_use" and err.value.extra["apps"][0]["app_name"] == "Storefront"
        with pytest.raises(MultiRepoError) as err2:
            project_repos.set_mode(db, pid, "single")
        assert err2.value.code == "apps_use_several"
        listed = project_repos.list_project_repos(db, pid)
        assert listed["repo_mode"] == "multi"
        assert listed["apps"][0]["chain"] == "contracts → api → web"


def test_suggestions_come_from_names_and_languages(eng, world):
    ids = _allowed_world(eng, world)
    pid = world["projects"][0]
    with eng.begin() as db:
        b = {n: _add(db, pid, ids[n])["binding_id"] for n in ("acme/web", "acme/infra", "acme/api", "acme/contracts")}
        out = components.suggest(db, pid, list(b.values()))["components"]
    assert [(c["name"], c["repository"], c["kind"]) for c in out] == [
        ("Contracts", "acme/contracts", "code"), ("Backend", "acme/api", "code"),
        ("Frontend", "acme/web", "code"), ("Deploy config", "acme/infra", "deploy_config")]
    assert out[2]["depends_on"] == ["Contracts", "Backend"] and "web" in out[2]["reason"]
    assert out[3]["access"] == "read"
