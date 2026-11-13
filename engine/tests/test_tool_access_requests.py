"""BOSSAPP-848: an agent asks for MCP tools, a person grants exactly those.

request (the registry tool) -> a typed Grant card listing exactly the tools, their tiers and the
reason -> approval gives the role a holder grant for exactly those tools, Allow only for tier <= 1,
pinned to the schema hash the card showed -> a changed schema drops the grant (as BOSSAPP-818
quarantines a changed tool) -> a person can revoke it where holders are managed; a No is kept.
"""
from __future__ import annotations

import json
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import agents
from boss_engine.services import mcp_registry as reg
from boss_engine.services import tool_access_requests as tar
from boss_engine.services.decisions import DecisionService, InvalidDecisionTransitionError

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"),
)

READ = {"name": "read_issue", "description": "Reads an issue.",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}}}
CREATE = {"name": "create_issue", "description": "Files an issue.",
          "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}}}
CREATE_V2 = {**CREATE, "inputSchema": {"type": "object",
                                       "properties": {"title": {"type": "string"},
                                                      "assignee": {"type": "string"}}}}
DELETE = {"name": "delete_issue", "description": "Deletes an issue.",
          "inputSchema": {"type": "object"}}


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM mcp_access_grants LIMIT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable or not migrated: {error}")
    return engine


@pytest.fixture()
def project(eng):
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT id FROM workspaces LIMIT 1")).scalar()
        if wid is None:
            pytest.skip("test database has no workspace")
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'p29a 848', :s)"), {"i": pid, "w": wid, "s": pid})
        for role in ("pm", "backend_engineer"):
            c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name) "
                           "VALUES (:i, :p, :r, :r)"),
                      {"i": f"role_{uuid.uuid4().hex[:8]}", "p": pid, "r": role})
        c.execute(text(
            "INSERT INTO project_members (id, project_id, member_id, display_name, member_type, "
            "role_keys, permissions) VALUES (:i, :p, 'ceo', 'ceo', 'human', CAST(:r AS JSONB), "
            "CAST(:perm AS JSONB)) ON CONFLICT DO NOTHING"),
            {"i": f"pm_{pid}_ceo", "p": pid, "r": json.dumps(["ceo"]),
             "perm": json.dumps(["decision_resolve"])})
    yield pid
    with eng.begin() as c:
        for table in ("mcp_access_grants", "role_mcp_servers", "roles", "mcp_servers", "events",
                      "decisions", "project_members", "seed_exclusions", "agent_identities"):
            try:
                with c.begin_nested():
                    c.execute(text(f"DELETE FROM {table} WHERE project_id=:p"), {"p": pid})
            except Exception:  # noqa: BLE001
                pass
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


def _server(eng, project, *, overrides=None):
    with eng.begin() as c:
        server = reg.register_mcp_server(c, project, "tracker", "http://tracker:9/mcp",
                                         risk_tier=0, actor="operator",
                                         tool_risk_overrides=overrides or {"create_issue": 2,
                                                                           "delete_issue": 3})
        reg.review_mcp_server(c, project, server["id"], actor="operator")
        reg.record_reachability(c, project, server["id"], reachable=True,
                                tools=[READ, CREATE, DELETE])
    return server["id"]


def _ask(eng, project, tools, *, actor="agent:pm", reason="I need to file the bug I found."):
    from boss_engine.tools.builtin import build_default_registry

    spec = next(s for s in build_default_registry().specs(kind="write")
                if s.name == "request_tool_access")
    ctx = SimpleNamespace(begin=eng.begin, settings=None)
    return spec.handler(ctx, project_id=project, actor=actor,
                        args={"server": "tracker", "tools": tools, "reason": reason})


def _decide(eng, decision_id, action, note=None):
    from boss_engine.config import get_settings

    with eng.begin() as c:
        DecisionService(get_settings()).transition(c, decision_id, action=action, actor="ceo",
                                                   note=note)


def _card(eng, decision_id):
    with eng.begin() as c:
        return dict(c.execute(text("SELECT status, question, recommendation, risk_tier FROM "
                                   "decisions WHERE id = :i"), {"i": decision_id}).mappings().one())


