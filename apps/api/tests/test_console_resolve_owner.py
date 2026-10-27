"""Console deep links resolve an object to ITS OWN project before anything renders (Build L).

Covers: the shared resolver (boss_engine.identity.object_kinds + the new `self_fix` kind), the
console-facing resolve-owner route, and the link-generation retrofit that stamps `project_id`
onto every BOSS-generated console link.
"""
from __future__ import annotations

import uuid

from sqlalchemy import text

from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity import object_kinds


def test_self_fix_kind_resolves_to_its_own_project_not_the_requested_one(engine):
    """P0: the `self_fix` object kind (new in this build) resolves a self-fix row to the project
    it actually belongs to, never the project a caller happens to be asking from."""
    world = IdentityWorld.create(engine)
    sfx_id = f"sfx_{uuid.uuid4().hex[:10]}"
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO self_fixes (id, project_id, signature, class_signature, status, confidence, created_by) "
            "VALUES (:id, :p, 'sig', 'sig', 'open', 'low', 'system')"),
            {"id": sfx_id, "p": world.project_b})
    spec = object_kinds.get("self_fix")
    with engine.connect() as db:
        resolved = spec.load(db, sfx_id)
    assert resolved is not None
    org_id, project_id = resolved
    assert project_id == world.project_b
    assert project_id != world.project_a


def test_resolve_owner_route_switches_to_the_builds_own_project(engine, client):
    """P0: opening a build by id while a different project is selected answers the build's own
    project (and name), the core fix for the reported render-then-flip bug."""
    world = IdentityWorld.create(engine)
    alice = world.principal("alice")
    world.bind(alice, "operator", project=world.project_a)
    world.bind(alice, "operator", project=world.project_b)
    build_id = f"crun_{uuid.uuid4().hex[:10]}"
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, title) VALUES (:id, :p, 'running', 'test build')"),
            {"id": build_id, "p": world.project_b})
    with world.acting_as(world.ctx(alice)):
        resp = client.get("/v1/console/resolve-owner",
                           params={"type": "build", "id": build_id, "project_id": world.project_a})
    assert resp.status_code == 200
    body = resp.json()
    assert body["project_id"] == world.project_b
    assert body["can_view"] is True


def test_resolve_owner_denies_not_found_and_access_denied_identically(engine, client):
    """P1 (N5, F5): a nonexistent object and an object the caller cannot see must be
    indistinguishable to the caller — same status, same shape, and never a raw id in the body."""
    world = IdentityWorld.create(engine)
    alice = world.principal("alice")
    world.bind(alice, "operator", project=world.project_a)
    missing_id = f"crun_{uuid.uuid4().hex[:10]}"
    hidden_id = f"crun_{uuid.uuid4().hex[:10]}"
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, title) VALUES (:id, :p, 'running', 'test build')"),
            {"id": hidden_id, "p": world.project_b})
    with world.acting_as(world.ctx(alice)):
        missing_resp = client.get("/v1/console/resolve-owner",
                                   params={"type": "build", "id": missing_id})
        hidden_resp = client.get("/v1/console/resolve-owner",
                                  params={"type": "build", "id": hidden_id})
    assert missing_resp.status_code == hidden_resp.status_code
    missing_body, hidden_body = missing_resp.json(), hidden_resp.json()
    assert missing_body.get("can_view") is False
    assert hidden_body.get("can_view") is False
    assert missing_id not in missing_resp.text
    assert hidden_id not in hidden_resp.text
    assert world.project_b not in hidden_resp.text


def test_resolve_owner_fails_closed_on_unknown_object_type(client):
    """P1 (N1): an unrecognised object type must answer a denial, never a crash or stale data."""
    resp = client.get("/v1/console/resolve-owner", params={"type": "not_a_real_kind", "id": "x"})
    assert resp.status_code in (400, 404)
    assert resp.json().get("can_view", False) is False


def test_every_generated_console_link_carries_project_id():
    """P1 (F7, acceptance #2): every BOSS-generated console link is built through the shared
    helper and always carries `project_id`, so a copy/pasted link can self-correct."""
    from boss_engine.app_links.console import build_console_link

    link = build_console_link("observability", project_id="proj_demo123", view="alerts")
    assert "project_id=proj_demo123" in link
    assert "area=observability" in link


def test_link_generated_total_is_never_recorded_without_a_project_id():
    """P1 (spec §7 N6, acceptance #2): `link_generated_total{surface,has_project_id}` must be
    zero in the "false" bucket for every surface — `build_console_link` cannot emit a link
    without `project_id`, so no caller can ever be recorded as having skipped it."""
    from boss_engine.app_links import console

    console.build_console_link("code_tasks", project_id="proj_metric_check", build="crun_x")
    assert console.link_generated_metric_value("code_tasks", False) == 0
    assert console.link_generated_metric_value("code_tasks", True) >= 1


def test_resolve_owner_route_records_the_console_resolve_owner_metric(engine, client):
    """P1 (spec §7 N6, acceptance #2): a successful resolve increments
    `console_resolve_owner_total{object_type="build", result="resolved"}`."""
    from boss_engine.services import console_links

    world = IdentityWorld.create(engine)
    alice = world.principal("alice")
    world.bind(alice, "operator", project=world.project_a)
    world.bind(alice, "operator", project=world.project_b)
    build_id = f"crun_{uuid.uuid4().hex[:10]}"
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, title) VALUES (:id, :p, 'running', 'test build')"),
            {"id": build_id, "p": world.project_b})
    before = console_links.resolve_owner_metric_value("build", "resolved")
    with world.acting_as(world.ctx(alice)):
        resp = client.get("/v1/console/resolve-owner",
                           params={"type": "build", "id": build_id, "project_id": world.project_a})
    assert resp.status_code == 200
    assert console_links.resolve_owner_metric_value("build", "resolved") == before + 1
