"""Every reversibility class has its own true warning on an offered button (brec_4a4c244e).

request_self_fix is `costs_money` + `operator_only`: it only asks BOSS to diagnose a filed gap
and propose a diff a person still approves. Its button told the operator it "destroys work or
removes a safeguard", because the agent's warning table had entries only for the three
never-self-execute classes and everything else fell through to the destructive default.
"""
from __future__ import annotations

import typing

from boss_engine.tools.registry import (
    NEVER_SELF_EXECUTE, REVERSIBILITY_WARNINGS, Reversibility, ToolSpec, offer_warning)
from boss_engine.tools.builtin import build_default_registry, build_discuss_read_registry

_DESTRUCTIVE_WORDS = ("destroys", "throws away", "removes a", "cannot be undone")


def _spec(reversibility, **kw):
    return ToolSpec(name="t", description="d", parameters={"type": "object"},
                    handler=lambda *a, **k: {}, kind="write", reversibility=reversibility,
                    inherits_route_permission=True, **kw)


def test_every_reversibility_value_has_its_own_warning():
    values = set(typing.get_args(Reversibility))
    assert values == set(REVERSIBILITY_WARNINGS)
    # each class says something different: no class borrows another's sentence
    assert len(set(REVERSIBILITY_WARNINGS.values())) == len(values)


def test_each_class_wording_is_true_of_that_class():
    assert "budget" in offer_warning(_spec("costs_money"))
    assert "undone or simply redone" in offer_warning(_spec("safe"))
    assert "throws away work" in offer_warning(_spec("destroys_work"))
    assert "cannot be undone" in offer_warning(_spec("irreversible"))
    assert "reach on somebody's account" in offer_warning(_spec("grants_authority"))
    for value in typing.get_args(Reversibility):
        text = offer_warning(_spec(value))
        assert text.endswith("the operator decides")
        if value not in NEVER_SELF_EXECUTE:
            assert not any(w in text for w in _DESTRUCTIVE_WORDS), (value, text)


def test_request_self_fix_says_it_spends_budget_and_installs_nothing_unapproved():
    for reg in (build_default_registry(), build_discuss_read_registry()):
        spec = reg.get("request_self_fix")
        text = offer_warning(spec)
        assert "destroys work or removes a safeguard" not in text
        assert "budget" in text
        assert "nothing installs without a person's approval" in text


def test_every_offered_tool_in_the_default_registry_gets_a_non_destructive_warning_unless_it_is():
    reg = build_default_registry()
    for spec in reg.specs(kind="write"):
        if spec.self_executable:
            continue
        text = offer_warning(spec)
        if spec.reversibility not in NEVER_SELF_EXECUTE:
            assert not any(w in text for w in _DESTRUCTIVE_WORDS), (spec.name, text)


def test_the_agents_offer_and_the_registrys_refusal_use_the_same_wording():
    """Both surfaces read offer_warning: the engine agent's `why` and the registry's
    confirmation_required message."""
    import inspect
    from boss_engine.agent import engine_agent
    from boss_engine.tools import registry
    assert "offer_warning(spec)" in inspect.getsource(engine_agent)
    assert "destroys work or removes a safeguard" not in inspect.getsource(engine_agent)
    assert "offer_warning(spec)" in inspect.getsource(registry.ToolRegistry)


def test_the_chat_registry_keeps_each_tools_offer_wording():
    source = build_default_registry()
    for spec in build_discuss_read_registry().specs():
        assert offer_warning(spec) == offer_warning(source.get(spec.name)), spec.name
