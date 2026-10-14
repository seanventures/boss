"""The MCP / chat tools for app templates and the security loop (tools/app_platform_tools.py), on a
real database under enforce, with REAL role bindings deciding (no faked authz.check).

Templates: the gallery and one template (template:read), an app's templates (project:read), and
apply (workflow_run:start AND template:read, the route's two keys) starting a REAL governed
workflow run onto the app with the template pinned. Security: a scan requested through the existing
run_security_scan tool and executed with the tools' REAL recorded output, then read back through
security_scan_status, list_security_findings, security_report and security_review_status. Refusals:
a viewer cannot apply or scan, a caller without template:read cannot apply even holding
workflow_run:start, and another project's scan or run names nothing.
"""
from __future__ import annotations

import base64
import json
import uuid

import pytest
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.test_security_scan_routes import _app as _scan_app, _results
from boss_engine.config import get_settings
from boss_engine.context import EngineContext
from boss_engine.identity import vault
from boss_engine.security_scan import service as scan_service
from boss_engine.services import toolchains, workflow_catalogue
from boss_engine.tools import build_default_registry

TOOLS = {"semgrep": "1.180.0", "semgrep_rules": "sha256:" + "a" * 64, "osv_scanner": "2.6.0", "zap": "2.17.0"}


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


@pytest.fixture(scope="module")
def reg():
    return build_default_registry()


@pytest.fixture
def ctx(engine):
    return EngineContext(settings=get_settings(), engine=engine)


@pytest.fixture(autouse=True)
def enforce_and_keys(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "authz_mode", "enforce")
    monkeypatch.setattr(settings, "authz_router_modes", "")
    monkeypatch.setenv("BOSS_MASTER_KEY", base64.b64encode(b"t" * 32).decode())
    monkeypatch.delenv("BOSS_MASTER_KEY_FILE", raising=False)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"java": {"jdk": "x"}, "security": TOOLS}))
    monkeypatch.setenv(toolchains.MANIFEST_ENV, str(manifest))
    vault.reset_master_key_cache()
    yield
    vault.reset_master_key_cache()


def _person(world, role, project=None, *, org=False):
    """A person holding `role` in one project, or org-wide (`org=True`): template:read is an ORG
    permission, so only an org-wide binding reads the gallery (as over HTTP)."""
    p = world.person(f"mcp-{role}-{uuid.uuid4().hex[:6]}")
    world.bind(p, role, project=None if org else (project or world.project_a))
    return p


def _call(reg, ctx, world, person, tool_name, project=None, **args):
    return reg.invoke(tool_name, ctx, project_id=project or world.project_a, actor=person.id, args=args,
                      auth=world.ctx(person))


def _node_app(engine, project: str) -> str:
    """A succeeded build of a Node app, so apply has something to build onto."""
    run_id = f"crun_{uuid.uuid4().hex[:10]}"
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state, finalize_ctx) "
            "VALUES (:id, :p, 'completed', 'done', 'Notes', '{}'::jsonb, CAST(:s AS JSONB), '{}'::jsonb)"),
            {"id": run_id, "p": project,
             "s": json.dumps({"impl_files": ["web/package.json", "web/server.js"], "test_files": []})})
    return run_id


# ---- templates ----------------------------------------------------------------------------------

def test_every_role_reads_the_gallery_and_one_template(reg, ctx, world):
    for role in ("owner", "operator", "viewer", "auditor"):
        who = _person(world, role, org=True)
        cards = _call(reg, ctx, world, who, "list_templates")
        assert cards["ok"] is True, cards
        keys = {c["template_key"] for c in cards["templates"]}
        assert {"hello", "login-oauth", "stripe-checkout"} <= keys
    one = _call(reg, ctx, world, _person(world, "viewer", org=True), "describe_template", template_id="hello")
    assert one["template"]["template_key"] == "hello" and one["template"]["manifest"]["id"] == "hello"
    assert "untrusted_text" not in one["template"]            # a built-in is BOSS's own text
    assert _call(reg, ctx, world, _person(world, "viewer", org=True), "list_templates", stack="java")["templates"]
    missing = _call(reg, ctx, world, _person(world, "viewer", org=True), "describe_template", template_id="no-such")
    assert missing["ok"] is False and missing["error"] == "template_not_found"


