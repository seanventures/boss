"""The first deploy of an app that declares how it runs takes the manifest path, the manifest is
the app's own, and a plan that cannot work is refused in words before anyone approves it.

From wfr_9575c86ab4a74defa0d2 (Onboarding, 2026-10-13) and HelpDesk's moves between targets:
  #2 a declared app with no target fell to the one-container deploy_app, which made a target and
     then had its apply refused (deprun_22f9650e);
  #3 the build edited the repository ROOT's boss.deploy.yaml (stack-demo's) and wrote its app in
     onboarding-path-builder/: the root manifest was taken as the app's;
  #3 a Postgres app shipped with no database;
  #6 a target whose only deploy failed kept :8000, pushing HelpDesk to :8001;
  #9 a manifest that pins another PORT, or health-checks a route the app does not serve.
"""
from __future__ import annotations

import json
import os
import socket
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import deployment_fleet
from boss_engine.services.app_deploy_manifest import app_folder, declared_manifest, load_manifest, render
from boss_engine.services.stack_deploy import (
    manifest_misfit,
    stack_target_for_deploy,
    undeclared_deploy_refusal,
)
from boss_engine.services.workflow_capability_runner import DEPLOY_BRIEF_MARKER, DEPLOY_MANIFEST_BRIEF

TEST_DB = os.environ.get("BOSS_TEST_DATABASE_URL",
                         "postgresql+psycopg://boss:boss@localhost:5432/boss_test")
#: TEST-NET-1, never a real machine — and unique per process, because the port book is global.
ADDRESS = f"192.0.2.{uuid.uuid4().int % 250 + 2}"
ONBOARDING = ["boss.deploy.yaml", "onboarding-path-builder/server.mjs",
              "onboarding-path-builder/package.json", "onboarding-path-builder/boss.preview.command"]
NOTES = ["notes/server.mjs", "notes/package.json", "notes/boss.deploy.yaml"]


# ── the manifest is the app's own ───────────────────────────────────────────────────────────

def test_an_apps_own_folder_is_told_from_what_its_build_wrote():
    assert app_folder(ONBOARDING) == "onboarding-path-builder"
    assert app_folder(["boss.deploy.yaml", "package.json", "src/app.js"]) is None
    assert app_folder(["src/app.js", "src/db.js"]) is None, "src/ is part of a root app"
    assert declared_manifest(NOTES) == "notes/boss.deploy.yaml"
    assert declared_manifest(ONBOARDING) == "boss.deploy.yaml", "judged by its content at plan"


STACK_DEMO = """
version: 1
name: stack-demo
kind: compose
compose: {file: docker-compose.yml}
health: [{url: "{target.public_url}", expect: 200}]
"""
HELPDESK_MINI = """
version: 1
name: helpdesk-mini
kind: commands
install: ["cd helpdesk && npm ci", "cd helpdesk && (nohup npx tsx src/server/index.ts &)"]
health: [{url: "{target.public_url}/healthz", expect: 200}]
"""


def test_another_apps_root_manifest_is_refused_at_plan(db):
    from boss_engine.services.stack_deploy import root_manifest_misfit

    with db.engine.begin() as c:
        onboarding = _build(c, db.project, ONBOARDING, app="Onboarding", briefed=True)
        refused = root_manifest_misfit(c, load_manifest(STACK_DEMO), onboarding,
                                       {"docker-compose.yml": "services: {web: {build: .}}"}.get)
        assert refused and "'stack-demo'" in refused[0] and "onboarding-path-builder/" in refused[0]
        assert "onboarding-path-builder/boss.deploy.yaml" in refused[1]
        # The HelpDesk shape: a root manifest whose steps run in the app's folder is the app's.
        helpdesk = _build(c, db.project, ["boss.deploy.yaml", "helpdesk/package.json",
                                          "helpdesk/src/server/index.ts"], app="HelpDesk")
        assert root_manifest_misfit(c, load_manifest(HELPDESK_MINI), helpdesk, {}.get) is None
        # A root manifest named for the folder is the app's too; a root app owns the root one.
        named = load_manifest(STACK_DEMO.replace("stack-demo", "onboarding-path-builder"))
        assert root_manifest_misfit(c, named, onboarding, {}.get) is None
        root_app = _build(c, db.project, ["boss.deploy.yaml", "package.json", "server.mjs"])
        assert root_manifest_misfit(c, load_manifest(STACK_DEMO), root_app, {}.get) is None


