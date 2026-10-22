"""Every BOSS agent knows BOSS's fundamentals and is never blind (services/agent_baseline.py).

The audit (2026-10-13, laptop install): of 32 agents, none held a way to read a run's log,
diagnose a run, read BOSS's health, read the schema or a row, list what BOSS can do or propose a
fix. Harbor held `deployment.health.check` and `deployment.run.plan`; the panel voices held a text
model and had an EMPTY Passport ceiling. An agent step's prompt said "no shell, no database".

What this enforces:
  * the baseline is granted to EVERY role — shipped, panel voice, or made in the console;
  * a Harbor turn is OFFERED the baseline tools by name;
  * every tool in it is a declared READ, and its results are redacted;
  * the DB read is fenced to the project, SELECT-only by construction, capped;
  * every Passport preset can hold the baseline reads, and an agent with no preset gets them;
  * a proposed fix runs only after a person approves it, as that person;
  * every agent's system context starts with the same static primer.
"""
from __future__ import annotations

import inspect
import os
import pathlib
import uuid

import pytest
import yaml
from sqlalchemy import create_engine, text

from boss_engine.services import agent_baseline as ab

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPECPACK = ROOT / "packages" / "specpack"
DATABASE_URL = os.environ.get("DATABASE_URL")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="no database configured")
PROJECT = "boss-dogfood"


def _kit_skills() -> dict[str, dict]:
    data = yaml.safe_load((SPECPACK / "skills" / "kit.yaml").read_text())
    return {str(s["id"]): s for s in data["skills"]}


def _registry():
    from boss_engine.tools.builtin import build_discuss_read_registry

    return build_discuss_read_registry(edition="community")


# ── the skills ────────────────────────────────────────────────────────────────────────────────

def test_the_baseline_skills_ship_in_the_community_kit():
    kit = _kit_skills()
    pack = yaml.safe_load((SPECPACK / "packs" / "community_edition.yaml").read_text())
    assert "kit" in pack["installs"]["skills"]
    for skill in ab.BASELINE_SKILLS:
        assert skill in kit, skill
        assert int(kit[skill]["risk_tier"]) == 0, skill


def test_every_agent_holds_the_knowledge_graph():
    """BOSSAPP-622: knowledge.read is a fundamental. Every role can ask where code is, who depends
    on it, what a change breaks and why it is so; every tool it offers is a read."""
    assert "knowledge.read" in ab.BASELINE_SKILLS
    assert ab.bundles()["knowledge.read"] == ("kg_symbol", "kg_callers", "kg_blast_radius", "kg_why",
                                              "kg_search", "kg_precedent")
    reg = _registry()
    assert all(reg.get(t).kind == "read" for t in ab.KNOWLEDGE_TOOLS)


def test_kg_propose_fact_is_granted_to_exactly_the_six_roles():
    """Operator decision 2026-10-21: pm, engineering_captain, qa, adversarial_reviewer,
    security_reviewer and sre hold knowledge.propose; no other role, and not the baseline."""
    assert "knowledge.propose" not in ab.BASELINE_SKILLS
    holders = sorted(p.stem for p in (SPECPACK / "roles").glob("*.yaml")
                     if "knowledge.propose" in (((yaml.safe_load(p.read_text()) or {}).get("permissions") or {})
                                                .get("allowed_skills") or []))
    assert holders == sorted(ab.PROPOSE_FACT_ROLES)
    assert ab.bundles()["knowledge.propose"] == ("kg_propose_fact",)
    spec = ab.bundle_tool_spec("knowledge.propose", "kg_propose_fact")
    assert spec is not None and spec.kind == "write" and spec.reversibility == "safe"
    assert all("kg_propose_fact" not in tools for skill, tools in ab.bundles().items()
               if skill != "knowledge.propose")                               # no other bundle offers it
    from boss_engine.services.skill_executors import registered_executor_ids

    assert "knowledge.propose" in set(registered_executor_ids())


def test_each_baseline_skill_has_an_executor():
    from boss_engine.services.skill_executors import registered_executor_ids

    assert set(ab.BASELINE_SKILLS) <= set(registered_executor_ids())


def test_the_spec_lists_exactly_the_tools_the_bundle_offers():
    kit = _kit_skills()
    for skill, tools in ab.bundles().items():
        assert list(kit[skill]["policy"]["tools"]) == list(tools), skill


