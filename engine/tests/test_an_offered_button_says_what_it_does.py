"""An offered action's button says what pressing it does, whichever path offered it.

The exam (2026-10-24): set up three projects from boss-dogfood; of three held-write buttons, two
carried the model's labels and the first read "set project setting" — no project, no key, no
value. The fallback was the tool's name in words. Now every path that turns a call into a button
(propose_action, the write gate, the router's button text, a proposal recovered from prose) falls
back to services/offer_label: the tool, the project it acts in, and its arguments.
"""
from __future__ import annotations

import types

from boss_engine.config import get_settings
from boss_engine.services import intent_router
from boss_engine.services.offer_label import button_label, fallback_label
from boss_engine.tools import build_discuss_read_registry
from boss_engine.tools.registry import ToolRegistry, ToolSpec

CALL = {"project": "northpeak-bank-a", "key": "knowledge_graph", "value": "off"}
SAID = "Set project setting in northpeak-bank-a: knowledge_graph = off"


def test_the_fallback_names_the_tool_the_project_and_the_arguments():
    assert fallback_label("set_project_setting", CALL) == SAID
    assert fallback_label("register_repository",
                          {"project": "northpeak-bank-b", "repository": "paramg/northpeak-fineract-b",
                           "base_branch": "main"}) == \
        "Register repository in northpeak-bank-b: paramg/northpeak-fineract-b, main"
    assert fallback_label("allow_repository", {"repository": "acme/api", "allowed": False}) == \
        "Allow repository: acme/api, allowed: no"
    # internal and empty arguments never reach the button; it is never blank and never too long
    assert fallback_label("redeploy", {"_writes_allowed": True, "run_id": ""}) == "Redeploy"
    assert fallback_label("", {}).strip()
    assert len(fallback_label("draft_workflow", {"title": "x" * 300, "notes": "y"})) <= 90


def test_a_label_that_is_only_the_tool_name_is_replaced_and_a_real_one_is_kept():
    for bare in (None, "", "  ", "set project setting", "set_project_setting", "Set Project Setting"):
        assert button_label(bare, "set_project_setting", CALL) == SAID, bare
    assert button_label("Turn the knowledge graph off in bank A", "set_project_setting", CALL) == \
        "Turn the knowledge graph off in bank A"


def test_a_held_write_carries_a_label_and_the_router_shows_it():
    """The write gate (allow_writes=False) held the call with no label at all; the router then
    printed the tool name. Both ends are covered: the held proposal is labelled from its call,
    and the router's button text reads it."""
    from boss_engine.agent.engine_agent import EngineAgent

    reg = ToolRegistry()
    reg.register(ToolSpec("set_project_setting", "s", {"type": "object"},
                          lambda ctx, **k: {"ok": True}, kind="write",
                          permission="settings:update"))
    agent = EngineAgent(types.SimpleNamespace(settings=get_settings()), registry=reg,
                        adapter=object())
    pending: list = []
    out = agent._run_tool("set_project_setting", dict(CALL), project_id="p", actor="u",
                          allow_writes=False, pending=pending)
    assert out["status"] == "confirmation_required"
    assert out["proposed"]["label"] == SAID
    assert pending == [{"tool": "set_project_setting", "args": CALL}]
    step = {"tool": "set_project_setting", "args": CALL}
    assert intent_router._button_label(out, step) == SAID
    # a held write with no label at all (an older shape) is said from its arguments too
    assert intent_router._button_label({"proposed": {"tool": step["tool"], "args": CALL}}, step) \
        == SAID


def test_propose_action_without_a_usable_label_says_the_call():
    spec = build_discuss_read_registry().get("propose_action")
    ctx = types.SimpleNamespace(settings=get_settings())
    out = spec.handler(ctx, project_id="p", actor="u",
                       args={"tool": "set_project_setting", "args": CALL,
                             "label": "set project setting"})
    assert out["proposed"] is True and out["label"] == SAID, out


def test_a_proposal_recovered_from_prose_is_labelled_from_its_call():
    reply = ('Here you go: {"tool": "set_project_setting", "args": {"project": "northpeak-bank-a", '
             '"key": "knowledge_graph", "value": "off"}}')
    found, _rest = intent_router._recover_printed_actions(reply, {"set_project_setting"}, [])
    assert [f["label"] for f in found] == [SAID]
