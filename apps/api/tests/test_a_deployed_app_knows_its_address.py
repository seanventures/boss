"""Two app-platform gaps, each fixed as a class.

1. A DEPLOYED APP KNOWS ITS ADDRESS. A template that declares the well-known env APP_BASE_URL
   (login-oauth, stripe/paypal checkout, catalog-cart) got it filled for previews only; on a
   deploy it stayed at the template's default (http://localhost:3000), so OAuth callbacks pointed
   at localhost. Now `templates.base_url.deploy_environment` is THE reader of a deployed app's
   environment: deploy secrets + APP_BASE_URL from the deployment target. The setup guide shows
   the deployed URL and each provider's callback URL to register.

2. A TEMPLATE IS APPLIED THROUGH ANY WORKFLOW THAT BUILDS. `GET /v1/templates/workflows` lists
   the project's published workflows with a build step (the console's picker); apply refuses a
   workflow that cannot build; and the run input carries no `repository`, so a build step that
   binds it from input builds in the project's primary repository (blank = primary), or the
   app's own repository when it extends the app.
"""
from __future__ import annotations

import base64
import json
import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity import vault
from boss_engine.templates import apply as apply_service
from boss_engine.templates import base_url
from boss_engine.templates import secrets as app_secrets
from boss_engine.templates.service import get_row, manifest_of, sync_builtins

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


@pytest.fixture(scope="module")
def operator(world):
    p = world.person("addr-operator")
    world.bind(p, "operator")
    return p


@pytest.fixture(autouse=True)
def master_key(monkeypatch):
    monkeypatch.setenv("BOSS_MASTER_KEY", base64.b64encode(b"a" * 32).decode())
    monkeypatch.delenv("BOSS_MASTER_KEY_FILE", raising=False)
    vault.reset_master_key_cache()
    yield
    vault.reset_master_key_cache()


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def _apply(engine, world, app_key: str, template_key: str) -> dict:
    with engine.begin() as db:
        sync_builtins(db, world.org)
        row = get_row(db, world.org, template_key)
        db.execute(text("INSERT INTO app_template_applications (id, project_id, app_key, template_id, version, "
                        "stack, status) VALUES (:i, :p, :a, :t, :v, 'node', 'applied')"),
                   {"i": f"tapp_{uuid.uuid4().hex[:10]}", "p": world.project_a, "a": app_key, "t": row["id"],
                    "v": row["version"]})
        app_secrets.ensure_test_defaults(db, org_id=world.org, project_id=world.project_a, app_key=app_key,
                                         manifest=manifest_of(row), actor_id="usr_test")
    return dict(row)


def _target(engine, world, app_name: str, **cols) -> dict:
    row = {"id": f"dt_{uuid.uuid4().hex[:8]}", "project_id": world.project_a, "app_name": app_name,
           "repo": "acme/demo", "environment": f"prod-{uuid.uuid4().hex[:4]}", "target_kind": "ssh",
           "target_ref": cols.pop("target_ref", "deploy@192.0.2.202"), "status": "active", "created_by": "test",
           "domains": json.dumps(cols.pop("domains", [])),
           "required_resources": json.dumps(cols.pop("required_resources", []))}
    with engine.begin() as db:
        db.execute(text("INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
                        "target_ref, status, created_by, domains, required_resources) VALUES (:id, :project_id, "
                        ":app_name, :repo, :environment, :target_kind, :target_ref, :status, :created_by, "
                        "CAST(:domains AS JSONB), CAST(:required_resources AS JSONB))"), row)
        for k, v in cols.items():
            db.execute(text(f"UPDATE deployment_targets SET {k} = :v WHERE id = :i"), {"v": v, "i": row["id"]})
        return dict(db.execute(text("SELECT * FROM deployment_targets WHERE id = :i"),
                               {"i": row["id"]}).mappings().first())


PORT = [{"kind": "app_port", "name": "3012"}]


# ---- 1. the address --------------------------------------------------------------------------------

@pytest.mark.parametrize("target,base", [
    # A host slot: no domain yet, BOSS chose the port.
    ({"target_ref": "deploy@192.0.2.202", "required_resources": PORT}, "http://192.0.2.202:3012"),
    # The domain names a stale port: the app port wins (the connector's own rule).
    ({"target_ref": "deploy@192.0.2.202", "domains": ["192.0.2.202:3000"], "required_resources": PORT},
     "http://192.0.2.202:3012"),
    ({"target_ref": "deploy@h", "domains": ["https://help.example.test/"], "required_resources": PORT},
     "https://help.example.test"),
    # A configured public URL outranks everything.
    ({"target_ref": "deploy@h", "public_url": "https://desk.example.test/", "domains": ["h:3012"],
      "required_resources": PORT}, "https://desk.example.test"),
    ({"target_ref": "deploy@h", "domains": ["desk.example.test"]}, "https://desk.example.test"),
    ({"target_ref": ""}, ""),
])
def test_a_targets_public_base_url(target, base):
    assert base_url.target_base_url(target) == base