def test_the_grant_is_role_skills_never_allowed_callers():
    """Giving a skill to an agent is the grant. The baseline widens nobody's allowed_callers."""
    kit = _kit_skills()
    for skill in ab.BASELINE_SKILLS:
        assert kit[skill]["permissions"]["allowed_callers"] == ["kit_try"], skill


def test_every_fundamental_is_a_registered_read_tool():
    reg = _registry()
    missing = [t for t in ab.FUNDAMENTALS_TOOLS if t not in reg.names() and t not in ab.OPTIONAL_TOOLS]
    assert not missing, f"named but not registered: {missing}"
    writes = [t for skill, tools in ab.bundles().items() if skill not in ab.WRITE_BUNDLES for t in tools
              if t in reg.names() and reg.get(t).kind != "read"]
    assert not writes, f"a baseline bundle may offer reads only: {writes}"


def test_the_fundamentals_cover_every_area_an_agent_needs():
    have = set(ab.FUNDAMENTALS_TOOLS) | set(ab.bundles()["repo.read"]) | set(ab.PROPOSE_TOOLS)
    for area, tools in {
        "logs": {"read_logs", "get_run_activity", "read_preview_log"},
        "diagnose": {"diagnose_run", "explain_run", "read_run_turns"},
        # micro-VMs are not part of the community release (BOSSAPP-516 dropped), so no
        # microvm_status: every agent would be offered a tool for something that never runs.
        "infra": {"inspect_self", "diagnose_setup", "list_running_work",
                  "consistency_report", "describe_deployment_target", "describe_deployment_host",
                  "list_deployments"},
        "code": {"read_code", "search_code", "git_history", "list_source", "read_pull_request"},
        "db": {"describe_schema", "query_project_data"},
        "catalogue": {"list_capabilities", "list_skills", "list_harnesses", "describe_workflow"},
        "precheck": {"precheck_workflow"},
        "memory": {"recall"},
        "fix": {"propose_action"},
    }.items():
        assert tools <= have, (area, tools - have)


# ── every role holds it ───────────────────────────────────────────────────────────────────────

def test_the_sync_grants_the_baseline_to_every_role_whatever_its_spec_names():
    from boss_engine.services import skill_definitions as sd

    source = inspect.getsource(sd.sync_role_skills)
    assert "BASELINE_SKILLS" in source
    # still additive, and still respects an operator's removal
    assert "seed_exclusions.role_skill_subject" in source


def test_an_agent_made_in_the_console_is_born_holding_the_baseline():
    from boss_engine.services import agents

    source = inspect.getsource(agents.create_agent)
    assert "BASELINE_SKILLS" in source


@needs_db
def test_every_role_holds_the_baseline_after_a_sync():
    """EXERCISED on the database, in a transaction that is rolled back: every agent on the
    project, and an agent created just now with an empty spec, holds every baseline skill."""
    from boss_engine.services import seed_exclusions
    from boss_engine.services import skill_definitions as sd

    from boss_engine.db import ensure_schema

    engine = create_engine(DATABASE_URL)
    ensure_schema(engine)                 # idempotent: a fresh test database has no tables yet
    with engine.connect() as c:
        tx = c.begin()
        try:
            # ITS OWN PROJECT, in the transaction that is rolled back: the test must not depend
            # on a project another test (or an operator's install) happened to leave behind.
            project = "p_baseline_" + uuid.uuid4().hex[:8]
            c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_baseline_test', 't') "
                           "ON CONFLICT DO NOTHING"))
            c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES "
                           "('ws_baseline_test', 'org_baseline_test', 't') ON CONFLICT DO NOTHING"))
            c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                           "VALUES (:p, 'ws_baseline_test', 't', :p)"), {"p": project})
            key = "baseline_probe_" + uuid.uuid4().hex[:6]
            shipped = "baseline_shipped_" + uuid.uuid4().hex[:6]
            for k, spec in ((key, "{}"),
                            # a role whose spec names skills of its own: the baseline comes on top
                            (shipped, '{"permissions": {"allowed_skills": ["repo.read"]}}')):
                c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name, "
                               "autonomy_level, spec) VALUES (:i, :p, :k, 'Probe', 0, "
                               "CAST(:s AS jsonb))"),
                          {"i": "role_" + uuid.uuid4().hex[:12], "p": project, "k": k, "s": spec})
            sd.sync_role_skills(c, project_id=project, actor="test")
            held: dict[str, set[str]] = {}
            for r in c.execute(text("SELECT role_key, skill_id FROM role_skills WHERE project_id = :p"),
                               {"p": project}).mappings():
                held.setdefault(r["role_key"], set()).add(r["skill_id"])
            off = seed_exclusions.excluded(c, project, "role_skill")
            roles = c.execute(text("SELECT DISTINCT role_key FROM roles WHERE project_id = :p"),
                              {"p": project}).scalars().all()
            assert key in roles and shipped in roles
            lacking = {role: sorted(s for s in ab.BASELINE_SKILLS
                                    if s not in held.get(role, set())
                                    and seed_exclusions.role_skill_subject(role, s) not in off)
                       for role in roles}
            lacking = {r: s for r, s in lacking.items() if s}
            assert not lacking, f"roles without the baseline: {lacking}"
        finally:
            tx.rollback()


