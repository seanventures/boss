"""A REPOSITORY WITH SEVERAL APPS SHOWS EACH PROJECT ITS OWN (BOSSAPP-931, FB-3/4).

The operator walked "Hello World", whose repository (paramg/boss-sandbox) holds a hello REST API,
web-nqueens, a stock app and more. Roadmap › Recommendations listed "Test serve_stock.py" and
"Test sudoku.js" — other apps' work — and Repo & knowledge showed the whole repository's dossier
and a graph of nqueens files. Here one repository holds three apps (hello-api/, web-nqueens/,
stock_core/) and two projects bind it (Hello owns hello-api/, Queens owns web-nqueens/):

* the Roadmap lists Hello's own items first; items about other apps sit in "elsewhere", grouped by
  app, with "Hand to Queens" for the app a project owns; repo-wide items say "Whole repo";
* "Hand to" moves the item to the project that owns the app;
* Repo & knowledge shows Hello's dossier and graph by default, the whole repository on
  `?scope=repo`;
* a project whose repository holds one app sees nothing scoped.

Real Postgres through the API; no model, no GitHub.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld

REPO = "acme/sandbox"
HELLO_FILES = ["hello-api/serve_hello.py", "hello-api/greeting.py", "hello-api/metrics.py"]
QUEENS_FILES = ["web-nqueens/app.js", "web-nqueens/board.js", "web-nqueens/solver.js",
                "web-nqueens/ui.js", "web-nqueens/storage.js"]
STOCK_FILES = ["stock_core/analysis.py", "stock_core/orders.py", "stock_core/prices.py",
               "stock_core/risk.py", "stock_core/api.py"]
TESTS = ["tests/test_hello_greeting.py", "tests/test_stock_orders.py"]


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:14]}"


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("scope-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def _project(world, client, name: str) -> str:
    slug = f"{name.lower()}-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": name, "slug": slug})
    assert made.status_code == 201, made.text
    return slug


def _bind(db, project_id: str, repo: str) -> None:
    conn = _id("ghc")
    db.execute(text(
        "INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
        "credential_ref, status, created_by) VALUES (:c, :p, 'seed', 'personal_access_token', "
        "'secret:none', 'active', 'seed')"), {"c": conn, "p": project_id})
    owner, name = repo.split("/")
    db.execute(text(
        "INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, "
        "full_name, role, default_branch, created_by) VALUES (:i, :p, :c, :o, :n, :r, 'primary', "
        "'main', 'seed')"), {"i": _id("ghb"), "p": project_id, "c": conn, "o": owner, "n": name,
                             "r": repo})


def _run(db, project_id: str, repo: str, impl: list[str], tests: list[str] = ()) -> None:
    db.execute(text(
        "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state) VALUES "
        "(:i, :p, 'completed', 'done', 'Build', CAST(:s AS JSONB), CAST(:st AS JSONB))"),
        {"i": _id("crun"), "p": project_id, "s": json.dumps({"owner": repo.split("/")[0],
                                                              "repo": repo.split("/")[1]}),
         "st": json.dumps({"impl_files": impl, "test_files": list(tests)})})


def _node(db, project_id: str, kind: str, name: str, path=None, layer="code", repo=REPO) -> str:
    nid = _id("kgn")
    db.execute(text(
        "INSERT INTO kg_nodes (id, project_id, kind, node_key, name, knowledge_layer, repo, path) "
        "VALUES (:i, :p, :k, :nk, :n, :kl, :r, :path)"),
        {"i": nid, "p": project_id, "k": kind, "nk": f"{kind}:{name}", "n": name, "kl": layer,
         "r": repo, "path": path})
    return nid


def _edge(db, project_id: str, a: str, b: str, kind="imports") -> None:
    db.execute(text("INSERT INTO kg_edges (id, project_id, src_id, dst_id, kind) VALUES "
                    "(:i, :p, :a, :b, :k)"), {"i": _id("kge"), "p": project_id, "a": a, "b": b,
                                              "k": kind})


def _rec(db, project_id: str, title: str, *, detector: str, subject: str, facts=None,
         score=50) -> str:
    wid = _id("wi")
    fp = _id("fp")
    db.execute(text(
        "INSERT INTO work_items (id, project_id, subject_type, subject_id, title, ambient, "
        "fingerprint, lifecycle, task_class, created_by) VALUES (:i, :p, 'roadmap', :s, :t, true, "
        ":f, 'proposed', 'roadmap_item', 'seed')"),
        {"i": wid, "p": project_id, "s": fp, "t": title, "f": fp})
    db.execute(text(
        "INSERT INTO ambient_recommendations (work_item_id, project_id, detector, kind, "
        "fingerprint, subject, title, why, proposed_shape, facts, score, priority_score) VALUES "
        "(:w, :p, :d, 'test_gap', :f, :sub, :t, :t, 'ticket', CAST(:fa AS JSONB), :sc, :sc)"),
        {"w": wid, "p": project_id, "d": detector, "f": fp, "sub": subject, "t": title,
         "fa": json.dumps(facts or {}), "sc": score})
    return wid


@pytest.fixture
def sandbox(world, client, engine):
    from boss_engine.services.ambient import dossier

    hello = _project(world, client, "Hello")
    queens = _project(world, client, "Queens")
    every = HELLO_FILES + QUEENS_FILES + STOCK_FILES + TESTS
    with engine.begin() as db:
        for pid in (hello, queens):
            _bind(db, pid, REPO)
        _run(db, hello, REPO, HELLO_FILES + ["package.json"], ["tests/test_hello_greeting.py"])
        _run(db, queens, REPO, QUEENS_FILES)
        ids = {p: _node(db, hello, "file", p, path=p) for p in every}
        rule = _node(db, hello, "rule", "Greetings are polite", layer="rules")
        hub = ids["hello-api/serve_hello.py"]
        for other in ("hello-api/greeting.py", "web-nqueens/app.js", "stock_core/api.py"):
            _edge(db, hello, hub, ids[other])
        _edge(db, hello, rule, hub, "governs")
        dossier.build(db, project_id=hello, repo=REPO, paths=every, seed_signals=False)
        db.execute(text(
            "UPDATE project_dossiers SET facts = facts || CAST(:f AS JSONB) WHERE project_id = :p"),
            {"p": hello, "f": json.dumps({
                "commands": {"test": ["pytest"], "build": ["npm run build --prefix web-nqueens"]},
                "hotspots": [{"file": "web-nqueens/app.js", "churn": 584, "lines": 584, "has_tests": True},
                             {"file": "hello-api/serve_hello.py", "churn": 120, "lines": 90, "has_tests": False},
                             {"file": "stock_core/analysis.py", "churn": 285, "lines": 285, "has_tests": True}]})})
        sig = _id("sig")
        db.execute(text(
            "INSERT INTO signals (id, project_id, source, kind, fingerprint, title, observed_at) "
            "VALUES (:i, :p, 'scan', 'scan.no_ci', :f, 'acme/sandbox has no CI', now())"),
            {"i": sig, "p": hello, "f": _id("f")})
        recs = {
            "queens_hot": _rec(db, hello, "Split up hotspot app.js", detector="hotspot",
                               subject="file:web-nqueens/app.js", score=90),
            "hello_tests": _rec(db, hello, "Add tests for greeting.py", detector="missing_tests",
                                subject="file:hello-api/greeting.py", score=80),
            "stock_tests": _rec(db, hello, "Test analysis.py", detector="onboarding",
                                subject="signal:sig_other", score=70,
                                facts={"where": "stock_core/analysis.py"}),
            "ci": _rec(db, hello, "Add CI", detector="onboarding", subject=f"signal:{sig}",
                       score=60, facts={"where": None}),
            "incident": _rec(db, hello, "Harden the stage", detector="slow_stage",
                             subject="stage:build", score=40),
        }
    return {"hello": hello, "queens": queens, "recs": recs}


def _get(world, client, path: str) -> dict:
    with world.acting_as(world.ctx(world.owner)):
        r = client.get(path)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_roadmap_shows_the_projects_own_items_first(world, client, sandbox):
    rm = _get(world, client, f"/v1/ambient/roadmap?project_id={sandbox['hello']}")
    recs = sandbox["recs"]
    mine = [i["id"] for i in rm["items"]]
    assert mine == [recs["hello_tests"], recs["ci"], recs["incident"]]
    assert [i["rank"] for i in rm["items"]] == [1, 2, 3]
    ci = next(i for i in rm["items"] if i["id"] == recs["ci"])
    assert ci["scope"]["where"] == "repo" and ci["scope"]["label"] == "Whole repo"
    assert rm["scope"]["scoped"] is True and rm["scope"]["roots"] == ["hello-api"]
    assert rm["scope"]["elsewhere_count"] == 2


def test_other_apps_items_are_grouped_by_app_with_hand_to_its_project(world, client, sandbox):
    rm = _get(world, client, f"/v1/ambient/roadmap?project_id={sandbox['hello']}")
    groups = {g["app"]: g for g in rm["elsewhere"]}
    assert set(groups) == {"web-nqueens", "stock_core"}
    queens = groups["web-nqueens"]
    assert [i["id"] for i in queens["items"]] == [sandbox["recs"]["queens_hot"]]
    assert queens["project"]["id"] == sandbox["queens"]
    assert queens["hand_to"] == {"label": "Hand to Queens", "project_id": sandbox["queens"]}
    stock = groups["stock_core"]
    assert stock["project"] is None and stock["hand_to"] is None
    assert stock["items"][0]["scope"]["path"] == "stock_core/analysis.py"


def test_hand_to_moves_the_item_to_the_project_that_owns_the_app(world, client, sandbox):
    item = sandbox["recs"]["queens_hot"]
    with world.acting_as(world.ctx(world.owner)):
        r = client.post(f"/v1/ambient/roadmap/{item}/hand-to",
                        json={"project_id": sandbox["hello"], "to_project": sandbox["queens"]})
    assert r.status_code == 200, r.text
    assert r.json()["moved"] is True
    hello = _get(world, client, f"/v1/ambient/roadmap?project_id={sandbox['hello']}")
    assert item not in [i["id"] for g in hello["elsewhere"] for i in g["items"]]
    queens = _get(world, client, f"/v1/ambient/roadmap?project_id={sandbox['queens']}")
    assert item in [i["id"] for i in queens["items"]]


def test_hand_to_refuses_a_project_outside_the_repository(world, client, sandbox):
    stranger = _project(world, client, "Stranger")
    with world.acting_as(world.ctx(world.owner)):
        r = client.post(f"/v1/ambient/roadmap/{sandbox['recs']['queens_hot']}/hand-to",
                        json={"project_id": sandbox["hello"], "to_project": stranger})
    assert r.status_code in (400, 409, 422), r.text


def test_repo_and_knowledge_shows_this_app_by_default(world, client, sandbox):
    k = _get(world, client, f"/v1/projects/{sandbox['hello']}/knowledge")
    assert k["scope"]["mode"] == "app" and k["scope"]["scoped"] is True
    assert [o["label"] for o in k["scope"]["options"]] == ["This app", "Whole repo"]
    d = k["dossier"]
    assert [h["path"] for h in d["hotspots"]] == ["hello-api/serve_hello.py"]
    assert d["stack"] == ["Python"], d["stack"]
    assert d["files"] == 4                      # three app files + its test in tests/
    assert d["tests"]["files"] == 1
    assert all(p["dir"].startswith("hello-api") or p["dir"] == "tests"
               for p in d["code_map"]["parts"]), d["code_map"]
    g = k["graph"]
    details = [n["detail"] for n in g["nodes"]]
    assert "hello-api/serve_hello.py" in details and "hello-api/greeting.py" in details
    assert not any(str(x).startswith(("web-nqueens/", "stock_core/")) for x in details), details
    assert any(n["group"] == "rule" for n in g["nodes"]), "a node with no path stays"


def test_the_whole_repo_is_one_switch_away(world, client, sandbox):
    k = _get(world, client, f"/v1/projects/{sandbox['hello']}/knowledge?scope=repo")
    assert k["scope"]["mode"] == "repo"
    paths = [h["path"] for h in k["dossier"]["hotspots"]]
    assert paths[0] == "web-nqueens/app.js" and "stock_core/analysis.py" in paths
    assert "JavaScript" in k["dossier"]["stack"]


def test_a_repository_with_one_app_changes_nothing(world, client, engine):
    from boss_engine.services.ambient import dossier

    solo = _project(world, client, "Solo")
    files = ["server/app.py", "server/db.py", "web/src/main.js", "web/src/list.js",
             "infra/main.tf"]
    with engine.begin() as db:
        _bind(db, solo, "acme/solo")
        _run(db, solo, "acme/solo", files[:4])
        for p in files:
            _node(db, solo, "file", p, path=p, repo="acme/solo")
        dossier.build(db, project_id=solo, repo="acme/solo", paths=files, seed_signals=False)
        rec = _rec(db, solo, "Add tests for db.py", detector="missing_tests",
                   subject="file:server/db.py")
    k = _get(world, client, f"/v1/projects/{solo}/knowledge")
    assert k["scope"]["scoped"] is False and k["scope"]["options"] == []
    assert k["dossier"].get("scope") is None
    rm = _get(world, client, f"/v1/ambient/roadmap?project_id={solo}")
    assert [i["id"] for i in rm["items"]] == [rec] and rm["elsewhere"] == []
    assert "scope" not in rm["items"][0]