def test_the_brief_asks_for_the_apps_own_manifest_and_its_database():
    assert DEPLOY_MANIFEST_BRIEF.startswith(DEPLOY_BRIEF_MARKER)
    assert "APP'S OWN FOLDER" in DEPLOY_MANIFEST_BRIEF
    assert "belongs to another app" in DEPLOY_MANIFEST_BRIEF
    assert "database.relational" in DEPLOY_MANIFEST_BRIEF and "postgres" in DEPLOY_MANIFEST_BRIEF


# ── fixtures ────────────────────────────────────────────────────────────────────────────────

class _Closed:
    """No socket ever reaches a machine: every probed port is free."""

    def __init__(self, *a, **k):
        pass

    def settimeout(self, *_):
        pass

    def connect(self, *_):
        raise OSError("closed")

    def close(self):
        pass


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setattr(socket, "socket", _Closed)
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            wid = c.execute(text("SELECT id FROM workspaces LIMIT 1")).scalar()
            if wid is None:
                c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_boss', 'BOSS') "
                               "ON CONFLICT DO NOTHING"))
                c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES "
                               "('ws_first_deploy', 'org_boss', 'ws') ON CONFLICT DO NOTHING"))
                wid = "ws_first_deploy"
    except Exception as error:  # pragma: no cover
        pytest.skip(f"boss_test unavailable: {error}")
    pid = f"p_{uuid.uuid4().hex[:10]}"
    host = f"dh_{pid}"
    with engine.begin() as c:
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'first deploy test', :i)"), {"i": pid, "w": wid})
        c.execute(text("INSERT INTO deployment_hosts (id, project_id, name, address, status, "
                       " port_min, port_max) VALUES (:h, :p, 'vm', :a, 'verified', 8000, 8999)"),
                  {"h": host, "p": pid, "a": ADDRESS})
    yield SimpleNamespace(engine=engine, project=pid, host=host)
    with engine.begin() as c:
        c.execute(text("DELETE FROM deployment_runs WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM code_task_runs WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM deployment_targets WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM deployment_hosts WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM events WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})


def _build(c, pid, files, *, app="Notes", briefed=False, postgres=False):
    run_id = f"crun_{uuid.uuid4().hex[:12]}"
    spec = {"owner": "acme", "repo": "boss-sandbox",
            "instructions": f"build it\n\n{DEPLOY_MANIFEST_BRIEF}" if briefed else "build it"}
    state = {"impl_files": files, "build_database": {"postgres": postgres, "mode": "auto"}}
    c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, spec, state, "
                   " finalize_ctx, pull_request_number) VALUES (:i, :p, :t, 'completed', "
                   " CAST(:s AS JSONB), CAST(:st AS JSONB), CAST(:f AS JSONB), 7)"),
              {"i": run_id, "p": pid, "t": app, "s": json.dumps(spec), "st": json.dumps(state),
               "f": json.dumps({"app_name": app})})
    return run_id


def _target(c, pid, host, app, port, *, status="active"):
    tid = f"deptgt_{uuid.uuid4().hex[:12]}"
    c.execute(text("INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
                   " target_kind, target_ref, host_id, domains, created_by, status) VALUES "
                   " (:t, :p, :a, 'boss-sandbox', 'staging', 'ssh', 'root@x:22', :h, "
                   " CAST(:d AS JSONB), 'ceo', :s)"),
              {"t": tid, "p": pid, "a": app, "h": host, "d": json.dumps([f"{ADDRESS}:{port}"]),
               "s": status})
    return tid


