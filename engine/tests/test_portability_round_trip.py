"""PORTABILITY (BOSSAPP-837): a project goes from one install to another and comes back the same.

Two real databases, created for this module and dropped after, stand for two installs. The
source project is configured only through BOSS's own services, exported, imported into the
second install (dry run, then apply), and exported again: the two bundles' sections must match,
the import's fidelity report must be clean, and no secret value may appear in either bundle.
"""
from __future__ import annotations

import json
import os

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import get_settings
from boss_engine.db import ensure_schema
from boss_engine.services import (agents, deploy_policy, mcp_registry, portability,
                                  project_settings, skill_definitions, tool_policies, workflows)
from boss_engine.services.specpack import SpecPackService

SECRET = "sk-portability-NEVER-LEAVES-9f3c7d21"


SOURCE_DB, TARGET_DB = "boss_test_p17_src", "boss_test_p17_dst"


def _fresh_databases():
    """Two empty databases on the test server, one per 'install', made with psycopg."""
    import psycopg
    from sqlalchemy.engine import make_url

    url = os.environ.get("BOSS_TEST_DATABASE_URL", "")
    name = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if not url or "test" not in name:
        pytest.skip("BOSS_TEST_DATABASE_URL must name a disposable boss_test* database")
    base = make_url(url)
    admin = base.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False)
    try:
        with psycopg.connect(admin, autocommit=True) as c:
            for db in (SOURCE_DB, TARGET_DB):
                c.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
                c.execute(f'CREATE DATABASE "{db}"')
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot create the test databases: {type(exc).__name__}")
    engines = [create_engine(base.set(database=db), future=True) for db in (SOURCE_DB, TARGET_DB)]
    for eng in engines:
        ensure_schema(eng)
    return admin, engines


def _drop_databases(admin: str) -> None:
    import psycopg

    with psycopg.connect(admin, autocommit=True) as c:
        for db in (SOURCE_DB, TARGET_DB):
            c.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')


def _project(engine, org: str, project: str, slug: str = "alpha") -> None:
    with engine.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, :o) ON CONFLICT DO NOTHING"),
                  {"o": org})
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, :w) "
                       "ON CONFLICT DO NOTHING"), {"w": f"ws_{org}", "o": org})
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                       "(:p, :w, :p, :s) ON CONFLICT DO NOTHING"),
                  {"p": project, "w": f"ws_{org}", "s": slug})


def _spec(seconds: int) -> dict:
    return {"entry_step": "hold",
            "steps": [{"id": "hold", "kind": "timer", "wait_seconds": seconds,
                       "binding": {}, "next": []}]}


def _configure(engine, project: str) -> None:
    settings = get_settings()
    with engine.begin() as c:
        project_settings.set_one(c, project, "code_agent_model", "claude-sonnet-x",
                                 actor="op", scope="project")
        svc = SpecPackService(settings)
        svc.register_role(c, project, "lead", display_name="Lead", autonomy_level=2,
                          spec={"purpose": "decides"}, actor="op")
        svc.register_role(c, project, "builder", display_name="Builder", autonomy_level=1,
                          reports_to="lead", spec={"purpose": "builds"}, actor="op")
        skill_definitions.save_skill(c, project_id=project, actor="op", skill_key="house_style",
                                     label="House style", behaviour_kind="guide",
                                     summary="How we write", knowledge="Short sentences.",
                                     mode="create")
        mcp_registry.register_mcp_server(
            c, project, "docs", "https://docs.example.invalid/mcp", actor="op",
            settings=settings, risk_tier=1, description="Docs search",
            auth={"type": "bearer", "secret": SECRET})
        skill_definitions.assign_skill_to_role(c, project_id=project, actor="op",
                                               role_key="builder", skill_id="house_style")
        agents.grant_mcp(c, project, actor="op", role_key="builder", mcp_server_id="docs",
                         tools=["search"])
        made = workflows.create_definition(c, project_id=project, workflow_key="nightly_hold",
                                           name="Nightly hold", description="waits",
                                           actor="op", namespace=workflows.PRODUCT_NAMESPACE)
        workflows.publish_version(c, project_id=project, definition_id=made["id"],
                                  spec=_spec(90), actor="op")
        deploy_policy.set_approval_policy(c, project_id=project, environment="staging",
                                          requires_approval=False, actor="op")
        server = mcp_registry.find_mcp_server(c, project, "docs")
        tool_policies.grant_always_allow(c, project_id=project, server_id=server["id"],
                                         tool="search", schema_sha256="abc123",
                                         source_decision_id=None)


@pytest.fixture(scope="module")
def installs():
    admin, (a, b) = _fresh_databases()
    _project(a, "org_src", "proj_src")
    _project(b, "org_dst", "proj_dst")
    _configure(a, "proj_src")
    yield a, b
    a.dispose()
    b.dispose()
    _drop_databases(admin)


def _export(engine, project: str) -> dict:
    with engine.begin() as c:
        return portability.export_bundle(c, get_settings(), scope="project", target_id=project,
                                         actor="op")


