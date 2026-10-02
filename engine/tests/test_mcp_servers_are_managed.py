"""An MCP server is a thing an operator manages: registers, edits, checks, and removes safely.

THE INCIDENTS, all on boss-1:

* A server could be registered and switched on or off, and nothing else. Changing its URL meant
  delete-and-re-register, which silently unbound every custom step on it (ON DELETE SET NULL).
* Names were not unique. A double-pressed Register made two servers called the same thing, and
  the agent loop namespaces tools by NAME, so the second silently replaced the first's tools.
* Delete checked nothing: authored skills kept an id pointing at nothing.
* The three servers BOSS seeds point at 127.0.0.1 ports the community compose never starts.
  Every screen called them enabled and healthy, the builder waited 6 seconds per server per
  load to find out otherwise, and remembered nothing.
* A re-seed put back the URL an operator had fixed, and recreated a server they had removed.
* The same tool's tier was computed by three copies of one rule, one of which let the tool's
  own annotation lower the operator's tier.

Every one of those is a property of the server MODEL (mcp_registry), so that is where the tests
point; the first two enumerate the code so a second owner cannot appear.
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import mcp_registry as reg

ROOT = pathlib.Path(__file__).resolve().parents[2]
TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"),
)
_SHIPPING = ("engine/boss_engine", "apps/api/app", "apps/worker")


def _sql_constants(pattern: re.Pattern) -> list[str]:
    found = []
    for top in _SHIPPING:
        for path in sorted((ROOT / top).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if "/tests/" in rel:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if pattern.search(" ".join(node.value.split())):
                        found.append(f"{rel}:{node.lineno}")
    return found


def test_only_the_registry_writes_mcp_servers():
    """Every INSERT/UPDATE/DELETE of mcp_servers in shipping code, enumerated. A second writer
    is how uniqueness, reference checks and the operator's edits get bypassed."""
    writes = _sql_constants(re.compile(
        r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+mcp_servers\b", re.IGNORECASE))
    assert writes, "the scan found nothing — it is not looking where the code is"
    strays = [w for w in writes
              if not w.startswith("engine/boss_engine/services/mcp_registry.py")]
    assert not strays, f"only mcp_registry may write mcp_servers: {strays}"


#: Who may ask a server for its tools, and why. Everyone else reads what the registry recorded.
_PROBERS = {
    "engine/boss_engine/services/mcp_registry.py": "the registry's own probe",
    # The agent loop discovers tools for the run it is in, and records what it found.
    "engine/boss_engine/services/agent_loop.py": "discovery for a live run (recorded)",
    # The client itself.
    "engine/boss_engine/services/mcp_client.py": "the client",
    # The repo-host adapter talks to a configured repository-host MCP endpoint (settings, not
    # a registered server), so there is no mcp_servers row to record against.
    "engine/boss_engine/services/mcp_repo_host.py": "configured repo host, not a registered server",
}


def test_every_screen_reads_reachability_from_the_registry():
    """The builder and Capabilities each probed every server inline, every load, 6 seconds
    each, and kept nothing — so the screens disagreed about whether a server was up. Every
    `.list_tools(` call in shipping code is enumerated; only these may make one."""
    callers = []
    for top in _SHIPPING:
        for path in sorted((ROOT / top).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if "/tests/" in rel:
                continue
            if ".list_tools(" in path.read_text(encoding="utf-8"):
                callers.append(rel)
    strays = [c for c in callers if c not in _PROBERS]
    assert not strays, f"probe through mcp_registry.servers_with_tools instead: {strays}"


def _tables_referencing_mcp_servers() -> set[str]:
    schema = (ROOT / "db/schema.sql").read_text(encoding="utf-8")
    found = set()
    for match in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", schema, re.S):
        if re.search(r"REFERENCES\s+mcp_servers\s*\(\s*id\s*\)", match.group(2)):
            found.add(match.group(1))
    for match in re.finditer(r"ALTER TABLE (?:IF EXISTS )?(\w+)[^;]*REFERENCES\s+mcp_servers",
                             schema):
        found.add(match.group(1))
    return found


def test_every_table_that_points_at_a_server_is_checked_before_it_is_removed():
    """Round-1 review of this milestone: the delete check read workflow_custom_steps and
    skill_definitions, and the agents stream's role_mcp_servers (an agent HOLDING a server)
    arrived beside it unseen — its FK cascades, so removing a server took it off every agent
    with nothing said. Every table whose column REFERENCES mcp_servers(id) in db/schema.sql is
    enumerated here, plus the one JSON reference (an authored mcp_tool skill's config), and
    each must be a declared referrer that mcp_server_references reads."""
    import inspect

    declared = {table for table, _column, _key in reg.MCP_REFERRERS}
    by_fk = _tables_referencing_mcp_servers()
    assert "workflow_custom_steps" in by_fk, "the schema scan is not finding the FKs"
    missing = (by_fk | {"skill_definitions"}) - declared
    assert not missing, f"add these to mcp_registry.MCP_REFERRERS: {sorted(missing)}"
    source = inspect.getsource(reg.mcp_server_references)
    for table, _column, key in reg.MCP_REFERRERS:
        assert f"FROM {table}" in source and f'"{key}"' in source, (
            f"mcp_server_references does not read {table}")


def test_one_memory_of_a_deleted_seed_server():
    """Round 1 kept two: an events-table scan here and seed_exclusions in the agents stream.
    The registry now writes and reads seed_exclusions only."""
    import inspect

    source = inspect.getsource(reg)
    assert "_removed_by_operator" not in source
    assert "event_type = 'mcp.server_removed'" not in source
    assert "seed_exclusions.exclude(" in inspect.getsource(reg.delete_mcp_server)
    assert "seed_exclusions.is_excluded(" in inspect.getsource(reg.ensure_mcp_server)
    assert "seed_exclusions.lift(" in inspect.getsource(reg.register_mcp_server)


def test_the_tier_rule_has_one_home():
    """Three copies of 'override, else blanket tier' drifted: one let a tool's own annotation
    replace the operator's tier, so a server could mark its tools tier 0."""
    import inspect

    from boss_engine.services import agent_loop, skill_catalogue, workflow_custom_steps

    assert "tool_tier(" in inspect.getsource(agent_loop._mcp_tool_risk)
    assert "tool_tier(" in inspect.getsource(skill_catalogue.mcp_tool_tier)
    assert "mcp_tool_tier(" in inspect.getsource(workflow_custom_steps.resolve_risk)


def test_a_tool_may_raise_its_tier_never_lower_it():
    server = {"risk_tier": 2, "tool_risk_overrides": {"read": 0}}
    assert reg.tool_tier(server, "delete", declared=0) == 2       # cannot lower
    assert reg.tool_tier(server, "delete", declared=3) == 3       # may raise
    assert reg.tool_tier(server, "read", declared=3) == 0         # the operator's override wins
    assert reg.tool_tier({"risk_tier": 0}, "read") == 0           # tier 0 is not falsy here


def test_a_server_that_just_failed_is_skipped_and_one_never_checked_is_not():
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    assert reg.known_unreachable({"reachable": False, "last_checked_at": now})
    assert not reg.known_unreachable({"reachable": None, "last_checked_at": None})
    assert not reg.known_unreachable({"reachable": False,
                                      "last_checked_at": "2020-01-01T00:00:00+00:00"})


def test_an_unreachable_server_says_what_to_do():
    note = reg.status_note({"enabled": True, "reachable": False,
                            "last_error": "ConnectError: refused"})
    assert "ConnectError" in note and "127.0.0.1 inside the container" in note
    assert reg.status_note({"enabled": True, "reachable": None}) is None


# --------------------------------------------------------------------------- #
# Behaviour, against a real database                                            #
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT reachable, tools FROM mcp_servers LIMIT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable or not migrated: {error}")
    return engine


@pytest.fixture()
def project(eng):
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
        if wid is None:
            pytest.skip("test database has no workspace")
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'mcp test', :s)"), {"i": pid, "w": wid, "s": pid})
    yield pid
    with eng.begin() as c:
        for table in ("workflow_custom_steps", "skill_definitions", "role_mcp_servers",
                      "seed_exclusions", "mcp_servers", "events"):
            c.execute(text(f"DELETE FROM {table} WHERE project_id=:p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


def _register(c, pid, name="tracker", url="http://tracker:9/mcp", **kw):
    return reg.register_mcp_server(c, pid, name, url, actor="operator", **kw)


def test_names_are_unique_whatever_the_case(eng, project):
    with eng.begin() as c:
        _register(c, project, "Tracker")
        with pytest.raises(reg.McpServerConflict) as error:
            _register(c, project, "tracker ")
    assert "already registered" in str(error.value)


def test_the_database_refuses_a_duplicate_name_too(eng, project):
    """In the schema as well as in code: two concurrent presses both pass a code check."""
    with eng.begin() as c:
        _register(c, project, "tracker")
    with eng.begin() as c, pytest.raises(Exception) as error:
        c.execute(text("INSERT INTO mcp_servers (id, project_id, name, url) "
                       "VALUES (:i, :p, 'TRACKER', 'http://x/mcp')"),
                  {"i": f"mcp_{uuid.uuid4().hex[:8]}", "p": project})
    assert "uq_mcp_servers_project_name" in str(error.value)


def test_an_edit_keeps_the_id_and_forgets_what_the_old_url_said(eng, project):
    with eng.begin() as c:
        server = _register(c, project)
        reg.record_reachability(c, project, server["id"], reachable=False, error="refused")
        edited = reg.update_mcp_server(c, project, server["id"], actor="operator",
                                       url="http://tracker.internal:9/mcp", risk_tier=0,
                                       tool_risk_overrides={"delete": 3}, trusted=True,
                                       description="Our tracker")
    assert edited["id"] == server["id"]
    assert edited["url"] == "http://tracker.internal:9/mcp" and edited["risk_tier"] == 0
    assert edited["reachable"] is None and edited["last_error"] is None
    assert edited["tool_risk_overrides"] == {"delete": 3}
    # A review with no reviewer on record is just a flag somebody set.
    assert edited["trusted"] and edited["provenance"]["reviewed_by"] == "operator"


def test_a_new_address_voids_the_review(eng, project):
    """Round-1 review: a reviewed server re-pointed at another host stayed "reviewed". A person
    vouched for what answered at the OLD address."""
    with eng.begin() as c:
        server = _register(c, project, provenance={"trusted": True})
        assert server["trusted"]
        renamed = reg.update_mcp_server(c, project, server["id"], actor="operator",
                                        description="still the same place")
        moved = reg.update_mcp_server(c, project, server["id"], actor="someone",
                                      url="http://elsewhere:9/mcp")
        again = reg.update_mcp_server(c, project, server["id"], actor="reviewer",
                                      url="http://third:9/mcp", trusted=True)
    assert renamed["trusted"], "an edit that did not move the server dropped its review"
    assert moved["trusted"] is False and "reviewed_by" not in moved["provenance"]
    assert moved["provenance"]["review_voided_by_url_change"] == "http://tracker:9/mcp"
    # Reviewed again in the same edit only by saying so, and stamped with who said it.
    assert again["trusted"] and again["provenance"]["reviewed_by"] == "reviewer"


def test_an_edit_cannot_take_another_servers_name_or_an_unknown_field(eng, project):
    with eng.begin() as c:
        _register(c, project, "tracker")
        other = _register(c, project, "wiki", "http://wiki:9/mcp")
        with pytest.raises(reg.McpServerConflict):
            reg.update_mcp_server(c, project, other["id"], actor="operator", name="Tracker")
        with pytest.raises(reg.McpServerError):
            reg.update_mcp_server(c, project, other["id"], actor="operator", id="x")


def test_delete_refuses_while_a_step_or_a_skill_calls_it(eng, project):
    from boss_engine.services import skill_definitions as sd
    from boss_engine.services import workflow_custom_steps as steps

    with eng.begin() as c:
        server = _register(c, project)
        steps.create(c, project_id=project, label="File an issue", binding_kind="mcp_tool",
                     mcp_server_id=server["id"], mcp_tool_name="create_issue", actor="t")
        sd.save_skill(c, project_id=project, actor="t", skill_key="file_ticket",
                      label="File ticket", behaviour_kind="mcp_tool",
                      config={"mcp_server_id": server["id"], "tool_name": "create_issue"})
        refs = reg.mcp_server_references(c, project, server["id"])
        with pytest.raises(reg.McpServerInUse) as error:
            reg.delete_mcp_server(c, project, server["id"], actor="operator")
    assert [s["step_key"] for s in refs["custom_steps"]] == ["file_an_issue"]
    assert [s["skill_key"] for s in refs["skills"]] == ["file_ticket"]
    assert "File an issue" in str(error.value) and "turn the server off" in str(error.value)


def test_an_agent_holding_a_server_is_named_and_released_only_on_purpose(eng, project):
    """Deleting a server an agent held cascaded the grant away silently (role_mcp_servers is
    ON DELETE CASCADE). Now the refusal names the agent, and release_grants takes it off them
    as a stated choice — the same rule as deleting a skill an agent holds."""
    with eng.begin() as c:
        server = _register(c, project)
        c.execute(text("INSERT INTO role_mcp_servers (id, project_id, role_key, mcp_server_id) "
                       "VALUES (:i, :p, 'support_agent', :s)"),
                  {"i": f"rms_{uuid.uuid4().hex[:8]}", "p": project, "s": server["id"]})
        refs = reg.mcp_server_references(c, project, server["id"])
        with pytest.raises(reg.McpServerInUse) as error:
            reg.delete_mcp_server(c, project, server["id"], actor="operator")
    assert [a["role_key"] for a in refs["agents"]] == ["support_agent"]
    assert "held by support_agent" in str(error.value)
    assert "confirm" in str(error.value)
    with eng.begin() as c:
        assert reg.delete_mcp_server(c, project, server["id"], actor="operator",
                                     release_grants=True)
        left = c.execute(text("SELECT count(*) FROM role_mcp_servers WHERE project_id=:p"),
                         {"p": project}).scalar()
        event = c.execute(text("SELECT data FROM events WHERE project_id=:p "
                               "AND event_type='mcp.server_removed'"), {"p": project}).scalar()
    assert left == 0
    assert (event if isinstance(event, dict) else json.loads(event))["released_from"] == [
        "support_agent"]


def test_a_reseed_respects_what_the_operator_did(eng, project):
    """The seed put 127.0.0.1 back over an operator's fixed URL, and recreated a server they
    had removed."""
    with eng.begin() as c:
        seeded = reg.ensure_mcp_server(c, project, "web-intel", "http://127.0.0.1:8770/mcp",
                                       respect_operator=True)
        reg.update_mcp_server(c, project, seeded["id"], actor="operator",
                              url="http://web-intel:8770/mcp")
        again = reg.ensure_mcp_server(c, project, "web-intel", "http://127.0.0.1:8770/mcp",
                                      respect_operator=True)
        doc = reg.ensure_mcp_server(c, project, "doc-render", "http://127.0.0.1:8771/mcp",
                                    respect_operator=True)
        reg.delete_mcp_server(c, project, doc["id"], actor="operator")
        gone = reg.ensure_mcp_server(c, project, "doc-render", "http://127.0.0.1:8771/mcp",
                                     respect_operator=True)
    assert again["url"] == "http://web-intel:8770/mcp"
    assert gone is None
    # Remembered in seed_exclusions — the record every seed path asks — and lifted when a person
    # registers the same name again by hand.
    from boss_engine.services import seed_exclusions

    with eng.begin() as c:
        assert seed_exclusions.is_excluded(c, project, "mcp_server", "doc-render")
        _register(c, project, "doc-render", "http://doc-render:8771/mcp")
        assert not seed_exclusions.is_excluded(c, project, "mcp_server", "doc-render")


def test_a_probe_is_recorded_and_a_fresh_one_is_not_repeated(eng, project, monkeypatch):
    from boss_engine.services.mcp_client import MCPClient

    asked = []

    def refuse(self, url, headers=None):
        asked.append(url)
        raise ConnectionError("refused")

    monkeypatch.setattr(MCPClient, "list_tools", refuse)
    with eng.begin() as c:
        server = _register(c, project)

    @contextmanager
    def begin():
        with eng.begin() as c:
            yield c

    first = reg.servers_with_tools(begin, Settings(), project)
    second = reg.servers_with_tools(begin, Settings(), project)
    assert len(asked) == 1, "a server checked a moment ago was asked again"
    assert first[0]["reachable"] is False and second[0]["reachable"] is False
    assert "Unreachable from the BOSS API" in second[0]["status_note"]
    assert second[0]["id"] == server["id"]

    monkeypatch.setattr(MCPClient, "list_tools",
                        lambda self, url, headers=None: [{"name": "create_issue",
                                                          "inputSchema": {"type": "object"}}])
    now = reg.servers_with_tools(begin, Settings(), project, max_age_seconds=0)
    assert now[0]["reachable"] is True
    assert [t["name"] for t in now[0]["tools"]] == ["create_issue"]
    # A failed probe afterwards keeps the last-known tools for the operator fixing it.
    with eng.begin() as c:
        reg.record_reachability(c, project, server["id"], reachable=False, error="down")
        kept = reg.get_mcp_server(c, project, server["id"])
    assert [t["name"] for t in kept["tools"]] == ["create_issue"]
    assert json.dumps(kept)  # serialisable for the API
