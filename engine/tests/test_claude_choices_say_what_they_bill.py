"""Every Claude choice in Repo and models says what it bills, and the order is the operator's.

Observed on boss-1, 2026-11-03: the Claude subscription and the Anthropic API key both map to the
claude_code engine, and the job menus keep one entry per engine — so they showed a single
"Claude Code" that ran on whichever credential claude_credential's order picked. "API key first"
had been set with no control to see or undo it; a day of builds billed the key ($112) while the
subscription sat idle, and the operator could not put one job on the subscription and another on
the key. Now the claude_code choice is labelled and priced by the credential it really uses, the
Agent SDK engine (always the key) is offered beside it as "Claude · API key", and the order has
its own action.
"""
from __future__ import annotations

import pytest

from boss_engine.services import claude_credential, setup_actions, setup_view


def _entries():
    return {"claude_code": {"label": "Claude Code", "kind": "subscription", "model": None},
            "claude_agent_sdk": {"label": "Claude Agent SDK", "kind": "metered",
                                 "model": "claude-sonnet-5"},
            "codex": {"label": "Codex", "kind": "subscription", "model": None}}


@pytest.mark.parametrize("active,label,kind", [
    ("subscription", setup_view.CLAUDE_ON_SUBSCRIPTION, "subscription"),
    ("cli_login", setup_view.CLAUDE_ON_SUBSCRIPTION, "subscription"),
    ("stored_key", setup_view.CLAUDE_CODE_ON_KEY, "metered"),
    ("env_key", setup_view.CLAUDE_CODE_ON_KEY, "metered"),
])
def test_claude_code_says_which_credential_it_runs_on(monkeypatch, active, label, kind):
    monkeypatch.setattr(claude_credential, "describe",
                        lambda c, s, p: {"active": active, "order": "api_key_first"})
    by_engine = _entries()
    setup_view._say_which_claude_credential(None, object(), "p", by_engine)
    assert (by_engine["claude_code"]["label"], by_engine["claude_code"]["kind"]) == (label, kind)
    assert by_engine["claude_agent_sdk"]["label"] == setup_view.CLAUDE_SDK_ON_KEY
    assert by_engine["claude_agent_sdk"]["kind"] == "metered"
    assert by_engine["codex"]["label"] == "Codex", "other engines are untouched"


def test_both_claude_choices_are_offered_side_by_side(monkeypatch):
    """Subscription and key at once: each job can take either."""
    monkeypatch.setattr(claude_credential, "describe",
                        lambda c, s, p: {"active": "subscription"})
    by_engine = _entries()
    setup_view._say_which_claude_credential(None, object(), "p", by_engine)
    labels = {e["label"] for e in by_engine.values()}
    assert {setup_view.CLAUDE_ON_SUBSCRIPTION, setup_view.CLAUDE_SDK_ON_KEY} <= labels


def test_a_describe_failure_leaves_the_menus_listing_what_works(monkeypatch):
    def boom(*a):
        raise RuntimeError("db")

    monkeypatch.setattr(claude_credential, "describe", boom)
    by_engine = _entries()
    setup_view._say_which_claude_credential(None, object(), "p", by_engine)
    assert by_engine["claude_code"]["label"] == "Claude Code"


def test_the_order_is_saved_and_recorded(monkeypatch):
    from boss_engine.services import core, project_settings

    wrote, events = [], []
    monkeypatch.setattr(project_settings, "set_one",
                        lambda c, p, k, v, a, scope=None: wrote.append((k, v, scope)))
    monkeypatch.setattr(core, "emit_event", lambda *a, **k: events.append(a[2:5]))
    out = setup_actions.set_claude_credential_order(None, object(), "p", "subscription_first",
                                                    actor="ceo")
    assert out == {"order": "subscription_first"}
    assert wrote == [("anthropic_credential_order", "subscription_first", None)]
    assert events and events[0][0] == "setup.claude_credential_order"


@pytest.mark.parametrize("order,scope", [("both", None), ("api_key_first", "team")])
def test_a_bad_order_or_scope_is_refused_and_nothing_written(monkeypatch, order, scope):
    from boss_engine.services import project_settings

    monkeypatch.setattr(project_settings, "set_one",
                        lambda *a, **k: pytest.fail("nothing may be written"))
    with pytest.raises(setup_actions.SetupActionError) as exc:
        setup_actions.set_claude_credential_order(None, object(), "p", order, scope=scope)
    assert exc.value.args[0] == 422 or getattr(exc.value, "status", 422) == 422