def test_export_is_versioned_and_never_carries_a_secret_value(installs):
    a, _ = installs
    bundle = _export(a, "proj_src")
    dumped = json.dumps(bundle)
    assert SECRET not in dumped and SECRET[-4:] not in dumped
    assert bundle["format"] == portability.FORMAT and bundle["schema_version"] == 1
    manifest = bundle["manifest"]
    assert manifest["scope"] == "project" and manifest["source"]["install_id"]
    assert manifest["checksum"] == portability.checksum(bundle)
    # The secret is a typed, empty placeholder: kind and name, never a value.
    (secret,) = bundle["secrets"]
    assert secret == {"kind": "api_token", "name": "mcp_docs_token", "scope_type": "project",
                      "value": None, "placeholder": True}
    (server,) = bundle["projects"][0]["sections"]["mcp_servers"]
    assert server["auth"]["type"] == "bearer"
    assert server["auth"]["secret"][portability.PLACEHOLDER_KEY]["value"] is None
    # The fidelity report says what could not travel, and why.
    not_exported = {e["what"]: e for e in bundle["fidelity"]["not_exported"]}
    assert not_exported["secret values"]["count"] == 1
    assert bundle["fidelity"]["exported"]["roles"] == 2


def test_the_whole_database_text_of_an_export_has_no_secret(installs):
    """Grep the bundle for every secret-ish thing the source install holds about the token."""
    a, _ = installs
    dumped = json.dumps(_export(a, "proj_src"))
    with a.begin() as c:
        refs = c.execute(text("SELECT id, ref_key FROM secret_refs WHERE project_id='proj_src'")).all()
    for ref_id, ref_key in refs:
        assert ref_id not in dumped and ref_key not in dumped


def test_a_bundle_with_a_secret_value_is_refused():
    with pytest.raises(portability.PortabilityError, match="secret-shaped"):
        portability.assert_no_secret_values({"projects": [{"sections": {"x": {"api_key": "v"}}}]})


def test_round_trip_dry_run_then_apply_then_export_matches(installs):
    a, b = installs
    source = _export(a, "proj_src")
    plan = portability.plan_import(b, get_settings(), scope="project", target_id="proj_dst",
                                   bundle=source, actor="op")
    assert plan["errors"] == [], plan["errors"]
    assert plan["fidelity"]["clean"] is True
    assert plan["summary"]["roles"] == {"create": 2}
    assert plan["summary"]["workflows"] == {"create": 1}
    assert [s["name"] for s in plan["secrets_to_provide"]] == ["mcp_docs_token"]
    # A dry run writes nothing.
    with b.begin() as c:
        assert c.execute(text("SELECT count(*) FROM roles WHERE project_id='proj_dst'")).scalar() == 0

    done = portability.apply_import(b, get_settings(), import_id=plan["import_id"], actor="op")
    assert done["status"] == "applied", done["report"]["errors"]
    assert done["report"]["fidelity"]["clean"] is True

    back = _export(b, "proj_dst")
    assert back["projects"][0]["sections"] == source["projects"][0]["sections"]
    assert SECRET not in json.dumps(back)

    again = portability.plan_import(b, get_settings(), scope="project", target_id="proj_dst",
                                    bundle=source, actor="op")
    actions = {a for counts in again["summary"].values() for a in counts}
    assert actions == {"unchanged"}, again["summary"]


def test_an_apply_resumes_from_its_last_chunk(installs):
    a, b = installs
    _project(b, "org_dst", "proj_resume", slug="resume")
    source = _export(a, "proj_src")
    plan = portability.plan_import(b, get_settings(), scope="project", target_id="proj_resume",
                                   bundle=source, actor="op", chunk_size=3)
    assert plan["chunks_total"] > 2
    first = portability.apply_import(b, get_settings(), import_id=plan["import_id"], actor="op",
                                     max_chunks=1)
    assert first["status"] == "applying" and first["chunks_done"] == 1
    with b.begin() as c:
        row = portability.get_import(c, plan["import_id"])
    assert row["chunks_done"] == 1 and row["status"] == "applying"
    rest = portability.apply_import(b, get_settings(), import_id=plan["import_id"], actor="op")
    assert rest["status"] == "applied", rest["report"]["errors"]
    assert rest["chunks_done"] == plan["chunks_total"]
    back = _export(b, "proj_resume")
    assert back["projects"][0]["sections"] == source["projects"][0]["sections"]


def test_an_org_bundle_matches_projects_by_slug(installs):
    a, b = installs
    with a.begin() as c:
        bundle = portability.export_bundle(c, get_settings(), scope="org", target_id="org_src",
                                           actor="op")
    assert bundle["manifest"]["scope"] == "org" and bundle["org"] is not None
    plan = portability.plan_import(b, get_settings(), scope="org", target_id="org_dst",
                                   bundle=bundle, actor="op")
    assert plan["errors"] == []
    assert [p["project_id"] for p in plan["plan"]["projects"]] == ["proj_dst"]


def test_a_newer_or_edited_bundle_is_refused(installs):
    a, b = installs
    bundle = _export(a, "proj_src")
    newer = {**bundle, "schema_version": 99}
    with pytest.raises(portability.PortabilityError, match="schema version"):
        portability.plan_import(b, get_settings(), scope="project", target_id="proj_dst",
                                bundle=newer, actor="op")
    edited = json.loads(json.dumps(bundle))
    edited["projects"][0]["sections"]["roles"][0]["display_name"] = "Tampered"
    with pytest.raises(portability.PortabilityError, match="checksum"):
        portability.plan_import(b, get_settings(), scope="project", target_id="proj_dst",
                                bundle=edited, actor="op")
