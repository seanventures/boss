"""EVERY AGENT FINDS A NAMED TICKET, FROM ANY PROJECT OF THE ORG (2026-11-15).

Operator: "Agents are not working as expected - it is supposed to find the ticket from jira".
Helm, asked about a key from New work in HelpDesk Laptop, called only `recall` and said "BOSS has
no connector to pull a ticket by ID". Three causes, each pinned here against the fake Jira on
the test DB:

  * the Jira connection lived on another project, and every reader asked only "this project's
    tracker" -> a ticket system is ORG-LEVEL (services/ticket_reach): read, searched and
    imported from every project of its org, never from another org;
  * `ticket_get` was a deferred tool (find_tools first) and in no agent's fundamentals -> it is
    in the chat core and in `boss.fundamentals`, and the primer says to call it;
  * a key whose project prefix no connection knew was never looked up -> it is tried on every
    reachable key-based system and answered "not found in <site>".
"""
from __future__ import annotations

import base64
import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

os.environ.setdefault("BOSS_MASTER_KEY",
                      base64.b64encode(b"p29a-test-master-key-32-bytes!!!").decode())

from boss_engine.connectors.tickets import http as ticket_http  # noqa: E402
from boss_engine.identity import vault  # noqa: E402

from . import dataset as D  # noqa: E402
from .fake_jira import EMAIL, TOKEN, FakeJira  # noqa: E402

TEST_DB = os.environ.get("BOSS_TEST_DATABASE_URL", os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"))
STORY = f"KAN-{D.STORY}"


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM work_tracker_bindings LIMIT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable or not migrated: {type(error).__name__}")
    vault.reset_master_key_cache()
    return engine


@pytest.fixture()
def fake(monkeypatch):
    server = FakeJira()
    monkeypatch.setattr(ticket_http, "SLEEP", lambda s: None)
    monkeypatch.setenv("BOSS_TICKET_SYNC", "off")
    yield server
    server.close()


def _settings():
    from boss_engine.config import get_settings

    return get_settings()


class Ctx:
    def __init__(self, engine):
        self.engine = engine
        self.settings = _settings()

    def begin(self):
        return self.engine.begin()


def _project(c, wid: str, name: str) -> str:
    pid = f"p_tkreach_{uuid.uuid4().hex[:8]}"
    c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:i, :w, :n, :i)"),
              {"i": pid, "w": wid, "n": name})
    return pid


@pytest.fixture()
def org(eng, fake):
    """Two projects of one org; Jira connected (as a ticket system) from the FIRST only, the
    way the operator's landed on boss-dogfood. Plus a project of ANOTHER org."""
    made: list[str] = []
    with eng.begin() as c:
        wid = c.execute(text("SELECT id FROM workspaces LIMIT 1")).scalar()
        if wid is None:
            pytest.skip("test database has no workspace")
        home = _project(c, wid, "BOSS dogfood bootstrap")
        here = _project(c, wid, "HelpDesk Laptop")
        made += [home, here]
        other_org = f"org_tkreach_{uuid.uuid4().hex[:8]}"
        other_ws = f"ws_tkreach_{uuid.uuid4().hex[:8]}"
        try:
            with c.begin_nested():
                c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'Elsewhere')"),
                          {"o": other_org})
                c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'x')"),
                          {"w": other_ws, "o": other_org})
            elsewhere = _project(c, other_ws, "Another org's app")
            made.append(elsewhere)
        except Exception:  # noqa: BLE001 — a build whose orgs table differs: no cross-org case
            elsewhere = None
        cid = f"trk_{uuid.uuid4().hex[:12]}"
        config = {"base_url": fake.base, "email": EMAIL, "project_key": "KAN",
                  "api_token": TOKEN, "ticket_system": True, "settings": {}}
        c.execute(text(
            "INSERT INTO work_tracker_providers (id, project_id, provider_key, display_name, "
            "status, is_default, configuration, created_by) VALUES (:id, :p, 'jira', 'Jira', "
            "'active', true, CAST(:c AS JSONB), 'test')"),
            {"id": cid, "p": home, "c": json.dumps(config)})
    yield {"home": home, "here": here, "elsewhere": elsewhere, "cid": cid}
    with eng.begin() as c:
        for pid in made:
            items = list(c.execute(text("SELECT vault_item_id FROM connection_credentials WHERE "
                                        "project_id = :p"), {"p": pid}).scalars().all()) \
                if _has(c, "connection_credentials") else []
            for sql in ("DELETE FROM work_tracker_bindings WHERE project_id = :p",
                        "DELETE FROM work_items WHERE project_id = :p",
                        "DELETE FROM connection_credentials WHERE project_id = :p",
                        "DELETE FROM secret_refs WHERE project_id = :p",
                        "DELETE FROM receipts WHERE project_id = :p",
                        "DELETE FROM tool_result_screens WHERE project_id = :p",
                        "DELETE FROM work_tracker_providers WHERE project_id = :p",
                        "DELETE FROM events WHERE project_id = :p",
                        "DELETE FROM projects WHERE id = :p"):
                try:
                    with c.begin_nested():
                        c.execute(text(sql), {"p": pid})
                except Exception:  # noqa: BLE001 — a table this build lacks
                    pass
            if items:
                with c.begin_nested():
                    c.execute(text("DELETE FROM identity_vault_items WHERE id = ANY(:i)"),
                              {"i": items})
        for sql in ("DELETE FROM workspaces WHERE id LIKE 'ws_tkreach_%'",
                    "DELETE FROM orgs WHERE id LIKE 'org_tkreach_%'"):
            try:
                with c.begin_nested():
                    c.execute(text(sql))
            except Exception:  # noqa: BLE001
                pass


