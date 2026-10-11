"""Tester round 2 (N7, N11, grouping): the app platform names what it is talking about, refuses
what would collide, and shows one problem once.

  N7   the consent sheet said "To run this workflow run": it names the workflow and the app;
  N11  a second template build started on an app while the first was still building: refused,
       in words ("Wait for … to finish, or cancel it");
  grouping  "Server leaks version via Server header" was listed three times (/, /robots.txt,
       /sitemap.xml): a header the server sends on every route is one problem.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from apps.api.tests.identity_fixtures import IdentityWorld


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _app_run(engine, project_id: str, name: str, *, status: str = "completed") -> dict:
    run_id, app_id = _uid("crun"), _uid("app")
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state, finalize_ctx) "
            "VALUES (:id, :p, :st, 'done', :t, '{}'::jsonb, '{\"impl_files\": [\"web/package.json\"]}'::jsonb, "
            "CAST(:f AS JSONB))"),
            {"id": run_id, "p": project_id, "st": status, "t": name,
             "f": json.dumps({"app_id": app_id, "app_name": name})})
    return {"run_id": run_id, "app_id": app_id}


def _definition(engine, project_id: str, name: str) -> str:
    d = _uid("wfd")
    with engine.begin() as db:
        db.execute(text("INSERT INTO workflow_definitions (id, project_id, workflow_key, name, namespace, created_by) "
                        "VALUES (:d, :p, :k, :n, 'product', 'ceo')"), {"d": d, "p": project_id, "k": d, "n": name})
    return d


# ---- N7 ------------------------------------------------------------------------------------------

def test_the_consent_sheet_names_the_workflow_and_the_app(world, engine):
    from boss_engine.services.workflows import consent_task_title

    p = world.project_a
    name = f"Member portal {uuid.uuid4().hex[:4]}"
    ids = _app_run(engine, p, name)
    d = _definition(engine, p, "Security review")
    with engine.begin() as db:
        # started from App > Security with only the app's id: the app's NAME, not its id
        assert consent_task_title(db, p, d, {"app_key": ids["app_id"]}) == f"Security review for {name}"
        assert consent_task_title(db, p, d, {"title": "Add sign-in"}) == "Security review: Add sign-in"
        assert consent_task_title(db, p, d, {}) == "Security review"
        assert "this workflow run" not in consent_task_title(db, p, d, {})


# ---- N11 -----------------------------------------------------------------------------------------

def _building_application(engine, world, app_key: str, *, wf_status: str) -> None:
    p = world.project_a
    d = _definition(engine, p, "Fast fix")
    v, w, tid = _uid("wfv"), _uid("wfr"), _uid("tpl")
    with engine.begin() as db:
        db.execute(text("INSERT INTO workflow_versions (id, project_id, workflow_definition_id, version, spec, "
                        "content_hash, created_by) VALUES (:v, :p, :d, 1, '{}'::jsonb, 'h', 'ceo')"),
                   {"v": v, "p": p, "d": d})
        db.execute(text("INSERT INTO workflow_runs (id, project_id, workflow_definition_id, workflow_version_id, "
                        "actor, status, input) VALUES (:w, :p, :d, :v, 'ceo', :s, '{}'::jsonb)"),
                   {"w": w, "p": p, "d": d, "v": v, "s": wf_status})
        db.execute(text("INSERT INTO app_templates (id, org_id, template_key, version, source, status, manifest) "
                        "VALUES (:i, :o, :k, '1.0.0', 'builtin', 'active', CAST(:m AS JSONB))"),
                   {"i": tid, "o": world.org, "k": f"catalog-cart-{tid}", "m": json.dumps({"name": "Catalog cart"})})
        db.execute(text("INSERT INTO app_template_applications (id, project_id, app_key, template_id, version, stack, "
                        "workflow_run_id, status) VALUES (:i, :p, :a, :t, '1.0.0', 'python', :w, 'building')"),
                   {"i": _uid("tap"), "p": p, "a": app_key, "t": tid, "w": w})


def test_a_second_template_build_waits_for_the_first(world, engine):
    from boss_engine.templates import apply as apply_service
    from boss_engine.templates.service import TemplateError

    name = f"Shop {uuid.uuid4().hex[:4]}"
    _app_run(engine, world.project_a, name)
    _building_application(engine, world, name.lower(), wf_status="running")
    with engine.begin() as db:
        app = apply_service.resolve_app(db, world.project_a, app_key=name)
        with pytest.raises(TemplateError) as refused:
            apply_service.check_not_building(db, world.project_a, app)
    assert refused.value.status == 409 and refused.value.body["error"] == "app_building"
    assert refused.value.body["message"].startswith(f"Wait for Catalog cart to finish building on {name}")
    assert "or cancel that build" in refused.value.body["message"]


def test_an_application_whose_run_ended_does_not_hold_the_app(world, engine):
    from boss_engine.templates import apply as apply_service

    name = f"Shop {uuid.uuid4().hex[:4]}"
    _app_run(engine, world.project_a, name)
    _building_application(engine, world, name.lower(), wf_status="cancelled")
    with engine.begin() as db:
        app = apply_service.resolve_app(db, world.project_a, app_key=name)
        apply_service.check_not_building(db, world.project_a, app)          # no refusal


def test_any_running_build_of_the_app_holds_it(world, engine):
    from boss_engine.templates import apply as apply_service
    from boss_engine.templates.service import TemplateError

    name = f"Shop {uuid.uuid4().hex[:4]}"
    _app_run(engine, world.project_a, name)
    _app_run(engine, world.project_a, name, status="running")
    with engine.begin() as db:
        app = apply_service.resolve_app(db, world.project_a, app_key=name)
        with pytest.raises(TemplateError) as refused:
            apply_service.check_not_building(db, world.project_a, app)
    assert "Wait for the build of" in refused.value.body["message"]


# ---- grouping ------------------------------------------------------------------------------------

def _zap(path: str, title: str, evidence: str = "", attack: str = "") -> dict:
    return {"id": _uid("f"), "tool": "zap", "rule_id": "10036", "title": title,
            "location": {"method": "GET", "path": path, "param": ""},
            "evidence": {"evidence": evidence, "attack": attack}, "severity": "low", "status": "open"}


def test_a_header_the_server_sends_everywhere_is_one_problem():
    from boss_engine.security_scan.service import group_findings, group_key

    leak = 'Server Leaks Version Information via "Server" HTTP Response Header Field'
    found = [_zap(p, leak, "nginx/1.25.3") for p in ("/", "/robots.txt", "/sitemap.xml")]
    assert len({group_key(f) for f in found}) == 1
    groups = group_findings(found)
    assert len(groups) == 1 and groups[0]["count"] == 3 and groups[0]["route_class"] == "every route"
    # a MISSING header still splits page from asset (an asset needs no CSP), as designed
    missing = [_zap(p, "Content Security Policy (CSP) Header Not Set") for p in ("/", "/robots.txt")]
    assert len({group_key(f) for f in missing}) == 2
    # and an attack's evidence is about the route it hit
    attacked = [_zap(p, "Header injection", "x", attack="%0d%0a") for p in ("/", "/robots.txt")]
    assert len({group_key(f) for f in attacked}) == 2
