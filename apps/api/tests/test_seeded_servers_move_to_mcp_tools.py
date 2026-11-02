"""An upgraded install's seeded MCP servers move to mcp-tools (skills-mcp spec E3, J11): rows the
release seeded at 127.0.0.1 and nobody touched get the served address, reviewed as first-party;
a server an operator re-pointed keeps its address; one an operator removed stays removed."""
from __future__ import annotations

import json

from sqlalchemy import text

PROJECT = "boss-dogfood"


def _row(c, name):
    return c.execute(text("SELECT url, risk_tier, provenance FROM mcp_servers "
                          "WHERE project_id=:p AND name=:n"), {"p": PROJECT, "n": name}
                     ).mappings().first()


def test_untouched_seeds_move_and_edited_ones_stay(engine):
    from boss_engine.bootstrap import BUILTIN_MCP_SERVERS, migrate_builtin_mcp_servers

    wanted = {s["name"]: s["url"] for s in BUILTIN_MCP_SERVERS}
    with engine.begin() as c:
        c.execute(text("UPDATE mcp_servers SET url='http://127.0.0.1:8770/mcp', risk_tier=2, "
                       "provenance=CAST(:p AS JSONB) WHERE project_id=:pr AND name='web-intel'"),
                  {"p": json.dumps({"source": "builtin", "version": "1", "trusted": True}),
                   "pr": PROJECT})
        c.execute(text("UPDATE mcp_servers SET url='http://10.0.0.5:8771/mcp', "
                       "provenance=CAST(:p AS JSONB) WHERE project_id=:pr AND name='doc-render'"),
                  {"p": json.dumps({"source": "builtin_edited", "edited_by": "ceo",
                                    "trusted": False}), "pr": PROJECT})
        moved = migrate_builtin_mcp_servers(c, PROJECT)
        web = _row(c, "web-intel")
        doc = _row(c, "doc-render")
    assert "web-intel" in moved and "doc-render" not in moved
    assert web["url"] == wanted["web-intel"] and web["risk_tier"] == 0
    assert web["provenance"]["reviewed_by"] == "boss (first-party)"
    assert web["provenance"]["seeded_url_before"] == "http://127.0.0.1:8770/mcp"
    assert doc["url"] == "http://10.0.0.5:8771/mcp"


def test_a_removed_seed_is_not_brought_back(engine):
    from boss_engine.services import shipped_cast
    from boss_engine.services.mcp_registry import delete_mcp_server, find_mcp_server

    with engine.begin() as c:
        server = find_mcp_server(c, PROJECT, "boss-devops-reference")
        delete_mcp_server(c, PROJECT, server["id"], actor="ceo", release_grants=True)
        shipped_cast._ensure_builtin_mcp_servers(c, PROJECT)
        assert find_mcp_server(c, PROJECT, "boss-devops-reference") is None
        # Put it back for the rest of the suite (mcp_servers outlives a test here).
        c.execute(text("DELETE FROM seed_exclusions WHERE project_id=:p AND kind='mcp_server' "
                       "AND subject='boss-devops-reference'"), {"p": PROJECT})
        shipped_cast._ensure_builtin_mcp_servers(c, PROJECT)


def test_a_renamed_builtin_keeps_its_row_and_its_grants(engine):
    """A release that renames a built-in server renames the project's row in place: same id, so
    every role's grant of it holds, and no second server is seeded beside it."""
    from boss_engine.bootstrap import RENAMED_BUILTIN_MCP_SERVERS, migrate_builtin_mcp_servers
    from boss_engine.services.mcp_registry import find_mcp_server

    old, new = next(iter(RENAMED_BUILTIN_MCP_SERVERS.items()))
    with engine.begin() as c:
        server = find_mcp_server(c, PROJECT, new)
        assert server is not None
        c.execute(text("UPDATE mcp_servers SET name = :old WHERE id = :id"),
                  {"old": old, "id": server["id"]})
        migrate_builtin_mcp_servers(c, PROJECT)
        renamed = find_mcp_server(c, PROJECT, new)
        assert renamed is not None and renamed["id"] == server["id"]
        assert find_mcp_server(c, PROJECT, old) is None
        assert c.execute(text("SELECT count(*) FROM mcp_servers WHERE project_id = :p "
                              "AND lower(name) IN (:o, :n)"),
                         {"p": PROJECT, "o": old, "n": new}).scalar() == 1