# ── a Harbor turn is offered it ───────────────────────────────────────────────────────────────

def test_a_harbor_turn_is_offered_the_baseline_tools_by_name():
    """Harbor (deployment_manager) holds its role skills plus the baseline; the run's tool list
    offers every baseline tool under its own name, each a call of its one bundle skill."""
    from boss_engine.services.agent_loop import _append_bundle_tools, _append_skill_tools

    kit = _kit_skills()
    role = yaml.safe_load((SPECPACK / "roles" / "deployment_manager.yaml").read_text())
    role = role.get("role", role)
    assert (role.get("persona") or {}).get("callsign") == "Harbor"
    held = [{**kit[s], "caller": "deployment_manager"} for s in ab.BASELINE_SKILLS]
    schemas: list[dict] = []
    occupied: set[str] = set()
    offered = _append_bundle_tools(schemas, occupied, held)
    _append_skill_tools(schemas, occupied, [])
    names = {s["function"]["name"] for s in schemas}
    reg = _registry()
    # The baseline bundles only: knowledge.propose is granted to six roles, not to Harbor.
    expected = {t for skill in ab.BASELINE_SKILLS for t in ab.bundles().get(skill, ()) if t in reg.names()}
    assert expected <= names, sorted(expected - names)
    for name, (spec, tool) in offered.items():
        assert tool in ab.bundles()[spec["id"]]
    assert {"read_logs", "diagnose_run", "read_code", "query_project_data",
            "propose_action"} <= names
    propose = next(s["function"] for s in schemas if s["function"]["name"] == "propose_action")
    assert "retry_code_task" in propose["description"], "the agent must see what it may propose"


def test_a_bundled_call_is_checked_against_the_tools_own_permission():
    """Inside a delegated run the Passport decides with the tool's own permission, so a ceiling
    that lacks it refuses it honestly; repo.read keeps the one it always had."""
    from boss_engine.services.agent_loop import _bundle_tool_permission

    reg = _registry()
    assert _bundle_tool_permission("boss.fundamentals", "read_logs") == reg.get("read_logs").permission
    assert _bundle_tool_permission("boss.propose_fix", "propose_action") == "decision:read"
    assert _bundle_tool_permission("repo.read", "read_pull_request") == "repository:read"


# ── the Passport can hold it ──────────────────────────────────────────────────────────────────

def _baseline_permissions() -> set[str]:
    from boss_engine.identity import catalogue

    reg = _registry()
    perms = {reg.get(t).permission for tools in ab.bundles().values() for t in tools
             if t in reg.names()}
    return {p for p in perms if catalogue.meta(p).delegable}


def test_every_passport_preset_holds_the_delegable_baseline_reads():
    from boss_engine.identity.permissions_gen import PASSPORT_PRESETS

    need = _baseline_permissions()
    for preset in PASSPORT_PRESETS.values():
        assert need <= set(preset.permissions), (preset.key, sorted(need - set(preset.permissions)))


def test_the_baseline_preset_is_reads_only():
    from boss_engine.identity.permissions_gen import PASSPORT_PRESETS

    assert all(p.endswith(":read") for p in PASSPORT_PRESETS["baseline"].permissions)


