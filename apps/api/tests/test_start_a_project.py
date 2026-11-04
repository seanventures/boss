"""Start a project (BOSSAPP-810) on the real API: import a repository → a project → its first
workflow, and the sample.

The console's New project flow makes no write of its own; it calls the routes that exist, in
order. This walks that order against the database: POST /v1/projects, PUT the registry repository
into it, read what BOSS reads (GET /v1/github/repos/{id}/facts), read the project page
(GET /v1/projects/{id}/start), Start (POST /v1/delivery-workflows/start with what the page says),
and see the work on the page. The sample applies the Hello World built-in as a new app through the
smallest planned workflow. GitHub is never called: the prober, the connection and the tree are
stand-ins.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.multirepo import project_repos, registry
from boss_engine.services import setup_actions, start_project

OK = {"ok": True, "state": "working"}
TREE = ["build.gradle", "settings.gradle", "src/main/java/org/acme/OrdersApplication.java",
        "src/main/resources/application.yml", "src/test/java/org/acme/OrdersTest.java"]


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("start-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def github(monkeypatch, world, engine):
    """One allowed repository in the org's registry, a project connection that reaches it, and
    GitHub's tree for it — all without the network."""
    repo_id = f"ghr_{uuid.uuid4().hex[:8]}"
    full_name = f"acme/order-service-{repo_id[-4:]}"
    with engine.begin() as db:
        db.execute(text("INSERT INTO github_repos (id, org_id, full_name, language, default_branch, allowed, "
                        "checks, checked_at) VALUES (:i, :o, :f, 'Java', 'develop', true, CAST(:c AS JSONB), now())"),
                   {"i": repo_id, "o": world.org, "f": full_name,
                    "c": json.dumps({"read": OK, "write": OK, "pull_requests": OK})})

    def connection_for(db, settings, project_id, actor, **_kw):
        cid = f"ghc_{project_id}"
        db.execute(text("INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
                        "credential_ref, status, created_by) VALUES (:c, :p, 'token', 'personal_access_token', "
                        "'secret:none', 'active', 'test') ON CONFLICT DO NOTHING"), {"c": cid, "p": project_id})
        return cid

    def select_repository(db, settings, project_id, connection_id, full, *, make_default, actor):
        return project_repos._bind_unchecked(db, settings, project_id, connection_id, full,  # noqa: SLF001
                                             make_default=make_default, actor=actor)

    monkeypatch.setattr(project_repos, "_connection_for", connection_for)
    monkeypatch.setattr(setup_actions, "select_repository", select_repository)
    monkeypatch.setattr(registry, "default_prober", lambda *a, **k: {"state": "working", "checks": {
        "read": OK, "write": dict(OK), "pull_requests": dict(OK)}})
    monkeypatch.setattr(start_project, "github_paths", lambda db, org, full, branch: list(TREE))
    return {"id": repo_id, "full_name": full_name}


def _consent(client, refused) -> bool:
    """The console's consent sheet, answered "For 30 days" (lib/identity/consent.ts grantBodyFor)."""
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


def _send(client, path, body):
    """POST a start the way the console does: consent asked and given, a readiness refusal
    answered once with Start anyway (the person ticks it), then sent again."""
    r = client.post(path, json=body)
    for _ in range(3):
        if _consent(client, r):
            r = client.post(path, json=body)
            continue
        missing = _missing(r)
        if missing and not body.get("start_anyway"):
            body = {**body, "start_anyway": missing}
            r = client.post(path, json=body)
            continue
        break
    return r


