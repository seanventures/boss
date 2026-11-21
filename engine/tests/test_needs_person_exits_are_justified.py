"""EVERY NEEDS-PERSON EXIT IS LISTED, JUSTIFIED, AND ITS CARD SAYS EXACTLY WHAT TO DO.

THE OPERATOR (2026-11-21): "we are selling the product mainly for Agents to work autonomously
by fixing the issue, if you are involving humans for these actions this is not a autonomous
software factory ... if it's blocked worst case it should provide the guidance for humans to fix
the issue".

THE GUARD, read from the code (no database):
  * every way BOSS hands a failure to a person — a `NeedsPerson(…, exit=…)` or `_hand_over(…,
    exit=…)` in failure_owner, a person-side cause rule, an ASK_PERSON-returning function in
    step_recovery, an `open_question` caller in the Shepherd, a merge card reopened for a person
    — is in needs_person_exits.EXITS with its justification; a new one fails here until it is;
  * an exit an agent now owns (AGENT_REMEDIES, the org default) is marked so;
  * every card's guidance, for every cause the loop can hand over, names what failed, why, the
    exact steps and what BOSS does after — and never "check the host" or "do what its page says";
  * the fixer authority can never carry a hard-limit key (merge, production deploy, approve,
    secrets, host:exec, delegation).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from boss_engine.identity import catalogue, fix_authority
from boss_engine.services import failure_guidance, failure_owner as fo
from boss_engine.services.needs_person_exits import EXITS, counts

ROOT = Path(__file__).resolve().parents[2]
SVC = ROOT / "engine" / "boss_engine" / "services"


def _tree(name: str) -> tuple[str, ast.Module]:
    src = (SVC / name).read_text(encoding="utf-8")
    return src, ast.parse(src)


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and (getattr(n.func, "id", None) == name or getattr(n.func, "attr", None) == name)]


def _exit_kw(call: ast.Call) -> ast.AST | None:
    return next((k.value for k in call.keywords if k.arg == "exit"), None)


def test_every_needs_person_raise_in_the_owner_loop_names_a_listed_exit():
    _, tree = _tree("failure_owner.py")
    calls = [c for c in _calls(tree, "NeedsPerson")]
    assert len(calls) >= 20, "the scan no longer sees the owner loop's exits"
    for call in calls:
        value = _exit_kw(call)
        assert isinstance(value, ast.Constant) and value.value in EXITS, (
            f"failure_owner.py:{call.lineno}: NeedsPerson(...) must name exit=<id> listed in "
            "needs_person_exits.EXITS with why a person is needed (or make an agent do it)")


def test_every_hand_over_says_which_exit_it_is():
    _, tree = _tree("failure_owner.py")
    calls = [c for c in _calls(tree, "_hand_over") if not isinstance(c.func, ast.Attribute)]
    assert len(calls) >= 4
    for call in calls:
        value = _exit_kw(call)
        assert value is not None, f"failure_owner.py:{call.lineno}: _hand_over(...) needs exit="
        if isinstance(value, ast.Constant):
            assert value.value in EXITS, value.value


def test_every_person_side_cause_is_listed_and_the_agent_remedies_are_marked_as_the_agents():
    codes = [code for code, _p, _a in fo._PERSON_RULES] + ["needs_human"]
    for code in codes:
        assert f"person_cause:{code}" in EXITS, f"person cause {code!r} is not in the inventory"
    for code in fo.AGENT_REMEDIES:
        assert EXITS[f"person_cause:{code}"].agent, f"{code} is an agent remedy now"
    assert "person_cause:asked_by_*" in EXITS


@pytest.mark.parametrize("module, predicate, prefix", [
    ("step_recovery.py", "ask", "step_recovery."),
    ("shepherd_service.py", "open_question", "shepherd."),
    ("merge_failures.py", "reopen", "merge."),
])
def test_every_ask_a_person_site_elsewhere_is_listed(module, predicate, prefix):
    _, tree = _tree(module)

    def hits(node: ast.AST) -> bool:
        if predicate == "ask":
            return isinstance(node, ast.Dict) and any(
                isinstance(k, ast.Constant) and k.value == "action" and isinstance(v, ast.Name)
                and v.id == "ASK_PERSON" for k, v in zip(node.keys, node.values))
        if predicate == "open_question":
            return isinstance(node, ast.Call) and getattr(node.func, "id", None) == "open_question"
        return isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_mark" and any(
            k.arg == "reopen" and isinstance(k.value, ast.Constant) and k.value.value is True
            for k in node.keywords)

    found = {fn.name for fn in ast.walk(tree) if isinstance(fn, (ast.FunctionDef,))
             and any(hits(n) for n in ast.walk(fn))}
    assert found, f"the scan no longer sees {module}'s person exits"
    missing = sorted(f"{prefix}{name}" for name in found if f"{prefix}{name}" not in EXITS)
    assert not missing, (f"{module} asks a person in {missing}: list each in "
                         "needs_person_exits.EXITS with why (or have the agent do it)")


def test_every_exit_says_where_and_why_in_words():
    for key, ex in EXITS.items():
        assert ex.where and len(ex.why.split()) >= 4, key
    c = counts()
    assert c["agent"] >= 8 and c["human"] >= 20, c


_CAUSES = [code for code, _p, _a in fo._PERSON_RULES] + [
    "needs_human", "asked_by_pilot", "platform:old", "app:old", "platform:spent", "app:spent",
    "grant:withheld", "platform:withheld", "retry_refused", "platform:held_proof",
    "merge_conflict:spent", "platform:sandbox_error:spent"]


@pytest.mark.parametrize("cause_code", _CAUSES)
@pytest.mark.parametrize("cls", ["person", "platform", "app"])
def test_every_card_gives_exact_guidance(cause_code, cls):
    action = fo._PERSON_RULES_BY_CODE.get(cause_code.split(":")[0]) or (
        "Free port 8000 on the host? Then press Done — retry." if cause_code.startswith("asked")
        else "Press Done — retry to run it again.")
    g = failure_guidance.build(cause_code=cause_code, cls=cls, title="API onboarding",
                               cause="1 of 12 contract case(s) failed: payments_rate_limited_11th.",
                               action=action, reason="BOSS tried 2 times; its agent could not "
                               "fix it.", hints={"receipt_id": "rcpt_x"})
    assert failure_guidance.problems(g) == [], (cause_code, cls, g)


def test_the_generic_advice_the_operator_got_is_refused():
    bad = {"what_failed": "API onboarding: 1 of 12 contract case(s) failed",
           "why": "BOSS tried 2 times", "steps": [
               "Check the BOSS host is healthy (Infra › Doctor), then press Done — retry."],
           "after": "BOSS runs it again."}
    assert any(p.startswith("generic") for p in failure_guidance.problems(bad))
    assert "Check the BOSS host is healthy" not in (SVC / "failure_owner.py").read_text()


@pytest.mark.parametrize("key", ["deployment:production", "decision:approve", "pull_request:merge",
                                 "code_task:merge", "secret:read", "secret:manage", "host:exec",
                                 "delegation:grant", "access_role:manage"])
def test_the_fixer_authority_never_carries_a_hard_limit(key):
    assert not fix_authority.allowed(key)


def test_the_fixer_authority_carries_only_delegable_keys():
    for key in catalogue.delegable():
        if fix_authority.allowed(key):
            assert catalogue.meta(key).delegable
    assert fix_authority.HARD_LIMITS and len(fix_authority.HARD_LIMITS) == 4