def _has(c, table: str) -> bool:
    return bool(c.execute(text("SELECT to_regclass(:t)"), {"t": table}).scalar())


def _tool(name: str):
    from boss_engine.tools import build_default_registry

    return build_default_registry().get(name).handler


# ── the tools, from a project that did not connect Jira ──────────────────────────────────────

def test_ticket_get_finds_a_key_from_another_project_and_says_where_it_belongs(eng, org, fake):
    got = _tool("ticket_get")(Ctx(eng), project_id=org["here"], actor="usr_test",
                              args={"ref": STORY})
    assert got["ok"], got
    assert got["ticket"]["key"] == STORY and got["ticket"]["title"] == "Add Apple Pay"
    assert got["ticket"]["acceptance_criteria"] == D.AC_ITEMS
    assert got["connection"]["site"] == fake.base and got["connection"]["reach"] == "org"
    assert got["maps_to"]["project_id"] == org["home"]
    assert got["in_this_project"] is False and "BOSS dogfood bootstrap" in got["note"]
    # A link works the same way.
    linked = _tool("ticket_get")(Ctx(eng), project_id=org["here"], actor="usr_test",
                                 args={"ref": f"{fake.base}/browse/KAN-{D.EPIC}"})
    assert linked["ok"] and linked["ticket"]["key"] == f"KAN-{D.EPIC}", linked


def test_an_unknown_key_is_looked_up_and_said_to_be_absent_in_the_site(eng, org, fake):
    got = _tool("ticket_get")(Ctx(eng), project_id=org["here"], actor="usr_test",
                              args={"ref": "LEND-142"})
    assert got["ok"] is False and got["error"] == "not_found", got
    assert got["message"] == f"LEND-142 was not found in {fake.base} (Jira)."
    # A known project, a missing number: the same plain answer.
    missing = _tool("ticket_get")(Ctx(eng), project_id=org["here"], actor="usr_test",
                                  args={"ref": "KAN-9999"})
    assert missing["error"] == "not_found" and fake.base in missing["message"], missing


def test_another_org_never_reaches_the_connection_and_is_told_where_to_connect(eng, org, fake):
    if org["elsewhere"] is None:
        pytest.skip("this build cannot make a second org")
    got = _tool("ticket_get")(Ctx(eng), project_id=org["elsewhere"], actor="usr_test",
                              args={"ref": STORY})
    assert got["ok"] is False and got["error"] == "not_connected", got
    assert "Setup › Ticket systems" in got["next"]