def test_import_a_repo_then_start_a_feature_on_it(world, client, github, engine):
    slug = f"order-service-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "Order service", "slug": slug})
        assert made.status_code == 201, made.text

        # BOSS reads it, before the repository is in the project.
        facts = client.get(f"/v1/github/repos/{github['id']}/facts", params={"project_id": slug})
        assert facts.status_code == 200, facts.text
        f = facts.json()
        assert f["full_name"] == github["full_name"] and f["branch"] == "develop"
        assert f["facts"] == {"language": "Java", "framework": "Spring", "build": "Gradle",
                              "tests": True, "runs_as": "Web API"}
        assert f["read_from"] == "github" and f["in_project"] is False
        assert f["mapping"]["state"] == "not_started"
        assert f["suggested_name"].startswith("Order service")

        # Before a repository, the page builds a new app (the project.new default).
        before = client.get(f"/v1/projects/{slug}/start").json()
        assert before["repo"] is None and before["start"]["mode"] == "new_app"
        assert before["start"]["work_class"] == "project.new"

        # Create project → the repository joins it (the same PUT the Knowledge Graph import uses).
        put = client.put(f"/v1/projects/{slug}/repos/{github['id']}", json={})
        assert put.status_code == 200, put.text
        assert [i["full_name"] for i in put.json()["items"]] == [github["full_name"]]

        page = client.get(f"/v1/projects/{slug}/start")
        assert page.status_code == 200, page.text
        view = page.json()
        assert view["name"] == "Order service"
        assert view["repo"]["full_name"] == github["full_name"]
        assert view["repo"]["branch"] == "develop"
        assert view["repo"]["mapping"]["state"] in ("queued", "running", "done")
        start = view["start"]
        # Nobody mapped small features here, so Start names the catalogue's Minor Feature.
        assert start["mode"] == "feature"
        assert (start["work_class"], start["workflow_key"]) == (None, "minor_feature")
        assert start["workflow_name"] == "Minor Feature" and start["minutes"] > 0
        assert view["examples"][0] == "Add an API endpoint" and len(view["next"]) == 5
        assert view["work"] == []

        # Start: the page's own choice, through the one start door, on the imported repository.
        started = _send(client, "/v1/delivery-workflows/start", {
            "project_id": slug, "workflow_key": start["workflow_key"],
            "input": {"directive": "Add an endpoint that lists orders by customer",
                      "title": "Orders by customer", "repository": github["full_name"]}})
        assert started.status_code == 200, started.text
        run_id = (started.json().get("run") or {}).get("id")
        assert run_id

        after = client.get(f"/v1/projects/{slug}/start").json()
    with engine.connect() as db:
        row = db.execute(text("SELECT r.project_id, d.workflow_key, r.input->>'repository' FROM workflow_runs r "
                              "JOIN workflow_definitions d ON d.id = r.workflow_definition_id WHERE r.id = :r"),
                         {"r": run_id}).one()
    assert tuple(row) == (slug, "minor_feature", github["full_name"])
    assert [w["run_id"] for w in after["work"]] == [run_id]
    first = after["work"][0]
    assert first["title"] == "Orders by customer" and first["action"]["label"] in ("Watch", "Review plan", "Open")


def test_a_project_nobody_can_read_is_refused(world, client):
    stranger = world.person("start-stranger")
    with world.acting_as(world.ctx(stranger)):
        r = client.get(f"/v1/projects/{world.project_a}/start")
    assert r.status_code in (403, 404)


def test_the_sample_applies_hello_world_as_a_new_app(world, client, engine):
    slug = f"hello-world-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "Hello World", "slug": slug})
        assert made.status_code == 201, made.text
        sample = client.get(f"/v1/projects/{slug}/start").json()["sample"]
        assert sample == {"template": "hello-world", "name": "Hello World", "stack": "node",
                          "workflow_key": "minor_feature"}
        body = {"project_id": slug, "new_app": {"name": sample["name"]}, "stack": sample["stack"],
                "workflow_key": sample["workflow_key"], "dedupe_key": f"sample:{slug}"}
        applied = _send(client, f"/v1/templates/{sample['template']}/apply", body)
        assert applied.status_code == 201, applied.text
        run_id = applied.json().get("workflow_run_id")
        assert run_id
        page = client.get(f"/v1/projects/{slug}/start").json()
    assert [w["run_id"] for w in page["work"]] == [run_id]
    with engine.connect() as db:
        key = db.execute(text("SELECT d.workflow_key FROM workflow_runs r JOIN workflow_definitions d "
                              "ON d.id = r.workflow_definition_id WHERE r.id = :r"), {"r": run_id}).scalar()
    assert key == "minor_feature"