def test_every_shipped_role_and_any_new_agent_gets_a_preset():
    from boss_engine.identity import passports

    for path in sorted((SPECPACK / "roles").glob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        role = data.get("role", data)
        callsign = str((role.get("persona") or {}).get("callsign") or "")
        assert passports.preset_for_role(callsign) or passports.preset_for_role(path.stem), path.stem
    assert "BASELINE_PRESET" in inspect.getsource(passports.upgrade_from_roles)


# ── read-only and redacted ────────────────────────────────────────────────────────────────────

def test_redaction_drops_credentials_and_keeps_code():
    out = ab.redact_for_agent({
        "password": "hunter2", "api_token": "abc", "DEMO_PASSWORD": "x", "secret_ref_id": "sec_1",
        "code": "def f():\n    return 1", "status_code": 500,
        "log": ["connect postgres://boss:s3cretpw@db:5432/boss", "key sk-ant-api03-ABCDEFGHIJKLMNOP"],
        "error": "the app needs a secret: DEMO_PASSWORD (declared by .env.example)",
    })
    assert out["password"] == out["api_token"] == out["DEMO_PASSWORD"] == "[REDACTED]"
    assert out["code"].startswith("def f()") and out["status_code"] == 500
    assert "s3cretpw" not in out["log"][0] and "ABCDEFGHIJKLMNOP" not in out["log"][1]
    # the NAME of a missing secret is the diagnosis; only values are removed
    assert "DEMO_PASSWORD" in out["error"]
    still = ab.redact_for_agent("password: hunter2xyz and secret: Abc_Def9 and token=DEMO_X")
    assert "hunter2xyz" not in still and "Abc_Def9" not in still and "DEMO_X" in still


class _Ctx:
    def __init__(self, engine):
        self.engine = engine
        self.settings = type("S", (), {"boss_edition": "community"})()

    def begin(self):
        return self.engine.begin()


@pytest.fixture()
def sqlite_ctx():
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE notes (id TEXT, project_id TEXT, body TEXT, password TEXT, "
                       "created_at TEXT)"))
        c.execute(text("CREATE TABLE secret_refs (id TEXT, project_id TEXT)"))
        c.execute(text("CREATE TABLE globals (id TEXT)"))
        c.execute(text("INSERT INTO notes VALUES ('n1','p1','mine','pw1','2026-01-02'),"
                       "('n2','p2','theirs','pw2','2026-01-03')"))
    return _Ctx(engine)


def _columns_from_sqlite(monkeypatch, ctx):
    """information_schema does not exist on SQLite: answer the column lookup from PRAGMA."""
    from boss_engine.tools import builtin

    real = builtin._text

    def fake_text(sql):
        if "information_schema.columns" in sql:
            return real("SELECT name FROM pragma_table_info(:t)")
        return real(sql)

    monkeypatch.setattr(builtin, "_text", fake_text)


def test_project_data_is_fenced_to_the_project_and_redacted(monkeypatch, sqlite_ctx):
    from boss_engine.tools.builtin import _query_project_data

    _columns_from_sqlite(monkeypatch, sqlite_ctx)
    out = _query_project_data(sqlite_ctx, "p1", {"table": "notes", "where": {"project_id": "p2"}})
    assert out["row_count"] == 1
    row = out["rows"][0]
    assert row["body"] == "mine" and row["password"] == "[REDACTED]"


def test_project_data_refuses_secret_tables_unfenced_tables_and_unknown_columns(monkeypatch, sqlite_ctx):
    from boss_engine.tools.builtin import _query_project_data

    _columns_from_sqlite(monkeypatch, sqlite_ctx)
    assert not _query_project_data(sqlite_ctx, "p1", {"table": "secret_refs"}).get("rows")
    assert "project_id" in _query_project_data(sqlite_ctx, "p1", {"table": "globals"})["error"]
    assert "unknown column" in _query_project_data(
        sqlite_ctx, "p1", {"table": "notes", "where": {"body; drop table notes": 1}})["error"]
    assert "plain table name" in _query_project_data(sqlite_ctx, "p1", {"table": "notes;--"})["error"]


def test_project_data_is_capped():
    from boss_engine.tools.builtin import PROJECT_DATA_ROW_CAP

    spec = _registry().get("query_project_data")
    assert spec.kind == "read"
    assert spec.parameters["properties"]["limit"]["maximum"] == PROJECT_DATA_ROW_CAP <= 50


def test_raw_query_db_stays_out_of_every_agent_and_is_redacted_where_it_runs():
    from boss_engine.tools import builtin

    assert "query_db" not in _registry().names()
    assert "query_db" not in ab.FUNDAMENTALS_TOOLS
    assert "redact_for_agent" in inspect.getsource(builtin._register_read_tools)


# ── fixes are proposed, a person approves ─────────────────────────────────────────────────────