def _deploy_run(c, pid, tid, app, status):
    c.execute(text("INSERT INTO deployment_runs (id, project_id, deployment_target_id, app_name, "
                   " environment, repo, target_kind, target_ref, status, created_by) VALUES "
                   " (:i, :p, :t, :a, 'staging', 'boss-sandbox', 'ssh', 'root@x:22', :s, 'ceo')"),
              {"i": f"deprun_{uuid.uuid4().hex[:12]}", "p": pid, "t": tid, "a": app, "s": status})


# ── #2: the first deploy of a declared app takes the manifest path ───────────────────────────

def test_a_declared_apps_first_deploy_makes_its_target_on_the_steps_host(db):
    with db.engine.begin() as c:
        run_id = _build(c, db.project, NOTES)
        asked = stack_target_for_deploy(c, db.project, {"run_id": run_id, "host_id": db.host})
        assert asked and asked.get("pending_create") and asked["id"] is None, asked
        assert asked["manifest_path"] == "notes/boss.deploy.yaml"
        assert not c.execute(text("SELECT 1 FROM deployment_targets WHERE project_id = :p"),
                             {"p": db.project}).first(), "asking never makes a target"
        made = stack_target_for_deploy(c, db.project, {"run_id": run_id, "host_id": db.host},
                                       create=True)
    assert made and made["id"] and made["host_id"] == db.host, made
    assert made["manifest_path"] == "notes/boss.deploy.yaml"
    assert made["app_name"] == "Notes"
    with db.engine.begin() as c:
        again = stack_target_for_deploy(c, db.project, {"run_id": run_id, "host_id": db.host},
                                        create=True)
    assert again["id"] == made["id"], "the second deploy ships to the target the first made"


def test_an_app_that_declares_nothing_keeps_the_one_container_path(db):
    with db.engine.begin() as c:
        run_id = _build(c, db.project, ["web/index.html"])
        assert stack_target_for_deploy(c, db.project, {"run_id": run_id, "host_id": db.host},
                                       create=True) is None


# ── #3: a deploy that cannot work is refused in words ────────────────────────────────────────

def test_a_briefed_build_that_wrote_no_manifest_of_its_own_is_refused(db):
    with db.engine.begin() as c:
        onboarding = _build(c, db.project, ONBOARDING[1:], app="Onboarding", briefed=True)
        said = undeclared_deploy_refusal(c, onboarding)
        assert said and "onboarding-path-builder/boss.deploy.yaml" in said, said
        # Told and did it: deployable.
        assert undeclared_deploy_refusal(c, _build(c, db.project, NOTES, briefed=True)) is None
        # Never told, no database: the one-container path stays.
        assert undeclared_deploy_refusal(c, _build(c, db.project, ["web/index.html"])) is None
        # A Postgres app with no manifest to bring the database: refused.
        pg = undeclared_deploy_refusal(c, _build(c, db.project, ["api/server.mjs", "api/db.mjs"],
                                                 postgres=True))
        assert pg and "Postgres" in pg and "database.relational" in pg


MANIFEST = """
version: 1
name: notes
kind: commands
install: ["npm ci && (PORT=$PORT node server.mjs &)"]
health: [{url: "{target.public_url}%s", expect: 200}]
%s
"""


def _misfit(c, run_id, body, *, health="", files=None, target_port=8000):
    manifest = load_manifest(MANIFEST % (health, body))
    target = {"id": "deptgt_x", "app_name": "Notes", "target_kind": "ssh",
              "domains": [f"{ADDRESS}:{target_port}"], "host_id": "dh"}
    host = {"id": "dh", "address": ADDRESS, "name": "vm"}
    rendered = render(manifest, {"commit": "c" * 40, "ref": "pull/7", "target.host": ADDRESS,
                                 "target.user": "root",
                                 "target.public_url": f"http://{ADDRESS}:{target_port}"})
    files = files or {}
    return manifest_misfit(c, manifest=manifest, rendered=rendered, path="notes/boss.deploy.yaml",
                           target=target, host=host, run_id=run_id,
                           read=lambda p: files.get(p))