def test_the_callbacks_come_from_the_templates_routes_and_providers():
    import yaml

    m = yaml.safe_load((ROOT / "templates/apps/login-oauth/boss-template.yaml").read_text())
    assert base_url.declares_base_url(m)
    got = {c["provider"]: c["url"] for c in base_url.callbacks([m], "http://192.0.2.202:3012")}
    assert got == {"google": "http://192.0.2.202:3012/auth/callback/google",
                   "github": "http://192.0.2.202:3012/auth/callback/github",
                   "boss": "http://192.0.2.202:3012/auth/callback/boss"}
    stripe = yaml.safe_load((ROOT / "templates/apps/stripe-checkout/boss-template.yaml").read_text())
    assert base_url.declares_base_url(stripe) and base_url.callbacks([stripe], "http://x:1") == []


def test_every_built_in_that_builds_absolute_urls_declares_the_well_known_name():
    """The class: a template whose code reads APP_BASE_URL declares it, so BOSS fills it."""
    import yaml

    for manifest in sorted((ROOT / "templates/apps").glob("*/boss-template.yaml")):
        code = "\n".join(p.read_text(errors="replace") for p in manifest.parent.glob("stacks/**/*")
                         if p.is_file() and p.suffix in (".py", ".mjs", ".js", ".java"))
        if base_url.BASE_URL_ENV in code:
            assert base_url.declares_base_url(yaml.safe_load(manifest.read_text())), manifest.parent.name


def test_a_deploy_fills_app_base_url_for_a_template_that_declares_it(engine, world):
    _apply(engine, world, "helpdesk", "login-oauth")
    target = _target(engine, world, "HelpDesk", required_resources=PORT)
    with engine.begin() as db:
        env, _withheld, address = base_url.deploy_environment(
            db, org_id=world.org, project_id=world.project_a, app_key="helpdesk", target=target)
    assert env["APP_BASE_URL"] == "http://192.0.2.202:3012"
    assert address["base_url"] == "http://192.0.2.202:3012"
    assert {"provider": "github", "url": "http://192.0.2.202:3012/auth/callback/github"} in address["callbacks"]
    assert "Callback URL for github: http://192.0.2.202:3012/auth/callback/github" in base_url.address_lines(address)


def test_a_bound_app_base_url_wins(engine, world):
    _apply(engine, world, "desk2", "login-oauth")
    target = _target(engine, world, "Desk2", required_resources=PORT)
    with engine.begin() as db:
        app_secrets.bind_issued(db, org_id=world.org, project_id=world.project_a, app_key="desk2",
                                name="APP_BASE_URL", env="deploy", value="https://desk.example.test",
                                actor_id="usr_test")
        env, _w, address = base_url.deploy_environment(
            db, org_id=world.org, project_id=world.project_a, app_key="desk2", target=target)
    assert env["APP_BASE_URL"] == "https://desk.example.test"
    assert address["callbacks"][0]["url"].startswith("https://desk.example.test/auth/callback/")


def test_an_app_whose_templates_do_not_declare_it_gets_nothing(engine, world):
    _apply(engine, world, "plain", "hello")
    target = _target(engine, world, "Plain", required_resources=PORT)
    with engine.begin() as db:
        env, _w, address = base_url.deploy_environment(
            db, org_id=world.org, project_id=world.project_a, app_key="plain", target=target)
    assert "APP_BASE_URL" not in env and address == {}


def test_every_deploy_reads_the_apps_environment_through_the_one_reader():
    """The enforcing rule: no deploy path reads `deploy` values on its own (it would skip
    APP_BASE_URL again). values_for_env(env="deploy") is called only by base_url."""
    engine_src = ROOT / "engine" / "boss_engine"
    offenders = []
    for path in list(engine_src.rglob("*.py")) + list((ROOT / "apps/api/app").rglob("*.py")):
        if path.name == "base_url.py" and path.parent.name == "templates":
            continue
        src = path.read_text(errors="replace")
        for call in re.finditer(r"values_for_env\((?:[^()]|\([^()]*\))*\)", src):
            if re.search(r"env\s*=\s*['\"]deploy['\"]", call.group(0)):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
    from boss_engine.services import governed_host_deploy
    import inspect

    assert "deploy_environment(" in inspect.getsource(governed_host_deploy._apply_on_host)  # noqa: SLF001