def test_no_binding_is_refused_before_the_handler(reg, ctx, world):
    nobody = world.person(f"mcp-nobody-{uuid.uuid4().hex[:6]}")
    out = _call(reg, ctx, world, nobody, "list_templates")
    assert out["ok"] is False and out["permission"] == "template:read"


def test_apply_starts_a_governed_build_with_the_template_pinned(reg, ctx, world, engine):
    with engine.begin() as c:
        workflow_catalogue.seed(c, world.project_a)       # the project's workflows, fast_fix among them
    run_id = _node_app(engine, world.project_a)
    op = _person(world, "operator", org=True)
    out = _call(reg, ctx, world, op, "apply_template", template_id="hello", stack="node",
                extends_run_id=run_id, note="use the router")
    if out.get("missing"):                       # the project lacks something; the operator said go
        out = _call(reg, ctx, world, op, "apply_template", template_id="hello", stack="node",
                    extends_run_id=run_id, note="use the router",
                    start_anyway=[m["key"] for m in out["missing"]])
    assert out["ok"] is True and out["status"] == "building", out
    assert out["application_id"].startswith("tap_") and out["workflow_key"] == "fast_fix"
    with engine.begin() as c:
        run = c.execute(text("SELECT project_id, input FROM workflow_runs WHERE id = :r"),
                        {"r": out["workflow_run_id"]}).mappings().one()
    assert run["project_id"] == world.project_a
    assert run["input"]["extends_run_id"] == run_id
    assert run["input"]["template"]["key"] == "hello" and run["input"]["template"]["stack"] == "node"
    assert run["input"]["template"]["digest"].startswith("sha256:")
    assert "use the router" in run["input"]["directive"]
    status = _call(reg, ctx, world, _person(world, "viewer", org=True), "template_status", app_key=out["app_key"])
    assert [(t["template_key"], t["status"], t["workflow_run_id"]) for t in status["templates"]] == [
        ("hello", "building", out["workflow_run_id"])]


def test_apply_refusals_say_what_is_missing(reg, ctx, world, engine):
    run_id = _node_app(engine, world.project_a)
    op = _person(world, "operator", org=True)
    wrong = _call(reg, ctx, world, op, "apply_template", template_id="hello", stack="java", extends_run_id=run_id)
    assert wrong["ok"] is False and wrong["error"] == "stack_not_supported"
    none = _call(reg, ctx, world, op, "apply_template", template_id="hello", stack="node")
    assert none["ok"] is False and none["error"] == "app_required"


def test_apply_needs_workflow_run_start_and_template_read(reg, ctx, world, engine):
    run_id = _node_app(engine, world.project_a)
    viewer = _person(world, "viewer", org=True)
    out = _call(reg, ctx, world, viewer, "apply_template", template_id="hello", stack="node", extends_run_id=run_id)
    assert out["ok"] is False and out["permission"] == "workflow_run:start"
    # workflow_run:start alone (a custom role) is not enough: the route also needs template:read
    role = world.custom_role(f"starter_{uuid.uuid4().hex[:6]}", ("workflow_run:start", "project:read"))
    starter = world.person(f"mcp-starter-{uuid.uuid4().hex[:6]}")
    world.bind(starter, role)
    out = _call(reg, ctx, world, starter, "apply_template", template_id="hello", stack="node", extends_run_id=run_id)
    assert out["ok"] is False and out["permission"] == "template:read", out
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM app_template_applications WHERE project_id = :p "
                              "AND created_by = :b"), {"p": world.project_a, "b": starter.id}).scalar() == 0


def test_apply_cannot_build_onto_another_projects_app(reg, ctx, world, engine):
    theirs = _node_app(engine, world.project_b)
    op = _person(world, "operator", org=True)
    out = _call(reg, ctx, world, op, "apply_template", template_id="hello", stack="node", extends_run_id=theirs)
    assert out["ok"] is False and out["error"] == "not_found", out


# ---- several connectors in one build (BOSSAPP-526) -----------------------------------------------

def _compose(reg, ctx, world, person, **args):
    """apply_template with `templates`; the project's missing readiness accepted as the operator."""
    out = _call(reg, ctx, world, person, "apply_template", **args)
    if out.get("missing"):
        out = _call(reg, ctx, world, person, "apply_template", **args,
                    start_anyway=[m["key"] for m in out["missing"]])
    return out


