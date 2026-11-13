# ruff: noqa: F811 — pytest fixtures imported by name and used as arguments
"""BOSSAPP-870: MCP aggregators (Composio, Arcade, Zapier, Pipedream) in the connector catalog.

  * each is a valid connector-catalog entry in `automation`, with its auth quirk in its setup
    words and every app's tools at tier 2 (Ask) by default;
  * Composio's meta-tools get their own tiers by name: search/schemas read (0), execute and
    connection management ask (2), the remote workbench/shell are tier 3 and Off;
  * `aggregator.toolkit_pattern` turns an app ("toolkit") into a rule selector, so "deny every
    Gmail tool on Composio" is one tool-policy rule that the engine decides as written;
  * `suggested_rules` (BOSSAPP-868) reach the Rules tab for a server added from the connector
    catalog, and a malformed aggregator block or suggestion is refused by the validator.
"""
from __future__ import annotations

import copy

import pytest

from boss_engine.services import connector_catalog as cc
from boss_engine.services import tool_policy_engine as tpe
from .test_tool_policy_plug_points_815 import eng, world  # noqa: F401 — fixtures

AGGREGATORS = ("composio", "arcade", "zapier", "pipedream")


def _defs():
    return {d["key"]: d for d in cc.definitions()}


def test_the_four_aggregators_are_valid_catalog_entries():
    defs = _defs()
    for key in AGGREGATORS:
        d = defs[key]
        assert cc.validate_definition(d) == [], key
        assert d["category"] == "automation" and d["transport"] == "http"
        assert d["risk"]["default_tier"] == 2, key  # every app's tools ask by default
        assert d["aggregator"]["toolkit_pattern"], key
        assert d.get("suggested_rules"), key
    view = cc.catalog_view(None, project_id="p1", category="automation")
    shown = {i["key"]: i for i in view["items"]}
    assert set(AGGREGATORS) <= set(shown)
    assert shown["composio"]["aggregator"]["execute_tools"] == ["COMPOSIO_MULTI_EXECUTE_TOOL"]


def test_auth_quirks_are_in_the_prefill_and_the_setup_words():
    defs = _defs()
    composio = cc.prefill(defs["composio"], "p1")
    assert composio["auth"] == {"type": "header", "header_name": "x-api-key"}
    assert "user" in defs["composio"]["setup"].lower()
    assert "Arcade-User-ID" in defs["arcade"]["setup"]
    assert "x-pd-external-user-id" in defs["pipedream"]["setup"]
    assert "expires" in defs["pipedream"]["setup"]


def test_composio_meta_tools_get_their_own_tiers():
    d = _defs()["composio"]
    tools = ["COMPOSIO_SEARCH_TOOLS", "COMPOSIO_GET_TOOL_SCHEMAS", "COMPOSIO_MULTI_EXECUTE_TOOL",
             "COMPOSIO_MANAGE_CONNECTIONS", "COMPOSIO_REMOTE_BASH_TOOL", "COMPOSIO_REMOTE_WORKBENCH",
             "GMAIL_SEND_EMAIL"]
    got = cc.tool_overrides(d, tools)
    assert got["tiers"] == {"COMPOSIO_SEARCH_TOOLS": 0, "COMPOSIO_GET_TOOL_SCHEMAS": 0,
                            "COMPOSIO_MULTI_EXECUTE_TOOL": 2, "COMPOSIO_MANAGE_CONNECTIONS": 2,
                            "COMPOSIO_REMOTE_BASH_TOOL": 3, "COMPOSIO_REMOTE_WORKBENCH": 3}
    assert got["policies"] == {"COMPOSIO_REMOTE_BASH_TOOL": "off",
                               "COMPOSIO_REMOTE_WORKBENCH": "off"}
    assert "GMAIL_SEND_EMAIL" not in got["tiers"]  # an app's tool keeps the server's tier 2


def test_a_toolkit_becomes_one_rule_selector():
    defs = _defs()
    assert cc.toolkit_selector(defs["composio"], "composio", "gmail") == {
        "server": "composio", "tool": "GMAIL_*"}
    assert cc.toolkit_glob(defs["arcade"], "Gmail") == "Gmail_*"
    assert cc.toolkit_glob(defs["pipedream"], "Google_Sheets") == "google_sheets-*"
    assert cc.toolkit_glob(defs["zapier"], "slack") == "slack_*"
    assert cc.toolkit_glob(defs["github"], "x") is None  # not an aggregator
    assert cc.toolkit_glob(defs["composio"], "../*") is None  # no glob or path smuggled in
    assert cc.toolkit_glob(defs["composio"], "gm*ail") == "GMAIL_*"
    rule = {"id": "deny_gmail", "scope": "project", "priority": 1, "effect": "deny",
            "status": "active", "conditions": [],
            "selector": cc.toolkit_selector(defs["composio"], "composio", "gmail")}
    ctx = lambda tool: tpe.PolicyContext(server="composio", tool=tool)  # noqa: E731
    assert tpe.decide(ctx("GMAIL_SEND_EMAIL"), rules=[rule])["decision"] == "deny"
    assert tpe.decide(ctx("SLACK_SEND_MESSAGE"), rules=[rule])["decision"] == "allow"


@pytest.mark.parametrize("mutate,field", [
    (lambda d: d["aggregator"].update(toolkit_pattern="GMAIL_*"), "aggregator.toolkit_pattern"),
    (lambda d: d["aggregator"].update(toolkit_pattern="{TOOLKIT}_{toolkit}*"),
     "aggregator.toolkit_pattern"),
    (lambda d: d["aggregator"].update(execute_tools="X"), "aggregator.execute_tools"),
    (lambda d: d.update(suggested_rules=[{"tool": "*", "effect": "rate_limit"}]),
     "suggested_rules[0]"),
    (lambda d: d.update(suggested_rules=[{"effect": "deny"}]), "suggested_rules[0]"),
])
def test_a_bad_aggregator_block_is_refused(mutate, field):
    d = copy.deepcopy(_defs()["composio"])
    mutate(d)
    assert any(field in p for p in cc.validate_definition(d)), cc.validate_definition(d)


def test_a_connector_catalog_server_gets_its_suggested_limits(world):
    from sqlalchemy import text

    w = world
    with w.eng.begin() as c:
        c.execute(text("UPDATE mcp_servers SET provenance = provenance || "
                       "'{\"source\": \"connector_catalog\", \"connector_key\": \"composio\"}'::jsonb "
                       "WHERE id = :id"), {"id": w.sid})
        found = tpe.suggested_rules(c, w.pid)
    assert len(found) == 1 and found[0]["catalogue"] == "composio"
    assert found[0]["rule"]["selector"] == {"server": w.name, "tool": "COMPOSIO_MULTI_EXECUTE_TOOL"}
    assert found[0]["rule"]["rate_limit"] == {"max_calls": 60, "window_seconds": 3600,
                                              "bucket": "run"}