def test_a_postgres_app_whose_manifest_brings_no_database_is_refused_at_plan(db):
    served = {"notes/server.mjs": "app.get('/', ...)"}
    with db.engine.begin() as c:
        run_id = _build(c, db.project, NOTES, postgres=True)
        refused = _misfit(c, run_id, "", files=served)
        assert refused and "Postgres" in refused[0] and "database.relational" in refused[1]
        assert _misfit(c, run_id, "resources: {db: {capability: database.relational, "
                                  "engine: postgres}}", files=served) is None
        plain = _build(c, db.project, NOTES)
        assert _misfit(c, plain, "", files=served) is None


def test_a_manifest_on_another_port_or_an_unserved_health_route_is_refused_at_plan(db):
    served = {"notes/server.mjs": "app.get('/health', ok); app.get('/', home)"}
    with db.engine.begin() as c:
        run_id = _build(c, db.project, NOTES)
        pinned = _misfit(c, run_id, "env: {PORT: '3000'}", files=served)
        assert pinned and "PORT=3000" in pinned[0] and "8000" in pinned[0], pinned
        assert _misfit(c, run_id, "env: {PORT: '8000'}", files=served) is None
        unserved = _misfit(c, run_id, "", health="/readyz", files=served)
        assert unserved and "/readyz" in unserved[0], unserved
        assert _misfit(c, run_id, "", health="/health", files=served) is None


def test_an_imported_apps_own_manifest_is_not_judged_by_the_files_one_build_changed(db):
    """helpdesk-ai 2026-11-06: the imported app's boss.deploy.yaml health-checks /auth/providers,
    served by existing code the SLA build never touched. Judging it against the build's changed
    files dead-lettered a correct deploy. Only a manifest the build wrote is judged that way."""
    changed = {"notes/server.mjs": "app.get('/', home)"}
    with db.engine.begin() as c:
        run_id = _build(c, db.project, ["notes/server.mjs", "notes/package.json"])
        assert _misfit(c, run_id, "", health="/auth/providers", files=changed) is None


# ── #6: a target whose every deploy failed holds no port; an app keeps its port ──────────────

def test_a_target_whose_deploys_all_failed_holds_no_port_and_the_app_gets_its_port_back(db):
    with db.engine.begin() as c:
        helpdesk_old = _target(c, db.project, db.host, "HelpDesk", 8000, status="retired")
        _deploy_run(c, db.project, helpdesk_old, "HelpDesk", "succeeded")
        onboarding = _target(c, db.project, db.host, "Onboarding", 8000)
        _deploy_run(c, db.project, onboarding, "Onboarding", "failed")
        fresh = _target(c, db.project, db.host, "Fresh", 8002)       # no deploy yet: holds it
        running = _target(c, db.project, db.host, "Running", 8003)
        _deploy_run(c, db.project, running, "Running", "failed")
        _deploy_run(c, db.project, running, "Running", "succeeded")
        taken = deployment_fleet._ports_assigned_on(c, ADDRESS)
        assert 8000 not in taken, taken
        assert taken.get(8002) == "Fresh" and taken.get(8003) == "Running"
        made = deployment_fleet.create_deployment_target(
            c, project_id=db.project, app_name="HelpDesk", environment="production",
            repo="boss-sandbox", host_id=db.host, actor="test")
    assert made["ok"] and made["port"] == 8000, made


