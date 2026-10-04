"""Allow / Ask / Off per tool, never below the floor (skills-mcp E7, test 8).

Granting is "may use"; the tier decides "must ask": a tier >= 2 tool always asks a person, so
it is safe to pre-grant write abilities. Off means not offered and refused by the effect itself.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

PROJECT = "boss-dogfood"


def _policy(client, ability, ref, policy):
    return client.post(f"/v1/kit/{ability}/policy", json={
        "project_id": PROJECT, "actor": "ceo", "tool_ref": ref, "policy": policy})


def test_allow_on_a_tier_two_tool_is_refused(client):
    kit = client.get("/v1/kit/open_prs", params={"project_id": PROJECT}).json()
    row = next(t for t in kit["tools"] if t["ref"] == "skill:github.create_pr_real")
    assert row["tier"] == 2 and row["policy"] == "ask" and row["floor"] == "ask"
    merge = next(t for t in kit["tools"] if t["ref"] == "skill:github.merge_pr_real")
    assert merge["policy"] == "off"
    r = _policy(client, "open_prs", "skill:github.create_pr_real", "allow")
    assert r.status_code == 409 and "always asks a person" in r.json()["detail"]


def test_off_is_not_offered_and_refused_by_the_effect(client, engine):
    from boss_engine.config import get_settings
    from boss_engine.services import mcp_registry
    from boss_engine.services.agent_loop import _mcp_tool_policy

    r = _policy(client, "web_research", "mcp:web-intel.study_site", "off")
    assert r.status_code == 200, r.text
    row = next(t for t in r.json()["tools"] if t["ref"] == "mcp:web-intel.study_site")
    assert row["policy"] == "off"
    with engine.begin() as c:
        server = mcp_registry.find_mcp_server(c, PROJECT, "web-intel")
        assert _mcp_tool_policy(server, {"name": "study_site"}) == "off"
        assert _mcp_tool_policy(server, {"name": "fetch_page"}) == "allow"
        from boss_engine.services.skill_executors import SkillExecutorContext

        scope = SkillExecutorContext(settings=get_settings(), connection=c, project_id=PROJECT,
                                     actor="ceo").tenant_scope()
        with pytest.raises(mcp_registry.McpToolOff):
            mcp_registry.call_tool_with_receipt(
                c, get_settings(), PROJECT, server, "study_site", {"url": "https://x"},
                dedupe_key="t-off", tenant_scope=scope, approved_by=None, approval_about=None)


def test_ask_needs_a_persons_approval_at_the_effect(client, engine):
    from boss_engine.config import get_settings
    from boss_engine.services import mcp_registry
    from boss_engine.services.skill_executors import SkillExecutorContext

    r = _policy(client, "web_research", "mcp:web-intel.fetch_page", "ask")
    assert r.status_code == 200
    with engine.begin() as c:
        server = mcp_registry.find_mcp_server(c, PROJECT, "web-intel")
        scope = SkillExecutorContext(settings=get_settings(), connection=c, project_id=PROJECT,
                                     actor="ceo").tenant_scope()
        with pytest.raises(mcp_registry.McpServerUnreviewed, match="ask a person"):
            mcp_registry.call_tool_with_receipt(
                c, get_settings(), PROJECT, server, "fetch_page", {"url": "https://x"},
                dedupe_key="t-ask", tenant_scope=scope, approved_by=None, approval_about=None)


def test_a_skill_turned_off_is_refused_by_the_runtime(client, engine):
    from boss_engine.config import get_settings
    from boss_engine.services.skill_runtime import SkillRuntimeService

    r = _policy(client, "render_docs", "skill:docs.draft", "off")
    assert r.status_code == 200
    with engine.begin() as c:
        got = SkillRuntimeService(get_settings()).invoke(
            c, PROJECT, "docs.draft", "pm", {"brief": "x"}, mock=False,
            executor=lambda inputs: {"ok": True}, actor="pm")
    assert got["status"] == "denied" and "turned off in Skills & MCP" in got["reason"]


def test_turning_an_ability_off_turns_its_tools_off(client, engine):
    # A failed test and an unreviewed server both outrank Off (spec §5.2 precedence), and
    # doc-render is not running here, so the earlier failure is cleared and the server reviewed
    # first: this test is about Off alone.
    from boss_engine.services import mcp_registry

    with engine.begin() as c:
        c.execute(text("DELETE FROM kit_tests WHERE project_id = :p AND ability = 'render_docs'"),
                  {"p": PROJECT})
        server = mcp_registry.find_mcp_server(c, PROJECT, "doc-render")
        if server is not None:
            mcp_registry.review_mcp_server(c, PROJECT, server["id"], actor="ceo")
    r = client.post("/v1/kit/render_docs/enabled",
                    json={"project_id": PROJECT, "actor": "ceo", "enabled": False})
    assert r.status_code == 200 and r.json()["status"] == "off"
    assert {t["policy"] for t in r.json()["tools"]} == {"off"}
    back = client.post("/v1/kit/render_docs/enabled",
                       json={"project_id": PROJECT, "actor": "ceo", "enabled": True})
    assert back.json()["status"] != "off"
    assert "off" not in {t["policy"] for t in back.json()["tools"]}