def test_apply_template_adds_several_connectors_in_one_build(reg, ctx, world, engine):
    from boss_engine.templates import compose

    with engine.begin() as c:
        workflow_catalogue.seed(c, world.project_a)
    op = _person(world, "operator", org=True)
    name = f"Shop {uuid.uuid4().hex[:6]}"
    out = _compose(reg, ctx, world, op, templates=["transactional-email", "login-oauth"], stack="node",
                   new_app_name=name, accept_egress=True, note="one login for both")
    assert out["ok"] is True and out["status"] == "building", out
    assert out["order"] == ["transactional-email", "login-oauth"]
    assert "accounts.google.com" in out["egress"] and "api.resend.com" in out["egress"]
    wfr = out["workflow_run_id"]
    with engine.begin() as c:
        runs = c.execute(text("SELECT id, input FROM workflow_runs WHERE id = :r"), {"r": wfr}).mappings().all()
        rows = c.execute(text("SELECT template_id, status FROM app_template_applications WHERE workflow_run_id = :w"),
                         {"w": wfr}).all()
        receipts = c.execute(text("SELECT count(*) FROM receipts WHERE effect_kind = :k AND dedupe_key LIKE :d"),
                             {"k": compose.RECEIPT_KIND, "d": f"template.compose:{wfr}:%"}).scalar()
    assert len(runs) == 1                                               # ONE build
    tpl = runs[0]["input"]["template"]
    assert tpl["key"].startswith("compose-") and [m["key"] for m in tpl["connectors"]] == out["order"]
    assert "one login for both" in runs[0]["input"]["directive"]
    assert len(rows) == 2 and {r[1] for r in rows} == {"building"} and receipts == 2
    assert [c["template_key"] for c in out["connectors"]] == out["order"]
    status = _call(reg, ctx, world, _person(world, "viewer", org=True), "template_status", app_key=out["app_key"])
    assert sorted((t["template_key"], t["workflow_run_id"]) for t in status["templates"]) == [
        ("login-oauth", wfr), ("transactional-email", wfr)]


def test_several_connectors_that_conflict_are_refused_whole_and_nothing_is_written(reg, ctx, world, engine):
    with engine.begin() as c:
        workflow_catalogue.seed(c, world.project_a)
        before = c.execute(text("SELECT count(*) FROM app_template_applications WHERE project_id = :p"),
                           {"p": world.project_a}).scalar()
    op = _person(world, "operator", org=True)
    out = _call(reg, ctx, world, op, "apply_template", templates=["stripe-checkout", "paypal-checkout"], stack="node",
                new_app_name=f"Pay {uuid.uuid4().hex[:6]}", accept_egress=True)
    assert out["ok"] is False and out["error"] == "connector_conflict", out
    whats = [c["what"] for c in out["conflicts"]]
    assert any("/api/orders/" in w for w in whats) and any("payments/" in w for w in whats)
    assert all(set(c["connectors"]) == {"stripe-checkout", "paypal-checkout"} for c in out["conflicts"])
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM app_template_applications WHERE project_id = :p"),
                         {"p": world.project_a}).scalar() == before


def test_several_connectors_refusals_say_what_is_missing(reg, ctx, world, engine):
    run_id = _node_app(engine, world.project_a)
    op = _person(world, "operator", org=True)
    both = _call(reg, ctx, world, op, "apply_template", template_id="hello", templates=["login-oauth", "stripe-checkout"],
                 stack="node", extends_run_id=run_id)
    assert both["ok"] is False and both["error"] == "template_ambiguous"
    none = _call(reg, ctx, world, op, "apply_template", stack="node", extends_run_id=run_id)
    assert none["ok"] is False and none["error"] == "template_required"
    idea = _call(reg, ctx, world, op, "apply_template", templates=["login-oauth", "hello"], stack="node",
                 extends_run_id=run_id)
    assert idea["ok"] is False and idea["error"] == "compose_connectors_only", idea
    twice = _call(reg, ctx, world, op, "apply_template", templates=["login-oauth", "login-oauth"], stack="node",
                  extends_run_id=run_id)
    assert twice["ok"] is False and twice["error"] == "compose_duplicate", twice
    hosts = _call(reg, ctx, world, op, "apply_template", templates=["login-oauth", "transactional-email"],
                  stack="node", extends_run_id=run_id)
    assert hosts["ok"] is False and hosts["error"] == "egress_not_accepted" and "api.resend.com" in hosts["egress"]
    # the same two keys as one template: a viewer cannot, and the identity gate says which key
    viewer = _person(world, "viewer", org=True)
    out = _call(reg, ctx, world, viewer, "apply_template", templates=["login-oauth", "transactional-email"],
                stack="node", extends_run_id=run_id, accept_egress=True)
    assert out["ok"] is False and out["permission"] == "workflow_run:start"


