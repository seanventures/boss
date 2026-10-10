"""One app is one app, whichever handle names it, and its stack is its own (tester round 2, N3, N5).

N3: "Member portal" was made from login-oauth NODE (its template record says node), but a
stripe-checkout node apply onto it was refused "Member portal is a python app": the stack was read
from files, and the files came from a repository another (Python) app shares. The app's record
decides first: the stack of the templates applied to it.

N5: one app went by its display name, its slug and its stable app id, and the template lookup was
case-sensitive on the display name (GET /v1/apps/Member%20portal/templates -> [] while its
templates were filed under "member portal"). Every handle resolves to the same app.
"""
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
    return IdentityWorld.create(engine)


@pytest.fixture(scope="module")
def viewer(world):
    p = world.person("handles-viewer")
    world.bind(p, "viewer")
    return p


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _app(engine, world, name: str, *, files: list[str], stack: str | None) -> dict:
    run_id, app_id = _uid("crun"), _uid("app")
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state, finalize_ctx) "
            "VALUES (:id, :p, 'completed', 'done', :t, '{}'::jsonb, CAST(:s AS JSONB), CAST(:f AS JSONB))"),
            {"id": run_id, "p": world.project_a, "t": name,
             "s": json.dumps({"impl_files": files, "test_files": []}),
             "f": json.dumps({"app_id": app_id, "app_name": name})})
        if stack:
            tid = _uid("tpl")
            db.execute(text("INSERT INTO app_templates (id, org_id, template_key, version, source, status) "
                            "VALUES (:i, :o, :k, '1.0.0', 'builtin', 'active')"),
                       {"i": tid, "o": world.org, "k": f"login-{tid}"})
            db.execute(text(
                "INSERT INTO app_template_applications (id, project_id, app_key, template_id, version, stack, "
                "status) VALUES (:i, :p, :a, :t, '1.0.0', :s, 'blocked')"),
                {"i": _uid("tap"), "p": world.project_a, "a": name.lower(), "t": tid, "s": stack})
    return {"run_id": run_id, "app_id": app_id}


# ---- N3: the stack is the app's own ----------------------------------------------------------

def test_the_apps_template_record_decides_its_stack_not_a_shared_repos_files(world, engine):
    from boss_engine.templates import apply as apply_service
    from boss_engine.templates.service import TemplateError

    name = f"Member portal {uuid.uuid4().hex[:4]}"
    # its builds' files are another app's Python (a shared repository); its record says node
    _app(engine, world, name, files=["app.py", "requirements.txt"], stack="node")
    with engine.begin() as db:
        app = apply_service.resolve_app(db, world.project_a, app_key=name)
    assert app["stacks"] == ["node"]
    apply_service.check_stack(app, "node")                     # accepted
    with pytest.raises(TemplateError) as refused:
        apply_service.check_stack(app, "python")
    assert refused.value.body["error"] == "stack_mismatch" and "is a node app" in refused.value.body["message"]


def test_an_app_with_no_template_record_still_reads_its_own_files(world, engine):
    from boss_engine.templates import apply as apply_service

    name = f"Notes {uuid.uuid4().hex[:4]}"
    _app(engine, world, name, files=["web/package.json", "web/server.js"], stack=None)
    with engine.begin() as db:
        app = apply_service.resolve_app(db, world.project_a, app_key=name)
    assert app["stacks"] == []
    apply_service.check_stack(app, "node")


# ---- N5: every handle names the same app ------------------------------------------------------

def test_every_handle_resolves_to_one_app(world, engine):
    from boss_engine.app_links.service import canonical_app_key, resolve_app_id

    name = f"Member Portal {uuid.uuid4().hex[:4]}"
    ids = _app(engine, world, name, files=["web/package.json"], stack="node")
    key = name.lower()
    with engine.begin() as db:
        for handle in (name, name.upper(), key, key.replace(" ", "-"), ids["run_id"], ids["app_id"]):
            assert canonical_app_key(db, world.project_a, handle) == key, handle
            assert resolve_app_id(db, world.project_a, handle) == ids["app_id"], handle
        # an app with no build yet (secrets are set before a new app's first build): normalised
        assert canonical_app_key(db, world.project_a, "Brand-New APP") == "brand new app"


def test_the_templates_route_finds_the_app_by_its_display_name(world, engine, viewer):
    name = f"Member portal {uuid.uuid4().hex[:4]}"
    ids = _app(engine, world, name, files=["web/package.json"], stack="node")
    client = TestClient(main.app, raise_server_exceptions=False)
    with world.acting_as(world.ctx(viewer)):
        for handle in (name, name.upper(), name.lower().replace(" ", "-"), ids["app_id"]):
            r = client.get(f"/v1/apps/{handle}/templates", params={"project_id": world.project_a})
            assert r.status_code == 200, r.text
            assert r.json()["app_key"] == name.lower(), handle
            assert [t["stack"] for t in r.json()["templates"]] == ["node"], handle