def test_a_compose_manifest_with_a_postgres_service_brings_the_database(db):
    from boss_engine.services.stack_deploy import manifest_declares_database

    manifest = load_manifest("version: 1\nname: notes\nkind: compose\n"
                             "compose: {file: docker-compose.yml}\n"
                             "health: [{url: \"{target.public_url}\", expect: 200}]\n")
    with_db = {"docker-compose.yml": "services:\n  db:\n    image: postgres:16\n"}
    assert manifest_declares_database(manifest, with_db.get)
    assert not manifest_declares_database(manifest, {"docker-compose.yml": "services: {}"}.get)


def test_a_production_deploy_flags_an_app_that_refuses_its_demo_password(db):
    from boss_engine.services.stack_deploy import refuses_in_production

    config = ("const prod = process.env.NODE_ENV === 'production';\n"
              "if (!prod && !process.env.DEMO_PASSWORD) throw new Error('DEMO_PASSWORD is required');\n"
              "if (prod && process.env.DEMO_PASSWORD) {\n"
              "  throw new Error('DEMO_PASSWORD must not be set in production');\n}\n")
    assert refuses_in_production(config, "DEMO_PASSWORD")
    assert not refuses_in_production("const seed = process.env.ALLOW_DEMO_SEED === '1' && "
                                      "process.env.DEMO_PASSWORD;\n", "DEMO_PASSWORD")
    served = {"notes/server.mjs": "app.get('/', home)", "notes/config.mjs": config}
    with db.engine.begin() as c:
        run_id = _build(c, db.project, NOTES + ["notes/config.mjs"])
        manifest = load_manifest(MANIFEST % ("", "env: {DEMO_PASSWORD: {generate: password}}"))
        target = {"id": "t", "app_name": "Notes", "target_kind": "ssh", "environment": "production",
                  "domains": [f"{ADDRESS}:8000"], "host_id": "dh"}
        host = {"id": "dh", "address": ADDRESS, "name": "vm"}
        rendered = render(manifest, {"commit": "c" * 40, "ref": "pull/7", "target.host": ADDRESS,
                                     "target.user": "root", "target.public_url": f"http://{ADDRESS}:8000"})

        def misfit(env, provided=()):
            return manifest_misfit(c, manifest=manifest, rendered=rendered,
                                   path="notes/boss.deploy.yaml", target={**target, "environment": env},
                                   host=host, run_id=run_id, read=served.get, provided=provided)

        refused = misfit("production")
        assert refused and "DEMO_PASSWORD in production" in refused[0] and "ALLOW_DEMO_SEED" in refused[1]
        assert misfit("staging") is None


# ── D2: a second app in a shared repository gets its own target ─────────────────────────────

def test_a_second_app_in_the_repository_never_ships_to_the_first_apps_stack_target(db):
    """paramg/boss-sandbox holds HelpDesk AI's stack target. Order API, built in the same
    repository, matched it as "the one stack target for this repository" and its first deploy
    would have been planned onto HelpDesk's target. A target is the app's own: its manifest, its
    lineage's deploys, or its name."""
    with db.engine.begin() as c:
        helpdesk = _target(c, db.project, db.host, "HelpDesk AI", 8000)
        c.execute(text("UPDATE deployment_targets SET target_kind = 'stack', repo = "
                       "'acme/boss-sandbox', manifest_path = 'helpdesk-ai/boss.deploy.yaml' "
                       "WHERE id = :t"), {"t": helpdesk})
        orders = _build(c, db.project, ["order-api/boss.deploy.yaml", "order-api/app/main.py"],
                        app="Order API")
        first = stack_target_for_deploy(c, db.project, {"run_id": orders, "host_id": db.host})
        assert first and first.get("pending_create"), first
        assert first["manifest_path"] == "order-api/boss.deploy.yaml"
        made = stack_target_for_deploy(c, db.project, {"run_id": orders, "host_id": db.host},
                                       create=True)
        assert made["id"] != helpdesk and made["app_name"] == "Order API"
        c.execute(text("UPDATE deployment_targets SET target_kind = 'stack', repo = "
                       "'acme/boss-sandbox', manifest_path = 'order-api/boss.deploy.yaml' "
                       "WHERE id = :t"), {"t": made["id"]})
        # its next deploy — and HelpDesk's — each find their own
        again = stack_target_for_deploy(c, db.project, {"run_id": orders})
        hd_build = _build(c, db.project, ["helpdesk-ai/boss.deploy.yaml"], app="HelpDesk AI")
        hd = stack_target_for_deploy(c, db.project, {"run_id": hd_build})
    assert again["id"] == made["id"]
    assert hd["id"] == helpdesk