def _role_tools(eng, project, sid, role="pm"):
    with eng.begin() as c:
        row = c.execute(text("SELECT tools FROM role_mcp_servers WHERE project_id = :p AND "
                             "role_key = :r AND mcp_server_id = :s"),
                        {"p": project, "r": role, "s": sid}).first()
    return None if row is None else sorted(row[0])


def _grants(eng, project, **kw):
    with eng.begin() as c:
        return tar.list_grants(c, project, include_ended=True, **kw)


def test_the_full_lifecycle_request_card_grant_pin_drop_revoke(eng, project):
    sid = _server(eng, project)
    asked = _ask(eng, project, ["create_issue", "read_issue"])
    assert asked["ok"] and asked["status"] == "asked"
    card = _card(eng, asked["decision_id"])
    rec = card["recommendation"]
    # THE GRANT CARD: exactly the tools asked for, each with its tier and the policy it would get.
    assert card["status"] == "pending" and rec["action"] == tar.ACTION and rec["card"] == "grant"
    assert {i["tool"]: (i["tier"], i["policy"]) for i in rec["tools"]} == {
        "create_issue": (2, "ask"), "read_issue": (0, "allow")}
    assert rec["role_key"] == "pm" and rec["reason"] == "I need to file the bug I found."
    assert "create_issue" in card["question"] and "read_issue" in card["question"]
    assert card["risk_tier"] >= 2
    assert _role_tools(eng, project, sid) is None                  # nothing granted yet
    _decide(eng, asked["decision_id"], "approve")
    # Exactly those tools, no others; tier-capped; pinned.
    assert _role_tools(eng, project, sid) == ["create_issue", "read_issue"]
    live = {g["tool"]: g for g in _grants(eng, project, server_id=sid)}
    assert live["read_issue"]["policy"] == "allow" and live["create_issue"]["policy"] == "ask"
    with eng.begin() as c:
        stored = {t["name"]: t for t in reg.get_mcp_server(c, project, sid)["tools"]}
    assert live["create_issue"]["schema_sha256"] == stored["create_issue"]["schema_sha256"]
    assert live["create_issue"]["decision_id"] == asked["decision_id"]
    with eng.begin() as c:
        assert agents.holds_mcp_tool(c, project, "pm", sid, "create_issue")
        assert not agents.holds_mcp_tool(c, project, "pm", sid, "delete_issue")
        # The runtime still asks for the tier-2 tool, whatever the grant (the floor).
        server = reg.get_mcp_server(c, project, sid)
        assert reg.tool_policy(server, "create_issue") == "ask"
    # A CHANGED SCHEMA DROPS THE GRANT before the tool is offered again.
    with eng.begin() as c:
        reg.record_reachability(c, project, sid, reachable=True, tools=[READ, CREATE_V2, DELETE])
    with eng.begin() as c:
        offered = agents.held_mcp_servers(c, project, "pm")
    assert [s["granted_tools"] for s in offered] == [["read_issue"]]
    by_tool = {g["tool"]: g for g in _grants(eng, project, server_id=sid)}
    assert by_tool["create_issue"]["status"] == "dropped"
    assert by_tool["create_issue"]["ended_reason"] == "schema_changed"
    # REVOKE where holders are managed: the last granted tool goes, and with it the holder row.
    with eng.begin() as c:
        tar.revoke_grant(c, project, by_tool["read_issue"]["id"], actor="ceo")
    assert _role_tools(eng, project, sid) is None
    with eng.begin() as c, pytest.raises(tar.ToolAccessError):
        tar.revoke_grant(c, project, by_tool["read_issue"]["id"], actor="ceo")


def test_a_tool_held_before_the_grant_stays_when_the_grant_ends(eng, project):
    sid = _server(eng, project)
    with eng.begin() as c:
        agents.grant_mcp(c, project, actor="operator", role_key="pm", mcp_server_id=sid,
                         tools=["read_issue"])
    asked = _ask(eng, project, ["read_issue", "create_issue"])
    assert [i["tool"] for i in _card(eng, asked["decision_id"])["recommendation"]["tools"]] == \
        ["create_issue"]                                  # only what it does not already hold
    _decide(eng, asked["decision_id"], "approve")
    assert _role_tools(eng, project, sid) == ["create_issue", "read_issue"]
    [grant] = [g for g in _grants(eng, project, server_id=sid) if g["status"] == "active"]
    with eng.begin() as c:
        tar.revoke_grant(c, project, grant["id"], actor="ceo")
    assert _role_tools(eng, project, sid) == ["read_issue"]
    assert _ask(eng, project, ["read_issue"])["status"] == "already_held"


