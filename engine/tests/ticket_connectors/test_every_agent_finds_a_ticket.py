"""EVERY AGENT FINDS A NAMED TICKET — through its project's own ticket system, or one SHARED
with the whole org (2026-11-15).

Operator: "Agents are not working as expected - it is supposed to find the ticket from jira".
Helm, asked about a key from New work in HelpDesk Laptop, called only `recall` and said "BOSS has
no connector to pull a ticket by ID". Three causes, each pinned here against the fake Jira on
the test DB:

  * the Jira connection lived on another project, and every reader asked only "this project's
    tracker" -> a person who manages it may SHARE it with the whole org (services/ticket_reach);
    unshared, another project is told it exists and is not shared, never handed its tickets;
    a shared read is receipted, prefers the reader's own sign-in, and never copies the token;
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
    from boss_engine.services import ticket_reach

    ticket_reach.clear_cache()
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


def _share(eng, cid: str, on: bool = True, **more) -> None:
    from boss_engine.services import ticket_intake as intake

    with eng.begin() as c:
        row = intake.get_row(c, cid)
        intake.update_settings_row(c, cid, intake.merge_settings(
            row["configuration"], {"share_with_org": on, **more}))


def _receipts(eng, project_id: str) -> list[dict]:
    with eng.connect() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT effect_kind, effect_scope, result FROM receipts WHERE project_id = :p AND "
            "effect_kind = 'ticket.cross_project_read' ORDER BY created_at"),
            {"p": project_id}).mappings().all()]


# ── unshared: the old reach, and a plain "not shared" ────────────────────────────────────────

def test_an_unshared_connection_is_not_read_from_another_project(eng, org, fake):
    got = _tool("ticket_get")(Ctx(eng), project_id=org["here"], actor="usr_test",
                              args={"ref": STORY})
    assert got["ok"] is False and got["error"] == "not_shared", got
    assert "added in BOSS dogfood bootstrap and is not shared with this project" in got["message"]
    assert not fake.requests(contains="/issue/"), "an unshared connection was read"
    # Its home still reads it, exactly as before.
    home = _tool("ticket_get")(Ctx(eng), project_id=org["home"], actor="usr_test",
                               args={"ref": STORY})
    assert home["ok"] and home["connection"]["reach"] == "project", home
    # Import through it from the other project is refused too.
    done = _tool("ticket_import")(Ctx(eng), project_id=org["here"], actor="usr_test",
                                  args={"items": [{"key": STORY}], "connection_id": org["cid"]})
    assert done["ok"] is False and done["error"] == "not_shared", done
    from boss_engine.services import ticket_intake as intake

    with eng.begin() as c:
        listed = intake.list_connections(c, org["here"])
        said = intake.resolve_text(c, org["here"], STORY)
    hint = [x for x in listed if x["id"] == org["cid"]]
    assert hint and hint[0]["reach"] == "unshared" and "settings" not in hint[0]
    assert said["tickets"] == [] and said["errors"][0]["error"] == "not_shared"


# ── shared: read from another project, receipted, never a token copied ──────────────────────

def test_a_shared_key_is_read_from_another_project_with_a_receipt(eng, org, fake):
    _share(eng, org["cid"])
    got = _tool("ticket_get")(Ctx(eng), project_id=org["here"], actor="usr_test",
                              args={"ref": STORY})
    assert got["ok"], got
    assert got["ticket"]["key"] == STORY and got["ticket"]["title"] == "Add Apple Pay"
    assert got["ticket"]["acceptance_criteria"] == D.AC_ITEMS
    assert got["connection"]["site"] == fake.base and got["connection"]["reach"] == "org"
    assert got["maps_to"]["project_id"] == org["home"]
    assert got["in_this_project"] is False and "BOSS dogfood bootstrap" in got["note"]
    # The connection's home id names it too (LOW 8).
    by_home = _tool("ticket_get")(Ctx(eng), project_id=org["here"], actor="usr_test",
                                  args={"ref": STORY, "connection_id": org["cid"]})
    assert by_home["ok"], by_home
    linked = _tool("ticket_get")(Ctx(eng), project_id=org["here"], actor="usr_test",
                                 args={"ref": f"{fake.base}/browse/KAN-{D.EPIC}"})
    assert linked["ok"] and linked["ticket"]["key"] == f"KAN-{D.EPIC}", linked
    seen = _receipts(eng, org["here"])
    assert len(seen) == 3
    assert seen[0]["result"]["reader"] == "usr_test"
    assert seen[0]["result"]["connection_id"] == org["cid"]
    assert seen[0]["result"]["what"] == STORY and seen[0]["result"]["project_id"] == org["here"]
    # A read in its own home project is not a cross-project read.
    _tool("ticket_get")(Ctx(eng), project_id=org["home"], actor="usr_test", args={"ref": STORY})
    assert _receipts(eng, org["home"]) == []


def test_an_unknown_key_is_looked_up_once_and_said_to_be_absent(eng, org, fake):
    _share(eng, org["cid"])
    got = _tool("ticket_get")(Ctx(eng), project_id=org["here"], actor="usr_test",
                              args={"ref": "LEND-142"})
    assert got["ok"] is False and got["error"] == "not_found", got
    assert got["message"] == f"LEND-142 was not found in {fake.base} (Jira)."
    asked = len(fake.requests(contains="LEND-142"))
    again = _tool("ticket_get")(Ctx(eng), project_id=org["here"], actor="usr_test",
                                args={"ref": "LEND-142"})
    assert again["message"] == got["message"] and again.get("cached") is True
    assert len(fake.requests(contains="LEND-142")) == asked, "the cached answer asked Jira again"
    missing = _tool("ticket_get")(Ctx(eng), project_id=org["here"], actor="usr_test",
                                  args={"ref": "KAN-9999"})
    assert missing["error"] == "not_found" and fake.base in missing["message"], missing


def test_the_fan_out_is_capped_most_recently_used_first(eng, org, fake, monkeypatch):
    """An unknown prefix tries at most MAX_FAN_OUT connections, most recently used first."""
    from boss_engine.services import ticket_reach

    rows = [{"id": f"r{i}", "provider_key": "jira", "updated_at": f"2026-11-{10 + i:02d}"}
            for i in range(8)]
    picked = ticket_reach._key_based(rows, "ZZZ-1")
    assert [r["id"] for r, _ in picked] == ["r7", "r6", "r5", "r4", "r3"]


def test_another_org_never_reaches_the_connection_and_is_told_where_to_connect(eng, org, fake):
    if org["elsewhere"] is None:
        pytest.skip("this build cannot make a second org")
    _share(eng, org["cid"])
    got = _tool("ticket_get")(Ctx(eng), project_id=org["elsewhere"], actor="usr_test",
                              args={"ref": STORY})
    assert got["ok"] is False and got["error"] == "not_connected", got
    assert "Setup › Ticket systems" in got["next"]


def test_import_from_the_other_project_copies_no_secret_and_rotation_applies_there(
        eng, org, fake):
    _share(eng, org["cid"])
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
            "SELECT id, status, configuration FROM work_tracker_providers "
            "WHERE project_id = :p AND provider_key = 'jira'"), {"p": org["here"]}).mappings().one()
        bound = c.execute(text("SELECT project_id FROM work_tracker_bindings WHERE "
                               "provider_id = :m AND external_key = :k"),
                          {"m": mirror["id"], "k": STORY}).scalar()
        vaulted = c.execute(text("SELECT count(*) FROM secret_refs WHERE project_id = :p"),
                            {"p": org["here"]}).scalar() if _has(c, "secret_refs") else 0
    config = mirror["configuration"]
    assert (config["mirror_of"], config["mirror_reason"], mirror["status"]) == \
        (org["cid"], "used", "active")
    for secret in ("api_token", "secret_ref_id", "last4", "fingerprint", "credential_ref",
                   "oauth_client_secret", "webhook_secret_ref"):
        assert secret not in config, secret
    assert TOKEN not in json.dumps(config)
    assert vaulted == 0, "a token was copied into the importing project's vault"
    assert bound == org["here"]
    # Reads through B's mirror use the HOME's credential: a rotation there applies here.
    from boss_engine.services import ticket_intake as intake
    from boss_engine.connectors.tickets import AuthFailed, get_ticket_adapter

    def read_through_mirror():
        with eng.begin() as c:
            conn = intake.ticket_connection(c, intake.get_row(c, mirror["id"]))
            return get_ticket_adapter("jira").get_ticket(conn, STORY)

    assert read_through_mirror().key == STORY
    with eng.begin() as c:
        c.execute(text("UPDATE work_tracker_providers SET configuration = configuration || "
                       "jsonb_build_object('api_token', 'rotated-away') WHERE id = :i"),
                  {"i": org["cid"]})
    with pytest.raises(AuthFailed):
        read_through_mirror()
    with eng.begin() as c:
        c.execute(text("UPDATE work_tracker_providers SET configuration = configuration || "
                       "jsonb_build_object('api_token', CAST(:t AS TEXT)) WHERE id = :i"),
                  {"i": org["cid"], "t": TOKEN})
    assert read_through_mirror().key == STORY
    # Disconnecting at home stops the mirror too.
    with eng.begin() as c:
        c.execute(text("UPDATE work_tracker_providers SET status = 'disconnected' WHERE id = :i"),
                  {"i": org["cid"]})
    with pytest.raises(Exception):
        read_through_mirror()


def test_the_pick_up_inbox_shows_a_shared_connections_tickets_only_where_mapped(eng, org, fake):
    from boss_engine.services import ticket_intake as intake

    _share(eng, org["cid"], pickup={"label": "boss", "mode": "ask"})
    with eng.begin() as c:
        none = intake.waiting(c, org["here"])
    assert none["tickets"] == [] and none["errors"] == []
    _share(eng, org["cid"], mappings=[
        {"external_project": "KAN", "boss_project_id": org["here"], "default": True}])
    with eng.begin() as c:
        mapped = intake.waiting(c, org["here"])
    assert STORY in {t["key"] for t in mapped["tickets"]}, mapped


def test_new_work_resolves_a_bare_key_once_shared(eng, org, fake):
    from boss_engine.services import ticket_intake as intake

    _share(eng, org["cid"])
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
    assert shared and shared[0]["reach"] == "org" and shared[0]["settings"]["share_with_org"]
    assert {k: shared[0]["home_project"][k] for k in ("id", "name")} == \
        {"id": org["home"], "name": "BOSS dogfood bootstrap"}


# ── every agent has it, with its own permission, and is told when to use it ──────────────────

def test_every_agent_holds_the_ticket_reads_under_ticket_read_only():
    from boss_engine.identity.permissions_gen import PASSPORT_PRESETS
    from boss_engine.services import agent_baseline as ab
    from boss_engine.tools import build_default_registry

    assert {"ticket_get", "ticket_search", "ticket_browse"} <= set(ab.FUNDAMENTALS_TOOLS)
    assert "boss.fundamentals" in ab.BASELINE_SKILLS
    assert "ticket_get" in ab.PRIMER and "not found" in ab.PRIMER and "UTF-8" in ab.PRIMER
    reg = build_default_registry()
    for name in ("ticket_get", "ticket_search", "ticket_browse"):
        assert reg.get(name).permission == "ticket:read", name
    assert "chat" in reg.get("ticket_get").exposure.core_for
    for preset in PASSPORT_PRESETS.values():
        assert "ticket:read" in preset.permissions, preset.key
    # integration:read is NOT handed to every agent for this: only the presets that had it.
    for key in ("reviewer", "analyst", "writer", "guardian", "archivist", "baseline"):
        assert "integration:read" not in PASSPORT_PRESETS[key].permissions, key


def test_a_cli_chat_turn_is_offered_ticket_get_and_reads_the_ticket(eng, org, fake, monkeypatch):
    """The CLI text loop offers the chat CORE only (BOSSAPP-1006). ticket_get is in it, so an
    agent asked about KAN-2 can read it in its first step and answer with the title."""
    from boss_engine.agent.engine_agent import EngineAgent
    from boss_engine.kernel.scoped_tools import FLAG
    from boss_engine.tools.builtin import build_discuss_read_registry
    from tests.test_cli_chat_turn_efficiency import ScriptedCli, _call, _schema_names

    _share(eng, org["cid"])
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


# ── re-review (2026-11-15): no cache crosses projects; every tool says not_shared ────────────

def test_one_projects_cached_not_found_never_reaches_another_project(eng, org, fake):
    """The home project's (unshared) Jira lacks ZED-2; "here" has its OWN Jira, which holds it.
    Asked in the home first (a cached "not found in <home site>"), then here within the cache
    window: here still finds it, and never sees the home's site."""
    from boss_engine.services import ticket_reach

    other = FakeJira()
    try:
        other._add("ZED-2", {"title": "Only on the other site", "type": "Task",
                             "status": "To Do", "body": "x", "labels": []})
        own = f"trk_{uuid.uuid4().hex[:12]}"
        with eng.begin() as c:
            c.execute(text(
                "INSERT INTO work_tracker_providers (id, project_id, provider_key, display_name, "
                "status, is_default, configuration, created_by) VALUES (:id, :p, 'jira', 'Jira', "
                "'active', true, CAST(:c AS JSONB), 'test')"),
                {"id": own, "p": org["here"], "c": json.dumps({
                    "base_url": other.base, "email": EMAIL, "project_key": "KAN",
                    "api_token": TOKEN, "ticket_system": True, "settings": {}})})
        home = _tool("ticket_get")(Ctx(eng), project_id=org["home"], actor="usr_test",
                                   args={"ref": "ZED-2"})
        assert home["error"] == "not_found" and fake.base in home["message"], home
        here = _tool("ticket_get")(Ctx(eng), project_id=org["here"], actor="usr_test",
                                   args={"ref": "ZED-2"})
        assert here["ok"], here
        assert here["ticket"]["title"] == "Only on the other site"
        assert fake.base not in json.dumps(here)
        again = _tool("ticket_get")(Ctx(eng), project_id=org["home"], actor="usr_test",
                                    args={"ref": "ZED-2"})
        assert again.get("cached") is True and other.base not in again["message"]
    finally:
        ticket_reach.clear_cache()
        other.close()


def test_after_unsharing_every_ticket_tool_says_not_shared(eng, org, fake):
    _share(eng, org["cid"], True)
    assert _tool("ticket_get")(Ctx(eng), project_id=org["here"], actor="usr_test",
                               args={"ref": STORY})["ok"]
    _share(eng, org["cid"], False)
    calls = {
        "ticket_get": {"ref": STORY},
        "ticket_search": {"query": "Apple"},
        "ticket_browse": {"what": "projects"},
        "ticket_comment": {"ref": STORY, "body": "hi"},
        "ticket_transition": {"ref": STORY, "to": "review"},
        "ticket_import": {"items": [{"key": STORY}]},
    }
    for name, args in calls.items():
        got = _tool(name)(Ctx(eng), project_id=org["here"], actor="usr_test", args=args)
        assert got["ok"] is False and got["error"] == "not_shared", (name, got)
        assert "BOSS dogfood bootstrap" in got["message"], (name, got)
    assert not fake.requests(method="POST"), "a write reached the unshared site"
