"""Phrase screen for instruction-override text inside a tool result (BOSSAPP-822).

A hit names the pattern and marks the result untrusted. The tool text is left as the tool
returned it. This does not call a model and it does not decide what the model may do next.
"""
from __future__ import annotations

import re
from typing import Any

#: Whole phrases, case-insensitive. A shared word ("ignore", "instructions") is not a hit.
_IGNORE_PREVIOUS = re.compile(
    r"\bignore\s+previous\s+instructions\b"
    r"|\bignore\s+all\s+previous\s+instructions\b"
    r"|\bignore\s+prior\s+instructions\b",
    re.IGNORECASE,
)
_DISREGARD_RULES = re.compile(
    r"\bdisregard\s+your\s+instructions\b"
    r"|\bdisregard\s+the\s+rules\b"
    r"|\bdisregard\s+your\s+rules\b"
    r"|\bdisregard\s+the\s+system\b",
    re.IGNORECASE,
)
_YOU_ARE_NOW = re.compile(
    r"\byou\s+are\s+now\s+a\b|\byou\s+are\s+now\s+an\b",
    re.IGNORECASE,
)
#: "system prompt" only counts when the same text also says it is new or updated.
_SYSTEM_PROMPT = re.compile(r"\bsystem\s+prompt\b", re.IGNORECASE)
_NEW_OR_UPDATED = re.compile(r"\b(?:new|updated)\b", re.IGNORECASE)
_HIDE_FROM_OPERATOR = re.compile(
    r"\bdo\s+not\s+tell\s+the\s+(?:user|operator)\b"
    r"|\bdon['\u2019]t\s+tell\s+the\s+(?:user|human)\b",
    re.IGNORECASE,
)

_ORDER = (
    "ignore_previous",
    "disregard_rules",
    "you_are_now",
    "new_system_prompt",
    "hide_from_operator",
)


def _texts(value: Any):
    """Strings inside a tool payload. Numbers, bools and null are not text."""
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _texts(item)
        return
    if isinstance(value, list):
        for item in value:
            yield from _texts(item)


def _ids_in(text: str) -> set[str]:
    found: set[str] = set()
    if _IGNORE_PREVIOUS.search(text):
        found.add("ignore_previous")
    if _DISREGARD_RULES.search(text):
        found.add("disregard_rules")
    if _YOU_ARE_NOW.search(text):
        found.add("you_are_now")
    if _SYSTEM_PROMPT.search(text) and _NEW_OR_UPDATED.search(text):
        found.add("new_system_prompt")
    if _HIDE_FROM_OPERATOR.search(text):
        found.add("hide_from_operator")
    return found


def screen_tool_result(content: Any) -> dict[str, Any]:
    """``hit``, ``patterns`` and ``untrusted``. ``untrusted`` is true only on a hit."""
    hits: set[str] = set()
    for text in _texts(content):
        hits.update(_ids_in(text))
    patterns = [pattern_id for pattern_id in _ORDER if pattern_id in hits]
    hit = bool(patterns)
    return {"hit": hit, "patterns": patterns, "untrusted": hit}


def carried_untrusted(value: Any) -> bool:
    """True when this result, or the invocation wrapped around it, was screened as untrusted."""
    if not isinstance(value, dict):
        return False
    if value.get("untrusted") is True:
        return True
    for key in ("result", "output"):
        inner = value.get(key)
        if isinstance(inner, dict) and inner.get("untrusted") is True:
            return True
    return False


def next_write_must_ask(screened: Any, *, write: bool, policy: str) -> str:
    """After an untrusted result, a write's allow becomes ask. Off and reads stay."""
    if policy == "off" or not write or not carried_untrusted(screened):
        return policy
    if policy == "allow":
        return "ask"
    return policy


def apply_screen(result: dict) -> dict:
    """Shallow-copy a hit with ``untrusted`` and ``screen``. A miss is the same dict.

    A non-dict is returned as it was. The input dict is never mutated, and the tool text
    is not rewritten.
    """
    if not isinstance(result, dict):
        return result
    found = screen_tool_result(result)
    if not found["hit"]:
        return result
    screened = dict(result)
    screened["untrusted"] = True
    screened["screen"] = {"patterns": list(found["patterns"])}
    return screened