def test_the_setup_guide_shows_the_deployed_url_and_each_callback(engine, world, operator, client):
    _apply(engine, world, "guided", "login-oauth")
    with world.acting_as(world.ctx(operator)):
        before = client.get("/v1/apps/guided/setup-guide", params={"project_id": world.project_a}).json()
    copy = before["guides"][0]["copy"]
    assert copy["deploy_url"] == "(deploy the app to get its address)"
    assert not [k for k in copy if k.startswith("callback_url:")]
    _target(engine, world, "Guided", domains=["192.0.2.202:3012"], required_resources=PORT)
    with world.acting_as(world.ctx(operator)):
        after = client.get("/v1/apps/guided/setup-guide", params={"project_id": world.project_a}).json()
    g = after["guides"][0]
    assert g["copy"]["deploy_url"] == "http://192.0.2.202:3012"
    assert g["copy"]["callback_url:github"] == "http://192.0.2.202:3012/auth/callback/github"
    assert "http://192.0.2.202:3012/auth/callback/google" in g["guide"]
    assert "{{deploy_url}}" not in g["guide"] and "<your deployed host>" not in g["guide"]


# ---- 2. any workflow that builds ---------------------------------------------------------------------

BUILD_SPEC = {"entry_step": "build", "steps": [
    {"id": "build", "kind": "capability", "next": [], "binding": {"capability": "build", "inputs": {
        "title": {"from_input": "title"}, "repository": {"from_input": "repository"},
        "instructions": {"from_input": "directive"}}}}]}
NO_BUILD_SPEC = {"entry_step": "report", "steps": [
    {"id": "report", "kind": "capability", "next": [], "binding": {"capability": "report", "inputs": {}}}]}


def _workflow(engine, project_id: str, key: str, name: str, spec: dict, catalogue: bool = False) -> None:
    did, vid = f"wfd_{uuid.uuid4().hex[:12]}", f"wfv_{uuid.uuid4().hex[:12]}"
    with engine.begin() as db:
        db.execute(text("INSERT INTO workflow_definitions (id, project_id, workflow_key, name, status, namespace, "
                        "is_catalogue, created_by) VALUES (:i, :p, :k, :n, 'active', 'product', :c, 'test')"),
                   {"i": did, "p": project_id, "k": key, "n": name, "c": catalogue})
        db.execute(text("INSERT INTO workflow_versions (id, project_id, workflow_definition_id, version, status, spec, "
                        "content_hash, created_by) VALUES (:v, :p, :d, 1, 'published', CAST(:s AS JSONB), :h, 'test')"),
                   {"v": vid, "p": project_id, "d": did, "s": json.dumps(spec), "h": uuid.uuid4().hex})
        db.execute(text("UPDATE workflow_definitions SET active_version_id = :v WHERE id = :d"), {"v": vid, "d": did})


@pytest.fixture
def workflows(engine, world):
    """Per test: the suite's cleanup empties the workflow tables between tests."""
    _workflow(engine, world.project_a, "fast_fix", "Fast Fix / Hotfix", BUILD_SPEC, catalogue=True)
    _workflow(engine, world.project_a, "helpdesk_delivery_d1", "HelpDesk Delivery (D1)", BUILD_SPEC)
    _workflow(engine, world.project_a, "research_only", "Research only", NO_BUILD_SPEC)


def test_the_picker_lists_the_projects_workflows_that_build_default_first(world, operator, client, workflows):
    with world.acting_as(world.ctx(operator)):
        r = client.get("/v1/templates/workflows", params={"project_id": world.project_a})
    assert r.status_code == 200, r.text
    assert [(w["workflow_key"], w["default"]) for w in r.json()["workflows"]] == [
        ("fast_fix", True), ("helpdesk_delivery_d1", False)]


def test_apply_through_a_chosen_workflow_and_for_a_new_app(world, operator, client, workflows, monkeypatch):
    started = []
    monkeypatch.setattr(main, "start_delivery_workflow",
                        lambda body, connection_in=None: started.append(body.model_dump()) or {"run": {"id": "wfr_d1"}})
    with world.acting_as(world.ctx(operator)):
        refused = client.post("/v1/templates/hello/apply", json={
            "project_id": world.project_a, "new_app": {"name": "Nope"}, "stack": "node",
            "workflow_key": "research_only"})
        assert refused.status_code == 422 and refused.json()["error"] == "workflow_cannot_build", refused.text
        assert started == []
        ok = client.post("/v1/templates/hello/apply", json={
            "project_id": world.project_a, "new_app": {"name": "HelpDesk D1"}, "stack": "node",
            "workflow_key": "helpdesk_delivery_d1"})
    assert ok.status_code == 201, ok.text
    assert started[0]["workflow_key"] == "helpdesk_delivery_d1"
    # No repository in the input: the build step's `repository` binding resolves blank, which is
    # the project's primary repository (repo_registry.resolve_repository_target, selector None).
    assert "repository" not in started[0]["input"]


def test_build_workflows_and_plan_share_one_rule(engine, world, workflows):
    with engine.begin() as db:
        keys = [w["workflow_key"] for w in apply_service.build_workflows(db, world.project_a)]
        assert "research_only" not in keys
        with pytest.raises(apply_service.TemplateError):
            apply_service.check_build_workflow(db, world.project_a, "research_only")
        apply_service.check_build_workflow(db, world.project_a, "helpdesk_delivery_d1")
