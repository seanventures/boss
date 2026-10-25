"""kg_propose_rule only quarantines a proposal, so the write gate must run it in-turn.

A proposed rule changes nothing until a person approves it in Rules review — the same shape as
file_platform_gap and record_learning, which declare queues_for_review=True so the write gate lets
them run even with allow_writes=False. register_knowledge_tools
(engine/boss_engine/tools/knowledge_tools.py) registers kg_propose_rule as a safe write but WITHOUT
queues_for_review=True, so EngineAgent._run_tool's write gate
(`spec.kind == "write" and not allow_writes and not spec.queues_for_review`, engine_agent.py:970)
returns status='confirmation_required' for it on a looking-only chat turn — an in-chat confirmation
on top of the separate human Rules-review approval (double approval).

These tests pin the registration (base registry AND the chat/discuss rebuild), pin the gate
behaviour, and pin the invariants a fix must not widen: kg_propose_rule stays a safe write keyed to
knowledge:read that still needs a person's approval, the read tool kg_rules stays a plain read, and
the write gate still holds an ordinary unqueued write.
"""
from __future__ import annotations

import types

from boss_engine.agent.engine_agent import EngineAgent
from boss_engine.tools import build_default_registry
from boss_engine.tools.builtin import build_discuss_read_registry
from boss_engine.tools.registry import ToolRegistry, ToolSpec

# Neutral fixture: a business rule found in an example app, with real-looking evidence. The gate is
# reached before the handler runs, so these args only have to be well-formed, never executed.
VALID_PROPOSAL = {
    "title": "A transfer over the daily limit is rejected",
    "statement": "A transfer whose amount exceeds the account's daily limit is rejected.",
    "category": "limit",
    "evidence": [{"symbol": "example_app.transfers.check_daily_limit",
                  "lines": [10, 20], "excerpt": "if amount > daily_limit: reject(transfer)"}],
    "confidence": 0.5,
}


def _agent(registry: ToolRegistry) -> EngineAgent:
    return EngineAgent(types.SimpleNamespace(), registry=registry, adapter=object())


def test_register_knowledge_tools_makes_kg_propose_rule_a_queued_write():
    """register_knowledge_tools must declare kg_propose_rule as a safe write that only queues for a
    person — in the base registry and in the chat/discuss rebuild that admits it."""
    base = build_default_registry()
    discuss = build_discuss_read_registry()
    propose = base.get("kg_propose_rule")
    rules = base.get("kg_rules")

    # THE BUG: without queues_for_review the write gate forces an in-chat confirmation on a turn
    # that may not write, on top of the separate human Rules-review approval.
    assert propose.queues_for_review is True, (
        "register_knowledge_tools in knowledge_tools.py must register kg_propose_rule with "
        "queues_for_review=True (it only quarantines a proposal that changes nothing until a "
        "person approves it in Rules review), so the engine_agent write gate runs it in-turn "
        "instead of returning status='confirmation_required'")

    # ...and the chat/discuss registry rebuild (build_discuss_read_registry) must preserve it, or
    # the proposal would come back as a button on every looking-only question.
    assert discuss.get("kg_propose_rule").queues_for_review is True, (
        "the chat/discuss rebuild must keep register_knowledge_tools' queues_for_review=True on "
        "kg_propose_rule")

    # INVARIANTS a fix must not widen: it stays a SAFE WRITE keyed to knowledge:read, and the rule
    # still needs a person's approval (the description says so, and the tool cannot approve it).
    assert propose.kind == "write", "kg_propose_rule must stay a write"
    assert propose.reversibility == "safe", "kg_propose_rule must stay a safe write"
    assert propose.permission == "knowledge:read", "kg_propose_rule must not widen its permission"
    assert "until a person approves" in propose.description
    assert "you cannot approve it" in propose.description

    # INVARIANT: the read tool beside it must not become a queued write — the gate's
    # `not spec.queues_for_review` branch is only for a write.
    assert rules.kind == "read", "kg_rules must stay a read"
    assert rules.queues_for_review is False, "the read tool kg_rules must not become a queued write"


def test_the_write_gate_runs_kg_propose_rule_in_turn_with_allow_writes_false():
    """Gate-only: over the REAL kg_propose_rule spec, with registry.invoke monkeypatched to a
    sentinel so no identity/DB is touched, the write gate must let the call through to invoke even
    with allow_writes=False — returning the sentinel, not status='confirmation_required'."""
    SENTINEL = {"sentinel": "the write gate let kg_propose_rule reach invoke"}
    agent = _agent(build_default_registry())
    agent.registry.invoke = lambda *a, **k: SENTINEL  # the handler's own output is not under test

    out = agent._run_tool("kg_propose_rule", dict(VALID_PROPOSAL),
                          project_id="p", actor="u", allow_writes=False, pending=[])

    assert out is SENTINEL, (
        "register_knowledge_tools must set queues_for_review=True on kg_propose_rule so "
        "engine_agent's write gate runs it in an ordinary turn (reaching invoke) instead of "
        "returning status='confirmation_required'; got: " + repr(out))
    assert out.get("status") != "confirmation_required"


def test_the_write_gate_still_holds_an_ordinary_unqueued_write():
    """INVARIANT: a plain write that does NOT queue for a person is still held behind the gate when
    allow_writes=False — a fix must not disable the write gate wholesale."""
    reg = ToolRegistry()
    reg.register(ToolSpec("plain_write", "an ordinary write", {"type": "object"},
                          lambda ctx, **k: {"ok": True}, kind="write",
                          permission="settings:update"))

    out = _agent(reg)._run_tool("plain_write", {}, project_id="p", actor="u",
                                allow_writes=False, pending=[])

    assert out.get("status") == "confirmation_required", (
        "the write gate must still hold an ordinary unqueued write when allow_writes=False")