def test_a_refusal_is_recorded_and_not_asked_again_for_a_day(eng, project):
    sid = _server(eng, project)
    asked = _ask(eng, project, ["delete_issue"])
    assert _card(eng, asked["decision_id"])["recommendation"]["tools"][0]["policy"] == "ask"
    _decide(eng, asked["decision_id"], "reject", note="not this one")
    assert _role_tools(eng, project, sid) is None
    [row] = _grants(eng, project, server_id=sid)
    assert row["status"] == "refused" and row["ended_by"] == "ceo"
    again = _ask(eng, project, ["delete_issue"])
    assert again["ok"] is False and again["status"] == "refused_recently"
    assert again["decision_id"] == asked["decision_id"]


def test_a_tool_that_changed_while_the_card_waited_refuses_the_approval(eng, project):
    sid = _server(eng, project)
    asked = _ask(eng, project, ["create_issue"])
    with eng.begin() as c:
        reg.record_reachability(c, project, sid, reachable=True, tools=[READ, CREATE_V2, DELETE])
    with pytest.raises(InvalidDecisionTransitionError, match="changed since the agent asked"):
        _decide(eng, asked["decision_id"], "approve")
    assert _card(eng, asked["decision_id"])["status"] == "pending"
    assert _role_tools(eng, project, sid) is None


def test_the_tier_cap_asks_for_anything_above_tier_one(eng, project):
    _server(eng, project, overrides={"read_issue": 1, "create_issue": 3, "delete_issue": 3})
    asked = _ask(eng, project, ["read_issue", "create_issue"])
    tools = {i["tool"]: i for i in _card(eng, asked["decision_id"])["recommendation"]["tools"]}
    assert tools["read_issue"]["policy"] == "allow" and tools["create_issue"]["policy"] == "ask"
    assert tar.policy_for_tier(0) == tar.policy_for_tier(1) == "allow"
    assert tar.policy_for_tier(2) == tar.policy_for_tier(3) == "ask"


def test_only_an_agent_may_ask_and_only_for_tools_the_server_has(eng, project):
    _server(eng, project)
    assert _ask(eng, project, ["read_issue"], actor="ceo")["ok"] is False
    unknown = _ask(eng, project, ["drop_database"])
    assert unknown["ok"] is False and "drop_database" in unknown["error"]
    assert _ask(eng, project, [])["ok"] is False
    assert _ask(eng, project, ["read_issue"], reason="pls")["ok"] is False
    from boss_engine.services.connection_credentials import acting_agent

    with acting_agent("backend_engineer"):         # the run's agent wins over the actor's name
        asked = _ask(eng, project, ["read_issue"], actor="agent:pm")
    assert _card(eng, asked["decision_id"])["recommendation"]["role_key"] == "backend_engineer"


def test_the_approval_arm_is_declared_and_extends_the_handler():
    import inspect

    from boss_engine.services.decision_actions import CARRIED_OUT_ON_APPROVAL

    assert tar.ACTION in CARRIED_OUT_ON_APPROVAL
    approved = inspect.getsource(DecisionService._handle_approved_follow_on)
    rejected = inspect.getsource(DecisionService._handle_rejected_follow_on)
    assert '"grant_tool_access"' in approved and '"grant_tool_access"' in rejected
    # The other arms are still there (extended, not rewritten).
    for arm in ("review_tool_change", "answer_form", "approve_change_set", "execution_stage"):
        assert f'"{arm}"' in approved


def test_removing_the_server_takes_its_grants_with_it(eng, project):
    sid = _server(eng, project)
    asked = _ask(eng, project, ["read_issue"])
    _decide(eng, asked["decision_id"], "approve")
    with eng.begin() as c:
        refs = reg.mcp_server_references(c, project, sid)
        assert [g["tool"] for g in refs["access_grants"]] == ["read_issue"]
        assert reg.delete_mcp_server(c, project, sid, "operator", release_grants=True)
    assert _grants(eng, project, server_id=sid) == []