def test_an_approved_proposal_runs_only_operational_actions_as_the_approver():
    from boss_engine.services import agent_loop

    source = inspect.getsource(agent_loop._execute_proposed_tool)
    assert "_DISCUSS_OPERATIONS_TOOLS" in source and "action_unavailable" in source
    assert "operator_confirmed=True" in source and "actor=actor" in source
    out = agent_loop._execute_proposed_tool(None, "p", None, "drop_everything", {}, "usr_x")
    assert "unknown" in out["error"]


def test_a_proposal_raises_a_hold_decision_and_runs_nothing():
    source = inspect.getsource(ab.propose)
    assert "create_blocking_decision" in source and '"execute_agent_tool"' in source
    assert ".invoke(" not in source


# ── the primer ────────────────────────────────────────────────────────────────────────────────

def test_the_primer_is_static_and_says_where_things_live():
    for text_ in (ab.PRIMER, ab.BUILD_PRIMER):
        assert "{" not in text_ and "}" not in text_
    for word in ("run", "step", "logs", "Postgres", "repository", "propose_action",
                 "diagnose_run", "read_logs", "query_project_data"):
        assert word in ab.PRIMER, word


def test_every_agent_context_starts_with_the_primer():
    from boss_engine.services import agent_steps, governed_code_task_service as g, intent_router

    assert "PRIMER" in inspect.getsource(agent_steps.agent_brief)
    assert "_baseline_primer()" in inspect.getsource(intent_router.IntentRouter)
    block = g._ctx_block("HelpDesk", "Build it.", ["works"])
    assert block.startswith("=== TASK CONTEXT")
    assert block.index(ab.BUILD_PRIMER) < block.index("TASK: HelpDesk")
    assert "no shell, no database" not in inspect.getsource(agent_steps.agent_brief)


def test_proposing_a_retry_raises_one_hold_decision_and_nothing_runs(monkeypatch):
    """EXERCISED: the agent proposes retry_code_task; one hold-default decision is raised whose
    approval runs it (execute_agent_tool); an invented argument or a non-action is refused."""
    from types import SimpleNamespace

    from boss_engine.services import core

    raised: list[tuple] = []
    monkeypatch.setattr(core, "create_blocking_decision",
                        lambda *a, **k: raised.append((a, k)) or "dec_1")
    monkeypatch.setattr(core, "emit_event", lambda *a, **k: None)
    ctx = SimpleNamespace(settings=SimpleNamespace(boss_edition="community"),
                          connection=SimpleNamespace(engine=None), project_id="p1",
                          actor="agent_loop", caller="deployment_manager")
    out = ab.propose(ctx, {"tool": "retry_code_task", "args": {"run_id": "crun_0d82ab62bb6443d4a576"},
                           "label": "Retry the build", "why": "after DEMO_PASSWORD is set"})
    assert out["proposed"] and out["decision_id"] == "dec_1" and out["risk_tier"] >= 2
    (args, kwargs), = raised
    rec = args[4]
    assert rec["action"] == "execute_agent_tool" and rec["tool"] == "retry_code_task"
    assert rec["proposed_by"] == "deployment_manager"
    assert not ab.propose(ctx, {"tool": "drop_database", "label": "x"})["proposed"]
    assert not ab.propose(ctx, {"tool": "retry_code_task", "args": {"run_id": "crun_placeholder"},
                                "label": "x"})["proposed"]
    assert len(raised) == 1


# ── query_project_data, adversarially ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("args", [
    {"table": "notes", "columns": ['body" FROM notes; --']},
    {"table": "notes", "columns": ["body", "id) UNION SELECT password FROM notes --"]},
    {"table": "notes", "order_by": "created_at; DELETE FROM notes"},
    {"table": "notes", "where": {"id = 'x' OR 1=1 --": "n1"}},
    {"table": "NOTES\"; DROP TABLE notes; --"},
])
def test_project_data_refuses_sql_in_every_name_it_accepts(monkeypatch, sqlite_ctx, args):
    """Every identifier is checked against the table's real columns before it is quoted, and
    every value is a bound parameter: SQL in a name is refused, never run."""
    from boss_engine.tools.builtin import _query_project_data

    _columns_from_sqlite(monkeypatch, sqlite_ctx)
    out = _query_project_data(sqlite_ctx, "p1", args)
    assert out.get("ok") is False and "rows" not in out
    with sqlite_ctx.engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM notes")).scalar() == 2


