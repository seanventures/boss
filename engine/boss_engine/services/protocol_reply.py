"""A model's DECISION STRUCTURE is never a reply.

Asked for the state of three projects through Metatron, BOSS's whole answer was

    {"thought": "Stop retrying create_project. ...", "tool_calls": [{"name": "search_code", ...

— the tool protocol's JSON, with one closing brace missing. The CLI adapter's parser found no
object at the top, fell back to the first balanced `{...}` inside it (one tool call's
`arguments`), saw no protocol keys there and handed the raw text back as the answer. The loop
took text-with-no-tool-calls for the final answer and every surface printed it.

The class is "the model's output is a decision or tool-call structure, well formed or not". It is
recognised here, once, for every adapter and every surface:

  * `parse` reads the protocol object at the TOP of the text — never an object nested inside it —
    and salvages a malformed one (its thought, its final answer, and each tool call whose
    arguments still parse), so a tool call the model meant is executed instead of printed;
  * `is_decision_structure` says whether a reply is such a structure and not prose;
  * `prose_of` is the words a person may read from one (its `final`, else its `thought`).
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

#: Keys only the tool protocol (`{"thought", "tool_calls" | "final"}`) and the decision contract
#: (`{"action", "confidence", ...}`) put at the top of an object.
PROTOCOL_KEYS = frozenset({"tool_calls", "tool_call", "final", "thought"})
_DECISION_KEYS = frozenset({"action", "confidence"})

_FENCE_OPEN = re.compile(r"^```[a-zA-Z0-9]*\s*")
_FENCE_CLOSE = re.compile(r"\s*```$")
_STRING_FIELD = r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"'
_CALL_START = re.compile(r'\{\s*"name"\s*:\s*"([^"\\]+)"\s*,\s*"arguments"\s*:\s*')


def _unfenced(text: str) -> str:
    t = str(text or "").strip()
    if t.startswith("```"):
        t = _FENCE_CLOSE.sub("", _FENCE_OPEN.sub("", t))
    return t.strip()


def _string_field(text: str, key: str) -> Optional[str]:
    found = re.search(_STRING_FIELD % re.escape(key), text)
    if not found:
        return None
    try:
        return json.loads(f'"{found.group(1)}"')
    except ValueError:
        return found.group(1)


def _salvage(text: str) -> Optional[dict]:
    """The protocol object a malformed reply meant: its thought, final and each tool call whose
    name and arguments can still be read."""
    out: dict[str, Any] = {}
    for key in ("thought", "final"):
        value = _string_field(text, key)
        if value is not None:
            out[key] = value
    calls = []
    decoder = json.JSONDecoder()
    for match in _CALL_START.finditer(text):
        try:
            arguments, _end = decoder.raw_decode(text, match.end())
        except ValueError:
            continue
        if isinstance(arguments, (dict, str)):
            calls.append({"name": match.group(1), "arguments": arguments})
    if calls:
        out["tool_calls"] = calls
    if not out and re.search(r'^\{\s*"(action|confidence)"\s*:', text):
        action = _string_field(text, "action")
        if action is not None:
            out["action"] = action
            out["confidence"] = None
    return out or None


def parse(text: Any) -> Optional[dict]:
    """The decision structure at the top of `text`, or None when the text is not one.

    Only an object that STARTS the text counts (code fences aside): prose that quotes some JSON
    is prose. A well-formed object is returned as is; a malformed one is salvaged."""
    t = _unfenced(text)
    if not t.startswith("{"):
        return None
    try:
        obj, _end = json.JSONDecoder().raw_decode(t)
    except ValueError:
        obj = None
    if isinstance(obj, dict):
        keys = set(obj)
        if keys & PROTOCOL_KEYS or _DECISION_KEYS <= keys:
            return obj
        return None
    if not re.match(r'^\{\s*"(thought|tool_calls|tool_call|final|action)"\s*:', t):
        return None
    return _salvage(t)


def is_decision_structure(text: Any) -> bool:
    return parse(text) is not None


def prose_of(text: Any) -> Optional[str]:
    """What a person may read from a decision structure: its final answer, else its thought.
    None when it holds neither (or the text is not a structure)."""
    obj = parse(text)
    if not obj:
        return None
    final = obj.get("final")
    if isinstance(final, str) and final.strip():
        return final.strip()
    thought = obj.get("thought")
    if isinstance(thought, str) and thought.strip():
        return thought.strip()
    return None


_EMBEDDED_START = re.compile(r'\{\s*"(thought|tool_calls|tool_call|final)"\s*:')


def parse_embedded(text: Any) -> Optional[dict]:
    """`parse`, also when some prose comes before the protocol object: the object is read from
    where a protocol key opens one, never from an object nested inside it."""
    found = parse(text)
    if found is not None:
        return found
    t = _unfenced(text)
    match = _EMBEDDED_START.search(t)
    return parse(t[match.start():]) if match else None


def prose_of_final(structure: Optional[dict]) -> Optional[str]:
    """A decision structure's FINAL answer only — its thought is reasoning, not an answer."""
    final = (structure or {}).get("final")
    if isinstance(final, str) and final.strip():
        return final.strip()
    return None
