"""Models are the ORG's: set once, every project of the org uses them (org_models).

Breaks covered (banking case study, 2026-10-24: four northpeak-bank-* projects answered "BOSS
needs a connected model … none is answering"; only boss-dogfood held the model rows):
  * a project with no model rows of its own not reading the org's choices;
  * a project's own pin not winning over the org, or not being removable;
  * Setup's write landing on one project instead of the org (and leaving the writer's pin);
  * the upgrade not adopting the most-configured project's models as the org's, or doing it twice;
  * a new project holding none of the org's connections, or a mirror reaching a key the
    project cannot resolve; a project's own connection overwritten by the org's;
  * a new project's chat not answering with the org's model;
  * a per-run model_config no longer winning over the org's engine;
  * Setup's view not saying whose models they are.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from boss_engine.config import get_settings
from boss_engine.db import ensure_schema
from boss_engine.services import org_models, project_settings as ps
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

ROOT = Path(__file__).resolve().parents[2]
ORG, OTHER_ORG = "org_om", "org_om2"
HOME, NEW, PINNED, ELSEWHERE = "om-home", "om-new", "om-pinned", "om-elsewhere"


def _database_url() -> str:
    return os.environ.get("BOSS_KG_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


@pytest.fixture(scope="module")
def pg_engine():
    url = _database_url()
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if not url or not any(marker in name for marker in ("test", "testing", "ci", "scratch", "tmp")):
        pytest.skip("BOSS_KG_TEST_DATABASE_URL must name a disposable database")
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        pytest.skip(f"disposable postgres is not reachable: {exc.__class__.__name__}")
    ensure_schema(engine)
    return engine


@pytest.fixture()
def connection(pg_engine):
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        try:
            for org in (ORG, OTHER_ORG):
                conn.execute(text("INSERT INTO orgs (id, name) VALUES (:o, :o)"), {"o": org})
                conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'W')"),
                             {"w": f"ws_{org}", "o": org})
            for pid, org, age in ((HOME, ORG, 3), (NEW, ORG, 1), (PINNED, ORG, 2),
                                  (ELSEWHERE, OTHER_ORG, 1)):
                conn.execute(text(
                    "INSERT INTO projects (id, workspace_id, name, slug, created_at) "
                    "VALUES (:p, :w, :p, :p, now() - make_interval(days => :d))"),
                    {"p": pid, "w": f"ws_{org}", "d": age})
            yield conn
        finally:
            transaction.rollback()


def _own(connection, pid):
    return ps.get_own(connection, pid)


# ── the resolver: project → org → install ────────────────────────────────────────────────────

def test_a_project_without_rows_reads_the_orgs_models_and_its_pin_wins(connection):
    settings = get_settings()
    ps.set_one(connection, HOME, "chat_model", "claude-sonnet-5", actor="ceo")
    ps.set_one(connection, HOME, "code_agent_engine", "codex", actor="ceo")
    # Written ONCE, for the org: no project holds a row, every project reads it.
    assert _own(connection, HOME) == {} and _own(connection, NEW) == {}
    for pid in (HOME, NEW, PINNED):
        assert ps.get_all(connection, pid)["chat_model"] == "claude-sonnet-5"
        assert ps.effective(connection, pid, settings, "code_agent_engine") == "codex"
        assert ps.applied(connection, pid, settings).code_agent_engine == "codex"
        assert ps.source_of(connection, pid, "chat_model") == "org"
    # Another org reads none of it: the install's value.
    assert "chat_model" not in ps.get_all(connection, ELSEWHERE)
    assert ps.effective(connection, ELSEWHERE, settings, "code_agent_engine") == \
        settings.code_agent_engine
    # A pin is this project's only, and wins there.
    ps.set_one(connection, PINNED, "chat_model", "claude-haiku-5", actor="ceo", scope="project")
    assert ps.get_all(connection, PINNED)["chat_model"] == "claude-haiku-5"
    assert ps.source_of(connection, PINNED, "chat_model") == "project"
    assert ps.get_all(connection, NEW)["chat_model"] == "claude-sonnet-5"
    # Removing the pin hands the project back to the org's choice, not to the install.
    ps.clear_one(connection, PINNED, "chat_model", actor="ceo", scope="project")
    assert ps.get_all(connection, PINNED)["chat_model"] == "claude-sonnet-5"
    assert org_models.org_values(connection, NEW)["chat_model"] == "claude-sonnet-5"


def test_setup_writes_the_org_and_drops_the_writers_pin(connection):
    ps.set_one(connection, PINNED, "code_review_model", "opus", actor="ceo", scope="project")
    ps.set_one(connection, PINNED, "code_review_model", "sonnet", actor="ceo")   # Setup's write
    assert "code_review_model" not in _own(connection, PINNED)
    assert ps.get_all(connection, NEW)["code_review_model"] == "sonnet"
    # Clearing at org scope: every project back to the install's value.
    ps.clear_one(connection, NEW, "code_review_model", actor="ceo")
    assert "code_review_model" not in ps.get_all(connection, PINNED)
    # A knob that is not a model's stays the project's, whatever the scope says.
    ps.set_one(connection, NEW, "code_agent_max_turns", 40, actor="ceo")
    assert _own(connection, NEW) == {"code_agent_max_turns": 40}
    assert "code_agent_max_turns" not in ps.get_all(connection, HOME)
    with pytest.raises(ValueError):
        ps.set_one(connection, NEW, "chat_model", "x", actor="ceo", scope="everywhere")


def test_version_and_change_record_follow_the_org_row(connection):
    assert ps.version_of(connection, NEW, "chat_model") == ("unset", None)
    ps.set_one(connection, HOME, "chat_model", "claude-sonnet-5", actor="ceo")
    version, by = ps.version_of(connection, NEW, "chat_model")
    assert version != "unset" and by == "ceo"
    event = connection.execute(text(
        "SELECT data FROM events WHERE project_id = :p AND event_type = 'settings.changed' "
        "ORDER BY created_at DESC LIMIT 1"), {"p": HOME}).scalar()
    payload = event if isinstance(event, dict) else json.loads(event)
    assert payload["scope"] == "org" and payload["to"] == "claude-sonnet-5"


# ── the upgrade: the org adopts its most-configured project's models ─────────────────────────

def _upgrade_step() -> str:
    sql = (ROOT / "db" / "schema.sql").read_text()
    start = sql.index("-- MODELS ARE THE ORG'S")
    block = sql[start:]
    return block[block.index("DO $$"):block.index("END $$;") + len("END $$;")]


def test_the_upgrade_adopts_the_most_configured_project_once(connection):
    for key, value in (("chat_model", "claude-sonnet-5"), ("code_agent_engine", "claude_agent_sdk"),
                       ("chat_llm_engine", "claude_code")):
        connection.execute(text(
            "INSERT INTO project_settings (project_id, key, value) VALUES (:p, :k, CAST(:v AS JSONB))"),
            {"p": HOME, "k": key, "v": json.dumps(value)})
    connection.execute(text(
        "INSERT INTO project_settings (project_id, key, value) VALUES "
        "(:p, 'chat_model', '\"claude-haiku-5\"'), (:p, 'knowledge_graph', '\"off\"')"), {"p": PINNED})
    connection.execute(text("DELETE FROM schema_once WHERE key = 'org_models_adopted_from_project'"))
    connection.execute(text(_upgrade_step()))
    assert org_models.home_project(connection, ORG) == HOME
    assert org_models.org_values(connection, NEW) == {
        "chat_model": "claude-sonnet-5", "code_agent_engine": "claude_agent_sdk",
        "chat_llm_engine": "claude_code"}
    assert _own(connection, HOME) == {}                       # moved, not copied
    assert _own(connection, PINNED) == {"chat_model": "claude-haiku-5", "knowledge_graph": "off"}
    assert ps.get_all(connection, NEW)["chat_model"] == "claude-sonnet-5"
    # An org with nothing configured is left without a home; it gets one on its first write.
    assert org_models.home_project(connection, OTHER_ORG) is None
    # Once: the key is recorded; and an org that already has model rows is never re-adopted.
    assert connection.execute(text(
        "SELECT 1 FROM schema_once WHERE key = 'org_models_adopted_from_project'")).scalar() == 1
    connection.execute(text("DELETE FROM schema_once WHERE key = 'org_models_adopted_from_project'"))
    connection.execute(text(_upgrade_step()))
    assert _own(connection, PINNED)["chat_model"] == "claude-haiku-5"
    assert org_models.org_values(connection, NEW)["chat_model"] == "claude-sonnet-5"


# ── the connections: the org's model home, mirrored into every project ──────────────────────

def _connect(connection, pid, source_id="setup-anthropic", *, key=True, metadata=None):
    from boss_engine.services.secrets_service import put_secret

    ref = None
    if key:
        ref = put_secret(connection, get_settings(), project_id=pid, name=f"KEY_{source_id}",
                         value="sk-ant-api03-ORGMODELSTESTKEY0123456789", backend="database",
                         actor="ceo")["secret_ref_id"]
    connection.execute(text(
        "INSERT INTO entitlement_sources (id, project_id, provider, source_id, source_kind, "
        " entitlement_kind, owner_type, owner_id, credential_secret_ref_id, connection_status, "
        " status, supported_models, connection_metadata, runtime_supported) "
        "VALUES (:id, :p, 'anthropic', :s, 'api', 'api_key', 'project', :p, :ref, 'connected', "
        " 'active', '[\"claude-sonnet-5\"]', CAST(:m AS JSONB), true) "
        "ON CONFLICT (project_id, source_id) DO UPDATE SET credential_secret_ref_id = :ref, "
        " connection_status = 'connected', connection_metadata = CAST(:m AS JSONB)"),
        {"id": f"src_{pid}_{source_id}", "p": pid, "s": source_id, "ref": ref,
         "m": json.dumps(metadata or {"model": "claude-sonnet-5"})})
    return ref


def _row(connection, pid, source_id="setup-anthropic"):
    row = connection.execute(text(
        "SELECT connection_status, credential_secret_ref_id, connection_metadata "
        "FROM entitlement_sources WHERE project_id = :p AND source_id = :s"),
        {"p": pid, "s": source_id}).mappings().first()
    return dict(row) if row else None


def test_every_project_holds_the_orgs_connection_and_reaches_its_key(connection):
    from boss_engine.services.secret_backends import resolve_secret_ref

    ref = _connect(connection, HOME)
    assert org_models.write_target(connection, HOME) == HOME          # the org's first: home
    assert org_models.home_project(connection, ORG) == HOME
    counts = org_models.sync_sources(connection, ORG)
    assert counts["mirrored"] == 2                                    # NEW and PINNED
    mirror = _row(connection, NEW)
    assert mirror["connection_status"] == "connected" and mirror["credential_secret_ref_id"] == ref
    assert mirror["connection_metadata"]["org_source"] == HOME
    scope = connection.execute(text("SELECT scope_type, scope_id, provider FROM secret_refs "
                                    "WHERE id = :i"), {"i": ref}).mappings().first()
    assert dict(scope) == {"scope_type": "org", "scope_id": ORG, "provider": "anthropic"}
    assert resolve_secret_ref(connection, ref, get_settings(), project_id=NEW).startswith("sk-ant")
    # Another org's project never gets it, and cannot resolve the key.
    assert _row(connection, ELSEWHERE) is None
    with pytest.raises(Exception):
        resolve_secret_ref(connection, ref, get_settings(), project_id=ELSEWHERE)
    # Idempotent.
    assert org_models.sync_sources(connection, ORG)["mirrored"] == 2
    # Setup in another project writes the org's home, not that project.
    assert org_models.write_target(connection, NEW) == HOME
    assert org_models.write_target(connection, NEW, scope="project") == NEW


def test_a_projects_own_connection_is_kept_and_a_removed_one_is_reset(connection):
    _connect(connection, HOME)
    org_models.set_home(connection, ORG, HOME, "ceo")
    own = _connect(connection, PINNED)
    counts = org_models.sync_sources(connection, ORG)
    assert counts["kept_own"] == 1
    assert _row(connection, PINNED)["credential_secret_ref_id"] == own
    assert "org_source" not in _row(connection, PINNED)["connection_metadata"]
    # The project's own source is ITS write target.
    assert org_models.write_target(connection, PINNED, source_id="setup-anthropic") == PINNED
    # The home disconnects: the mirror goes back to not connected; the own row stays.
    connection.execute(text("UPDATE entitlement_sources SET connection_status = 'not_connected', "
                            "credential_secret_ref_id = NULL WHERE project_id = :p"), {"p": HOME})
    counts = org_models.sync_sources(connection, ORG)
    assert counts["reset"] == 1
    assert _row(connection, NEW)["connection_status"] == "not_connected"
    assert _row(connection, NEW)["credential_secret_ref_id"] is None
    assert _row(connection, PINNED)["connection_status"] == "connected"


def test_a_new_project_answers_with_the_orgs_chat_model(connection, monkeypatch):
    from boss_engine.services import model_routing, setup_service

    _connect(connection, HOME)
    org_models.set_home(connection, ORG, HOME, "ceo")
    ps.set_one(connection, HOME, "chat_llm_engine", "claude_code", actor="ceo")
    ps.set_one(connection, HOME, "claude_cli_model", "sonnet", actor="ceo")
    # Created after the org's models were set: created the way create_project does it.
    connection.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                            "VALUES ('om-later', :w, 'L', 'om-later')"), {"w": f"ws_{ORG}"})
    org_models.sync_for(connection, "om-later")
    settings = get_settings()
    found = setup_service.connected_engines(connection, settings, "om-later")
    assert "claude_code" in found
    picked = model_routing.resolve_job(connection, settings, "om-later", "chat")
    assert (picked.engine, picked.model) == ("claude_code", "sonnet")
    assert picked.why == model_routing.CHOSEN


def test_create_project_mirrors_the_orgs_connections(connection):
    from boss_engine.bootstrap import create_project
    from boss_engine.services.entitlements import EntitlementBrokerService
    from boss_engine.services.members import MemberDirectoryService
    from boss_engine.services.specpack import SpecPackService

    _connect(connection, HOME)
    org_models.set_home(connection, ORG, HOME, "ceo")
    settings = get_settings()
    create_project(connection, settings, org_id=ORG, name="Bank", slug="om-bank",
                   entitlements=EntitlementBrokerService(settings),
                   members=MemberDirectoryService(settings), specpack=SpecPackService(settings))
    assert _row(connection, "om-bank")["connection_status"] == "connected"


def test_a_per_run_model_config_still_wins_over_the_org(connection):
    from boss_engine.services.model_router.stages import lane_engines

    ps.set_one(connection, HOME, "code_agent_engine", "claude_code", actor="ceo")
    settings = get_settings()
    _, build, _ = lane_engines(connection, settings, NEW, {
        "model_config": {"default": {"engine": "codex", "model": "gpt-5-codex"}}})
    assert build == "codex"
    layered, _, _ = lane_engines(connection, settings, NEW, {})
    assert layered.code_agent_engine == "claude_code"


def test_setup_view_says_whose_models_they_are(connection):
    from boss_engine.services import setup_view

    _connect(connection, HOME)
    org_models.set_home(connection, ORG, HOME, "ceo")
    org_models.sync_sources(connection, ORG)
    ps.set_one(connection, HOME, "chat_llm_engine", "claude_code", actor="ceo")
    ps.set_one(connection, PINNED, "code_review_engine", "claude_code", actor="ceo", scope="project")
    settings = get_settings()
    view = setup_view.build(connection, settings, NEW)
    assert view["models_scope"]["scope"] == "org" and view["models_scope"]["home_project"] == HOME
    assert view["models_scope"]["overrides"] == []
    assert {m["source_id"]: m["scope"] for m in view["models"]}["setup-anthropic"] == "org"
    chat = next(j for j in view["jobs"] if j["job"] == "chat")
    assert chat["choice"]["engine"] == "claude_code" and chat["scope"] == "org"
    pinned = setup_view.build(connection, settings, PINNED)
    review = next(j for j in pinned["jobs"] if j["job"] == "review")
    assert review["scope"] == "project"
    assert [o["key"] for o in pinned["models_scope"]["overrides"]] == ["code_review_engine"]
    home = setup_view.build(connection, settings, HOME)
    assert home["models_scope"]["is_home"] is True
    assert {m["source_id"]: m["scope"] for m in home["models"]}["setup-anthropic"] == "org"


def test_every_model_knob_is_a_known_setting_and_the_upgrade_lists_the_same_keys():
    assert org_models.MODEL_KNOBS <= set(ps.KNOBS)
    step = _upgrade_step()
    listed = set(re.findall(r"'([a-z_]+)'", step[step.index("ARRAY["):step.index("];")]))
    assert listed == set(org_models.MODEL_KNOBS)
