"""BOSSAPP-1041: catalogue suggested limits must show for an installed catalogue server.

Matching only ``provenance.catalogue`` / ``provenance.connector_key`` returns [] for a server
that was installed from the catalogue but stored without that key (publisher id, name or URL
still identify the entry). Nothing is applied; an existing live rule for the same effect,
server and tool is left out.
"""
from __future__ import annotations

from boss_engine.services import tool_policy_engine as tpe


def _rules(monkeypatch, servers, existing=None):
    monkeypatch.setattr(tpe, "list_rules", lambda *_a, **_k: list(existing or []))
    monkeypatch.setattr(
        "boss_engine.services.mcp_registry.list_mcp_servers",
        lambda *_a, **_k: list(servers),
    )
    return tpe.suggested_rules(object(), "p1")


def test_an_installed_github_server_suggests_its_catalogue_limit_without_the_catalogue_key(monkeypatch):
    found = _rules(monkeypatch, [{
        "id": "mcp_gh",
        "name": "github",
        "url": "https://api.githubcopilot.com/mcp/",
        "publisher": {"id": "github", "name": "GitHub", "kind": "vendor"},
        "provenance": {"source": "catalogue"},
    }])
    assert found, "a catalogue GitHub server must suggest its create_* limit"
    body = found[0]["rule"]
    assert found[0]["catalogue"] == "github"
    assert body["selector"] == {"server": "github", "tool": "create_*"}
    assert body["effect"] == "rate_limit"
    assert body["rate_limit"]["max_calls"] == 30
    assert body["rate_limit"]["bucket"] == "run"


def test_a_server_matched_only_by_url_prefers_the_entry_that_has_suggested_rules(monkeypatch):
    found = _rules(monkeypatch, [{
        "id": "mcp_9",
        "name": "GitHub MCP",
        "url": "https://api.githubcopilot.com/mcp",
        "publisher": {},
        "provenance": {"source": "custom"},
    }])
    assert [s["catalogue"] for s in found] == ["github"]
    assert found[0]["rule"]["selector"]["tool"] == "create_*"


def test_github_added_from_the_connector_catalog_still_gets_the_mcp_limit(monkeypatch):
    """The connector GitHub entry shares the MCP URL and has no suggested_rules of its own.
    The Rules tab still offers the catalogue limit (30 create_* an hour per run)."""
    found = _rules(monkeypatch, [{
        "id": "mcp_gh",
        "name": "GitHub",
        "url": "https://api.githubcopilot.com/mcp/",
        "provenance": {"source": "connector_catalog", "connector_key": "github"},
    }])
    assert [s["catalogue"] for s in found] == ["github"]
    assert found[0]["rule"]["selector"] == {"server": "GitHub", "tool": "create_*"}
    assert found[0]["rule"]["rate_limit"]["max_calls"] == 30


def test_a_hand_added_server_suggests_nothing(monkeypatch):
    found = _rules(monkeypatch, [{
        "id": "mcp_notes",
        "name": "notes",
        "url": "https://notes.example/mcp",
        "publisher": {"id": "notes"},
        "provenance": {"source": "custom"},
    }])
    assert found == []


def test_a_live_rule_for_the_same_tool_hides_the_suggestion(monkeypatch):
    found = _rules(monkeypatch, [{
        "id": "mcp_gh",
        "name": "github",
        "url": "https://api.githubcopilot.com/mcp/",
        "provenance": {"catalogue": "github", "source": "catalogue"},
    }], existing=[{
        "status": "active",
        "effect": "rate_limit",
        "selector": {"server": "github", "tool": "create_*"},
    }])
    assert found == []


def test_a_connector_key_still_offers_that_connector_limit(monkeypatch):
    found = _rules(monkeypatch, [{
        "id": "mcp_c",
        "name": "my-composio",
        "url": "https://backend.composio.dev/v3/mcp/abc?user_id=u",
        "provenance": {"source": "connector_catalog", "connector_key": "composio"},
    }])
    assert len(found) == 1 and found[0]["catalogue"] == "composio"
    assert found[0]["rule"]["selector"] == {"server": "my-composio", "tool": "COMPOSIO_MULTI_EXECUTE_TOOL"}
