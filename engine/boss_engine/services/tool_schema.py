"""AN MCP TOOL'S inputSchema AS EVERY MODEL PROVIDER ACCEPTS IT (BOSSAPP-1476).

MCP servers publish whatever JSON Schema their framework writes, and BOSS offered it to the
model untouched. Model APIs are stricter than JSON Schema, and one bad tool fails the WHOLE
turn (every tool of every server is in one request):

* a schema without ``"type": "object"`` at the root (``{}``, ``{"properties": ...}``, a root
  ``oneOf`` of object shapes) is refused: OpenAI-compatible APIs require an object root;
* ``$ref`` into ``$defs`` / ``definitions`` (pydantic and zod-to-json-schema write them) is
  refused by several providers' function-calling dialects;
* ``oneOf`` is refused by some, where ``anyOf`` is accepted by all.

:func:`for_model` returns a copy the model can be offered: local ``$ref`` inlined (a cycle or a
very deep one becomes a plain object), ``oneOf`` read as ``anyOf`` (looser, never stricter — the
server still validates the call), and an object root. The STORED schema and its hash
(mcp_registry._tool_summary, BOSSAPP-818) are untouched: this is a pure function of what is
hashed, so a change to the server's schema still changes what the model reads.
"""
from __future__ import annotations

import copy
from typing import Any

#: How deep $ref chains are followed before a reference is left as a plain object.
MAX_REF_DEPTH = 8
_DROPPED_AT_ROOT = ("$defs", "definitions", "$schema", "$id")


def _resolve_pointer(root: dict, ref: str) -> Any:
    if not ref.startswith("#/"):
        return None
    node: Any = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _walk(node: Any, root: dict, seen: tuple[str, ...]) -> Any:
    if isinstance(node, list):
        return [_walk(item, root, seen) for item in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str):
        target = _resolve_pointer(root, ref)
        extra = {k: v for k, v in node.items() if k != "$ref"}
        if target is None or ref in seen or len(seen) >= MAX_REF_DEPTH:
            # An outside, recursive or too-deep reference: an object the model fills freely.
            out = {"type": "object"}
            if extra.get("description") or (isinstance(target, dict)
                                             and target.get("description")):
                out["description"] = extra.get("description") or target.get("description")
            return out
        merged = {**copy.deepcopy(target), **extra} if isinstance(target, dict) else target
        return _walk(merged, root, seen + (ref,))
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "oneOf":
            out["anyOf"] = _walk(value, root, seen)
        else:
            out[key] = _walk(value, root, seen)
    return out


def _object_root(schema: dict) -> dict:
    if schema.get("type") == "object":
        return schema if isinstance(schema.get("properties"), dict) else \
            {**schema, "properties": {}}
    branches = schema.get("anyOf") or schema.get("allOf") or []
    objects = [b for b in branches if isinstance(b, dict)
               and (b.get("type") == "object" or "properties" in b)]
    if objects and len(objects) == len(branches):
        # A root union of object shapes: one object with every property; required only what
        # every shape requires (allOf: what any requires).
        properties: dict[str, Any] = {}
        for branch in objects:
            properties.update(branch.get("properties") or {})
        sets = [set(b.get("required") or []) for b in objects]
        required = (set().union(*sets) if "allOf" in schema and "anyOf" not in schema
                    else set.intersection(*sets) if sets else set())
        out = {k: v for k, v in schema.items() if k not in ("anyOf", "allOf", "type")}
        out.update({"type": "object", "properties": {**(schema.get("properties") or {}),
                                                     **properties}})
        if required:
            out["required"] = sorted(required)
        return out
    if "properties" in schema or not schema.get("type"):
        return {**{k: v for k, v in schema.items() if k not in ("anyOf", "allOf", "oneOf")},
                "type": "object", "properties": schema.get("properties") or {}}
    # A scalar root (rare and invalid for MCP): offered as an object with one `value`.
    return {"type": "object", "properties": {"value": schema}, "required": ["value"]}


def for_model(schema: Any) -> dict[str, Any]:
    """The tool's input schema as a model may be offered it (see the module docstring)."""
    if not isinstance(schema, dict) or not schema:
        return {"type": "object", "properties": {}}
    walked = _walk(schema, schema, ())
    for key in _DROPPED_AT_ROOT:
        walked.pop(key, None)
    return _object_root(walked)


def description_for_model(tool: dict[str, Any]) -> str:
    """A tool's description, never "None"; a server that gave none gets a line saying so."""
    text = tool.get("description")
    text = text.strip() if isinstance(text, str) else ""
    return text or f"{tool.get('name') or 'This tool'} (the server gave no description; " \
                   "read its parameters)."