def test_one_template_in_templates_is_the_single_apply(reg, ctx, world, engine):
    with engine.begin() as c:
        workflow_catalogue.seed(c, world.project_a)
    run_id = _node_app(engine, world.project_a)
    op = _person(world, "operator", org=True)
    out = _compose(reg, ctx, world, op, templates=["hello"], stack="node", extends_run_id=run_id)
    assert out["ok"] is True and out["status"] == "building", out
    assert out["application_id"].startswith("tap_") and "order" not in out
    with engine.begin() as c:
        tpl = c.execute(text("SELECT input FROM workflow_runs WHERE id = :r"),
                        {"r": out["workflow_run_id"]}).scalar()["template"]
    assert tpl["key"] == "hello"


# ---- security -----------------------------------------------------------------------------------

def _scan(reg, ctx, world, engine, app_key, which):
    op = _person(world, "operator")
    queued = _call(reg, ctx, world, op, "run_security_scan", app_key=app_key)
    assert queued["status"] == "queued", queued
    results, files = _results(which)
    done = scan_service.execute_scan(engine, main.settings, queued["scan_id"],
                                     do_checkout=lambda *a: ("c0ffee" if which == "before" else "beefed") * 6 + "abcd",
                                     run_tools=lambda *a: (results, files))
    assert done["ok"] is True
    return queued["scan_id"]


def test_scan_status_findings_and_report_through_the_tools(reg, ctx, world, engine):
    app_key = f"app-{uuid.uuid4().hex[:8]}"
    _scan_app(engine, world.project_a, app_key)
    before = _scan(reg, ctx, world, engine, app_key, "before")
    after = _scan(reg, ctx, world, engine, app_key, "after")
    viewer = _person(world, "viewer")
    one = _call(reg, ctx, world, viewer, "security_scan_status", scan_id=before)
    assert one["scan"]["status"] == "done" and one["scan"]["counts"]["by_tool"]["semgrep"]["high"] == 1
    listed = _call(reg, ctx, world, viewer, "security_scan_status", app_key=app_key)
    assert [s["id"] for s in listed["scans"]][:2] == [after, before]
    found = _call(reg, ctx, world, viewer, "list_security_findings", scan_id=before, tool="osv")
    assert found["count"] == 3 and {f["tool"] for f in found["findings"]} == {"osv"}
    assert "s4-proof-hardcoded-signing-key" not in json.dumps(
        _call(reg, ctx, world, viewer, "list_security_findings", scan_id=before))
    rep = _call(reg, ctx, world, viewer, "security_report", app_key=app_key)["report"]
    assert (rep["before"]["id"], rep["after"]["id"]) == (before, after) and rep["new"] == []
    assert ("osv", "GHSA-35jh-r3h4-6jhm") in {(f["tool"], f["rule_id"]) for f in rep["fixed"]}
    md = _call(reg, ctx, world, viewer, "security_report", app_key=app_key, format="md")
    assert "## Fixed" in md["markdown"]
    assert _call(reg, ctx, world, viewer, "security_review_status", app_key=app_key)["reviews"] == []


def test_a_viewer_cannot_scan_and_another_projects_scan_names_nothing(reg, ctx, world, engine):
    app_key = f"app-{uuid.uuid4().hex[:8]}"
    _scan_app(engine, world.project_a, app_key)
    viewer = _person(world, "viewer")
    refused = _call(reg, ctx, world, viewer, "run_security_scan", app_key=app_key)
    assert refused["ok"] is False and refused["permission"] == "security_scan:run"
    scan_id = _scan(reg, ctx, world, engine, app_key, "before")
    other = _person(world, "owner", project=world.project_b)
    for tool, args in (("security_scan_status", {"scan_id": scan_id}),
                       ("list_security_findings", {"scan_id": scan_id})):
        out = _call(reg, ctx, world, other, tool, project=world.project_b, **args)
        assert out["ok"] is False and out["error"] == "not_found", (tool, out)
    # and naming project A outright: B's owner holds nothing there
    out = _call(reg, ctx, world, other, "security_report", project=world.project_a, app_key=app_key)
    assert out["ok"] is False and out["permission"] == "project:read"