def test_a_target_shipped_from_its_manifest_is_found_as_such(db):
    """D2: the Order API's target (made by its first deploy) stays `ssh` by kind; its next release
    from Infra must still ship from the app's manifest, not as one guessed container."""
    from boss_engine.services.stack_deploy import shipped_from_manifest

    with db.engine.begin() as c:
        tid = _target(c, db.project, db.host, "Order API", 8001)
        assert shipped_from_manifest(c, tid) is None
        c.execute(text("INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, "
                       " commit_sha, manifest, status, created_by) VALUES (:i, :p, :t, "
                       " 'acme/boss-sandbox', 'main', 'c0a16f9', CAST(:m AS JSONB), 'healthy', 't')"),
                  {"i": f"sd_{uuid.uuid4().hex[:8]}", "p": db.project, "t": tid,
                   "m": json.dumps({"manifest_path": "order-api/boss.deploy.yaml"})})
        got = shipped_from_manifest(c, tid)
        c.execute(text("DELETE FROM stack_deployments WHERE target_id = :t"), {"t": tid})
    assert got == {"manifest_path": "order-api/boss.deploy.yaml",
                   "repository": "acme/boss-sandbox"}


# ── adding to an imported app deploys as that app, never as the task ─────────────────────────

def test_a_feature_on_an_imported_app_deploys_to_the_projects_own_target(db):
    """helpdesk-ai 2026-11-06: the SLA build recorded no app name, so its title stood in and the
    deploy made a second target "Add SLA timers to tickets" beside "HelpDesk AI" (made from Ask
    BOSS, with no repo recorded). A build at the root of the project's primary repository IS the
    project's app."""
    with db.engine.begin() as c:
        c.execute(text("UPDATE projects SET name = 'HelpDesk AI' WHERE id = :p"), {"p": db.project})
        c.execute(text("INSERT INTO github_connections (id, project_id, connection_label, "
                       " auth_method, credential_ref, created_by) VALUES (:i, :p, 'test', "
                       " 'github_app', 'env:NONE', 'ceo')"),
                  {"i": f"ghconn_{db.project}", "p": db.project})
        c.execute(text("INSERT INTO github_repository_bindings (id, project_id, connection_id, "
                       " owner, name, full_name, role, default_branch, status, created_by) VALUES "
                       " (:i, :p, :c, 'acme', 'boss-sandbox', 'acme/boss-sandbox', 'primary', "
                       " 'main', 'active', 'ceo')"),
                  {"i": f"ghrepo_{db.project}", "p": db.project, "c": f"ghconn_{db.project}"})
        own = _target(c, db.project, db.host, "HelpDesk AI", 8000)
        c.execute(text("UPDATE deployment_targets SET repo = '' WHERE id = :t"), {"t": own})
        sla = _build(c, db.project, ["boss.deploy.yaml", "server/sla.mjs"],
                     app="Add SLA timers to tickets")
        c.execute(text("UPDATE code_task_runs SET finalize_ctx = '{\"app_dir\": \"web\"}' "
                       "WHERE id = :i"), {"i": sla})
        got = stack_target_for_deploy(c, db.project, {"run_id": sla, "host_id": db.host})
        c.execute(text("DELETE FROM github_repository_bindings WHERE project_id = :p"),
                  {"p": db.project})
        c.execute(text("DELETE FROM github_connections WHERE project_id = :p"), {"p": db.project})
    assert got and got.get("id") == own, got