def test_a_value_with_sql_in_it_is_only_ever_a_value(monkeypatch, sqlite_ctx):
    from boss_engine.tools.builtin import _query_project_data

    _columns_from_sqlite(monkeypatch, sqlite_ctx)
    out = _query_project_data(sqlite_ctx, "p1",
                              {"table": "notes", "where": {"id": "n1' OR '1'='1"}})
    assert out["row_count"] == 0
    with sqlite_ctx.engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM notes")).scalar() == 2


def test_the_project_fence_cannot_be_moved_by_any_spelling_of_project_id(monkeypatch, sqlite_ctx):
    from boss_engine.tools.builtin import _query_project_data

    _columns_from_sqlite(monkeypatch, sqlite_ctx)
    for key in ("project_id", "PROJECT_ID", "Project_Id"):
        out = _query_project_data(sqlite_ctx, "p1", {"table": "notes", "where": {key: "p2"}})
        assert [r["body"] for r in out["rows"]] == ["mine"], key


def test_a_limit_a_model_spelled_in_words_is_a_page_not_a_crash(monkeypatch, sqlite_ctx):
    from boss_engine.tools.builtin import _query_project_data

    _columns_from_sqlite(monkeypatch, sqlite_ctx)
    for limit in ("twenty", None, -5, 10_000):
        out = _query_project_data(sqlite_ctx, "p1", {"table": "notes", "limit": limit})
        assert out["row_count"] == 1 and 1 <= out["capped_at"] <= 50, limit


@needs_db
def test_project_data_runs_in_a_read_only_transaction_on_postgres():
    """On Postgres the statement runs inside SET TRANSACTION READ ONLY: even a write that got
    past every check could not commit."""
    from boss_engine.tools.builtin import _query_project_data

    engine = create_engine(DATABASE_URL.replace("postgresql://", "postgresql+psycopg://"))
    table = f"qpd_{uuid.uuid4().hex[:8]}"
    with engine.begin() as c:
        c.execute(text(f"CREATE TABLE {table} (id TEXT, project_id TEXT, created_at TEXT)"))
        c.execute(text(f"INSERT INTO {table} VALUES ('a', 'p1', '1'), ('b', 'p2', '2')"))
    try:
        seen: list[str] = []

        class _PgCtx(_Ctx):
            def begin(self):
                conn_cm = super().begin()

                class _Wrap:
                    def __enter__(_s):
                        conn = conn_cm.__enter__()
                        real = conn.execute

                        def execute(stmt, *a, **kw):
                            seen.append(str(stmt))
                            return real(stmt, *a, **kw)

                        conn.execute = execute
                        return conn

                    def __exit__(_s, *exc):
                        return conn_cm.__exit__(*exc)

                return _Wrap()

        out = _query_project_data(_PgCtx(engine), "p1", {"table": table})
        assert [r["id"] for r in out["rows"]] == ["a"]
        assert seen and seen[0].strip().upper() == "SET TRANSACTION READ ONLY"
    finally:
        with engine.begin() as c:
            c.execute(text(f"DROP TABLE {table}"))
        engine.dispose()


@needs_db
def test_project_data_checks_the_columns_of_the_table_it_queries_in_any_schema():
    """The column check reads the schema the statement runs in (current_schema()), never a
    fixed `public`: a same-named table elsewhere must not lend the query its columns, and a
    table in the connection's own schema must be found."""
    from boss_engine.tools.builtin import _query_project_data

    url = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://")
    schema, table = f"qpd_s_{uuid.uuid4().hex[:8]}", f"qpd_{uuid.uuid4().hex[:8]}"
    admin = create_engine(url)
    with admin.begin() as c:
        c.execute(text(f"CREATE SCHEMA {schema}"))
        c.execute(text(f"CREATE TABLE {schema}.{table} (id TEXT, project_id TEXT)"))
        c.execute(text(f"INSERT INTO {schema}.{table} VALUES ('mine', 'p1'), ('theirs', 'p2')"))
        # A decoy in public with the same name and no project fence.
        c.execute(text(f"CREATE TABLE public.{table} (id TEXT, secret TEXT)"))
    scoped = create_engine(url, connect_args={"options": f"-c search_path={schema}"})
    try:
        out = _query_project_data(_Ctx(scoped), "p1", {"table": table})
        assert out.get("ok") is not False, out
        assert [r["id"] for r in out["rows"]] == ["mine"]
        assert "secret" not in out["rows"][0]
    finally:
        scoped.dispose()
        with admin.begin() as c:
            c.execute(text(f"DROP SCHEMA {schema} CASCADE"))
            c.execute(text(f"DROP TABLE public.{table}"))
        admin.dispose()
