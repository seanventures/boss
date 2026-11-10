"""Whether one build-agent tool call may run (BOSSAPP-813).

The native build loop asks this before it invokes the tool. A dict maps each
tool to ``allow`` (run), ``ask`` (wait) or ``off`` (deny); a missing name is
``allow``. A list is ordered rules: lower priority first, first match wins,
and no match denies. ``when`` is top-level argument equality. A database,
console, scopes, rate limits and time windows are not this slice.
"""
from __future__ import annotations

from typing import Any

RUN = "run"
WAIT = "wait"
DENY = "deny"

_ALLOW = "allow"
_ASK = "ask"
_OFF = "off"


def decide(name: str, policy: Any, arguments: Any = None) -> dict[str, Any]:
    """``decision``, ``matched`` rule ids, and a short ``explanation``."""
    if isinstance(policy, list):
        return _decide_rules(name, policy, arguments)
    if not isinstance(policy, dict):
        return {"decision": RUN, "matched": [], "explanation": _ALLOW}
    word = str(policy.get(name, _ALLOW) or _ALLOW).strip().lower()
    if word == _ALLOW:
        decision = RUN
    elif word == _ASK:
        decision = WAIT
    else:
        # ``off`` and any unknown word deny. An empty value never reaches here:
        # ``or "allow"`` already replaced it.
        decision = DENY
    return {"decision": decision, "matched": [], "explanation": word}


def verdict(name: str, policy: Any, arguments: Any = None) -> str:
    """``run``, ``wait`` or ``deny`` for this tool name. Unknown words deny."""
    return decide(name, policy, arguments)["decision"]


def _decide_rules(name: str, rules: list, arguments: Any) -> dict[str, Any]:
    args = arguments if isinstance(arguments, dict) else {}
    ordered: list[tuple[int, int, dict]] = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            continue
        ordered.append((int(rule.get("priority", 0)), index, rule))
    ordered.sort()
    for _priority, _index, rule in ordered:
        if _matches(name, rule, args):
            return _matched(rule)
    return {"decision": DENY, "matched": [], "explanation": "no rule matched"}


def _matches(name: str, rule: dict, args: dict) -> bool:
    tool = rule.get("tool")
    if tool != "*" and tool != name:
        return False
    when = rule.get("when")
    if not isinstance(when, dict):
        return True
    for key, expected in when.items():
        if key not in args or args[key] != expected:
            return False
    return True


def _matched(rule: dict) -> dict[str, Any]:
    effect = str(rule.get("effect", "") or "").strip().lower()
    rule_id = str(rule.get("id", ""))
    if effect == _ALLOW:
        decision = RUN
    elif effect == _ASK:
        decision = WAIT
    else:
        # ``off`` and an unknown effect both deny, and this rule is still the match.
        decision = DENY
    return {"decision": decision, "matched": [rule_id], "explanation": f"rule {rule_id}"}
