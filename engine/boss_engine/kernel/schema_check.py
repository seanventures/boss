"""Tool arguments checked against the tool's schema BEFORE the tool runs.

Every old loop parsed a call's arguments with `json.loads(...) or {}` and ran the tool on
whatever came out: a model that wrote half a JSON object got the tool run with no arguments,
and the tool's error (or worse, its default behaviour) became the evidence. Providers that
validate server-side (Groq) refused the call with a 400 instead, and agent_loop alone fed that
back. The kernel now checks every call itself, whatever the provider, and names the failing
place as a JSON pointer so the model can fix exactly that (design §2.8).

A deliberately small validator: the subset BOSS's tool schemas use (type, properties,
required, additionalProperties, enum, items, nullable type lists). An unknown keyword is
ignored, never failed on: a schema BOSS does not own (a third-party MCP tool) must not become
uncallable because it uses a keyword this does not read.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

_JSON_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "integer": int, "number": (int, float), "null": type(None),
}


def _pointer(path: list) -> str:
    if not path:
        return "/"
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in path)


def _type_ok(value: Any, declared: Any) -> bool:
    names = declared if isinstance(declared, list) else [declared]
    for name in names:
        expected = _JSON_TYPES.get(str(name))
        if expected is None:
            return True  # an unknown type name is not ours to enforce
        if isinstance(value, bool) and name in ("integer", "number"):
            continue  # JSON true is not a number, though Python says it is
        if isinstance(value, expected):
            return True
    return False


def first_violation(value: Any, schema: Optional[Mapping[str, Any]], path: Optional[list] = None
                    ) -> Optional[str]:
    """The first place `value` breaks `schema`, as "<pointer>: <what is wrong>", or None."""
    if not isinstance(schema, Mapping):
        return None
    path = list(path or [])
    declared = schema.get("type")
    if declared is not None and not _type_ok(value, declared):
        shown = declared if isinstance(declared, str) else " or ".join(map(str, declared))
        return f"{_pointer(path)}: must be {shown}, got {type(value).__name__}"
    if "enum" in schema and isinstance(schema.get("enum"), list) and value not in schema["enum"]:
        return f"{_pointer(path)}: must be one of {schema['enum'][:12]}"
    if isinstance(value, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
        for name in schema.get("required") or []:
            if name not in value:
                return f"{_pointer(path + [name])}: is required and missing"
        if schema.get("additionalProperties") is False:
            extra = sorted(k for k in value if k not in properties)
            if extra:
                return f"{_pointer(path + [extra[0]])}: is not a parameter of this tool"
        for name, sub in properties.items():
            if name in value:
                found = first_violation(value[name], sub, path + [name])
                if found:
                    return found
    if isinstance(value, list) and isinstance(schema.get("items"), Mapping):
        for n, item in enumerate(value):
            found = first_violation(item, schema["items"], path + [n])
            if found:
                return found
    return None


def check_arguments(arguments: Optional[dict], parameters: Optional[Mapping[str, Any]]
                    ) -> Optional[str]:
    """Why these arguments may not run the tool, or None when they may."""
    if arguments is None:
        return "/: the arguments are not a JSON object"
    return first_violation(arguments, parameters or {"type": "object"})


__all__ = ["check_arguments", "first_violation"]