def test_search_and_import_work_from_the_other_project(eng, org, fake):
    found = _tool("ticket_search")(Ctx(eng), project_id=org["here"], actor="usr_test",
                                   args={"labels": ["boss"], "open_only": True})
    assert found["ok"] and STORY in {t["key"] for t in found["tickets"]}, found
    done = _tool("ticket_import")(Ctx(eng), project_id=org["here"], actor="usr_test",
                                  args={"items": [{"key": STORY}]})
    assert done["ok"], done
    assert [i["key"] for i in done["imported"]] == [STORY]
    again = _tool("ticket_import")(Ctx(eng), project_id=org["here"], actor="usr_test",
                                   args={"items": [{"key": STORY}]})
    assert [e["key"] for e in again["already_in_boss"]] == [STORY]
    with eng.connect() as c:
        mirror = c.execute(text(
            "SELECT id, status, configuration->>'mirror_of' AS home, "
            "configuration->>'mirror_reason' AS why FROM work_tracker_providers "
            "WHERE project_id = :p AND provider_key = 'jira'"), {"p": org["here"]}).mappings().one()
        bound = c.execute(text("SELECT project_id FROM work_tracker_bindings WHERE "
                               "provider_id = :m AND external_key = :k"),
                          {"m": mirror["id"], "k": STORY}).scalar()
    assert (mirror["home"], mirror["why"], mirror["status"]) == (org["cid"], "used", "active")
    assert bound == org["here"]


def test_new_work_resolves_a_bare_key_in_the_other_project(eng, org, fake):
    from boss_engine.services import ticket_intake as intake

    with eng.begin() as c:
        seen = intake.resolve_text(c, org["here"], STORY)
        absent = intake.resolve_text(c, org["here"], "LEND-142")
        sentence = intake.resolve_text(c, org["here"], "Encode names as UTF-8 please")
        listed = intake.list_connections(c, org["here"])
    assert [t["key"] for t in seen["tickets"]] == [STORY]
    assert seen["tickets"][0]["maps_to"]["project_id"] == org["home"]
    assert seen["tickets"][0]["connection_reach"] == "org"
    assert absent["tickets"] == [] and absent["errors"][0]["error"] == "not_found"
    assert sentence == {"refs": [], "tickets": [], "errors": [], "connected": 1}
    shared = [c for c in listed if c["id"] == org["cid"]]
    assert shared and shared[0]["reach"] == "org"
    assert shared[0]["home_project"] == {"id": org["home"], "name": "BOSS dogfood bootstrap"}


# ── every agent has it, and is told to use it ────────────────────────────────────────────────

def test_every_agent_holds_the_ticket_reads_and_is_told_to_call_them():
    from boss_engine.services import agent_baseline as ab
    from boss_engine.tools import build_default_registry

    assert {"ticket_get", "ticket_search", "ticket_browse"} <= set(ab.FUNDAMENTALS_TOOLS)
    assert "boss.fundamentals" in ab.BASELINE_SKILLS
    assert "ticket_get" in ab.PRIMER and "not found" in ab.PRIMER
    spec = build_default_registry().get("ticket_get")
    assert "chat" in spec.exposure.core_for


def test_a_cli_chat_turn_is_offered_ticket_get_and_reads_the_ticket(eng, org, fake, monkeypatch):
    """The CLI text loop offers the chat CORE only (BOSSAPP-1006). ticket_get is in it, so an
    agent asked about KAN-2 can read it in its first step and answer with the title."""
    from boss_engine.agent.engine_agent import EngineAgent
    from boss_engine.kernel.scoped_tools import FLAG
    from boss_engine.tools.builtin import build_discuss_read_registry
    from tests.test_cli_chat_turn_efficiency import ScriptedCli, _call, _schema_names

    monkeypatch.delenv(FLAG, raising=False)
    final = json.dumps({"thought": "t", "final": "KAN-2 is 'Add Apple Pay'."})
    cli = ScriptedCli([_call("ticket_get", {"ref": STORY}), final])
    agent = EngineAgent(ctx=Ctx(eng), registry=build_discuss_read_registry(), adapter=cli)
    turn = agent.chat(project_id=org["here"], actor="usr_test",
                      message="About new work I'm drafting (ticket or link):\n" + STORY)
    assert "ticket_get" in _schema_names(cli.calls[0][0])
    read = next(t for t in turn.trace if t.get("tool") == "ticket_get")
    assert read["result"]["ok"], read["result"]
    assert read["result"]["ticket"]["title"] == "Add Apple Pay"
    assert "Add Apple Pay" in cli.calls[1][0]
