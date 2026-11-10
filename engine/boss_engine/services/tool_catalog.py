"""Identity of an MCP tool schema, and the quarantine that a change requires.

The hashes travel on the probe summary. A same-named tool whose schema or
description digest changed is quarantined to ask. A first sight is not a
change. This does not write the catalog table or open a decision: it marks
a copy of the observed list and names the decision a reachable probe opens.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def schema_sha256(schema: Any) -> str:
    """sha256 hex of the canonical schema. A missing schema hashes JSON null."""
    if schema is None:
        payload = None
    elif isinstance(schema, dict):
        payload = schema
    else:
        raise ValueError("schema must be a dict or null")
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def description_sha256(description: str | None) -> str:
    """sha256 hex of the description characters. Missing text hashes empty."""
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise ValueError("description must be a string or null")
    return hashlib.sha256(description.encode("utf-8")).hexdigest()


def _schema_hash(tool: dict) -> str:
    stored = tool.get("schema_sha256")
    if isinstance(stored, str) and stored:
        return stored
    schema = tool.get("inputSchema")
    return schema_sha256(schema if isinstance(schema, dict) else None)


def _description_hash(tool: dict) -> str:
    stored = tool.get("description_sha256")
    if isinstance(stored, str) and stored:
        return stored
    description = tool.get("description")
    return description_sha256(description if isinstance(description, str) else None)


def compare_catalog(previous: Any, observed: Any) -> dict[str, list]:
    """Quarantine same-named tools whose stored hashes changed.

    ``previous`` None, or a tool that is only on one side, quarantines nothing.
    The input lists are not mutated.
    """
    if not previous:
        return {"quarantined": []}
    by_name: dict[str, dict] = {}
    for tool in previous:
        if isinstance(tool, dict) and tool.get("name"):
            by_name[str(tool["name"])] = tool
    quarantined = []
    for tool in observed or []:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        name = str(tool["name"])
        old = by_name.get(name)
        if old is None:
            continue
        old_schema = _schema_hash(old)
        new_schema = _schema_hash(tool)
        old_description = _description_hash(old)
        new_description = _description_hash(tool)
        schema_changed = old_schema != new_schema
        description_changed = old_description != new_description
        if not schema_changed and not description_changed:
            continue
        quarantined.append({
            "name": name,
            "quarantine": "ask",
            "schema_changed": schema_changed,
            "description_changed": description_changed,
            "previous_schema_sha256": old_schema,
            "schema_sha256": new_schema,
            "previous_description_sha256": old_description,
            "description_sha256": new_description,
        })
    return {"quarantined": quarantined}


def _quarantined_names(quarantined: Any) -> set[str]:
    names: set[str] = set()
    for item in quarantined or []:
        if isinstance(item, dict):
            name = item.get("name")
        elif isinstance(item, str):
            name = item
        else:
            continue
        if name:
            names.add(str(name))
    return names


def apply_quarantine(observed: Any, quarantined: Any) -> list:
    """Copy of the observed tools. A tool named in ``quarantined`` is ask.

    Entries are the dicts :func:`compare_catalog` returns, or bare names.
    Neither the lists nor the tool dicts are mutated.
    """
    names = _quarantined_names(quarantined)
    marked: list = []
    for tool in observed or []:
        if not isinstance(tool, dict):
            marked.append(tool)
            continue
        copied = dict(tool)
        name = copied.get("name")
        if name and str(name) in names:
            copied["quarantine"] = "ask"
        marked.append(copied)
    return marked


def changed_tool_decisions(server_name: str, quarantined: Any) -> list[dict]:
    """One decision payload per quarantined tool. Nothing is written here."""
    decisions: list[dict] = []
    for item in quarantined or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        name = str(item["name"])
        decisions.append({
            "question": f"Tool changed: {server_name} / {name}",
            "recommendation": {
                "action": "review_tool_change",
                "server": server_name,
                "tool": name,
                "quarantine": "ask",
                "schema_changed": item.get("schema_changed"),
                "description_changed": item.get("description_changed"),
                "previous_schema_sha256": item.get("previous_schema_sha256"),
                "schema_sha256": item.get("schema_sha256"),
                "previous_description_sha256": item.get("previous_description_sha256"),
                "description_sha256": item.get("description_sha256"),
            },
        })
    return decisions
