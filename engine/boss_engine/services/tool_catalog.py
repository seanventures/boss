"""Identity of an MCP tool schema, and the quarantine that a change requires.

The hashes travel on the probe summary. A same-named tool whose schema or
description digest changed is quarantined to ask. A first sight is not a
change. This does not write the catalog table or open a decision: it marks
a copy of the observed list and names the decision a reachable probe opens.

THE MARK LASTS UNTIL A PERSON CLEARS IT (BOSSAPP-818). A probe after the change
sees the stored hash equal the observed one, so :func:`compare_catalog` alone
finds nothing and the mark would be gone one probe later. :func:`carry_quarantine`
carries the mark, its first-seen time and the BASELINE (the last definition a
person accepted, or BOSS first saw) forward from the stored entry; only approving
the tool_schema_changed decision clears it (mcp_registry.accept_tool_change). A
tool that changes again while quarantined keeps its first baseline, so the person
reviews baseline -> now, never one hop of several.
"""
from __future__ import annotations

import difflib
import hashlib
import json
from typing import Any, Optional

#: The description hash covers the WHOLE text the model is shown (agent_loop offers the live
#: description untruncated), so a change past any stored cut still quarantines. An entry hashed
#: before that carries no ``hash_version``: its description hash covered only the first
#: LEGACY_DESCRIPTION_CUT characters, and it is compared in those terms, so an upgrade does not
#: quarantine every tool with a long description.
HASH_VERSION = 2
LEGACY_DESCRIPTION_CUT = 500

#: What a stored entry keeps of an open quarantine. Carried forward until a person clears it.
QUARANTINE_KEYS = ("quarantine", "quarantined_at", "quarantine_baseline",
                   "quarantine_refused_by", "quarantine_refused_at")
#: What a stored entry keeps of the last accepted change. Carried forward while unchanged.
REVIEW_KEYS = ("reviewed_by", "reviewed_at")

#: A diff the decision carries is cut here (question and recommendation alike).
DIFF_LIMIT = 4000


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


def _comparable_description_hash(old: dict, new: dict) -> str:
    """The new description's hash in the OLD entry's terms. A legacy entry (no hash_version,
    with a stored hash) hashed only the first LEGACY_DESCRIPTION_CUT characters, so the new
    text is cut the same way for this one comparison; the new entry stores the full hash."""
    if old.get("hash_version") == HASH_VERSION or not old.get("description_sha256"):
        return _description_hash(new)
    description = new.get("description")
    text = description if isinstance(description, str) else ""
    return description_sha256(text[:LEGACY_DESCRIPTION_CUT])


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
        description_changed = old_description != _comparable_description_hash(old, tool)
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


def is_quarantined(tool: Any) -> bool:
    """Whether a stored tool entry waits for a person to review a change of its definition."""
    return isinstance(tool, dict) and bool(tool.get("quarantine"))


def _baseline(entry: dict) -> dict:
    description = entry.get("description")
    schema = entry.get("inputSchema")
    return {
        "schema_sha256": _schema_hash(entry),
        "description_sha256": _description_hash(entry),
        "description": description if isinstance(description, str) else "",
        "inputSchema": schema if isinstance(schema, dict) else None,
    }


def carry_quarantine(previous: Any, marked: Any, quarantined: Any, *, now: str) -> list:
    """The list to store: ``marked`` (from :func:`apply_quarantine`) with each tool's open
    quarantine and last review carried forward from ``previous``. Nothing is mutated.

    * a tool already quarantined stays quarantined, with its first-seen time and baseline,
      whatever this probe found;
    * a tool newly quarantined gets ``quarantined_at`` = ``now`` and the previous entry as its
      baseline; an earlier review of it no longer stands;
    * an unchanged, unquarantined tool keeps who last reviewed a change of it.
    """
    fresh = _quarantined_names(quarantined)
    by_name: dict[str, dict] = {}
    for tool in previous or []:
        if isinstance(tool, dict) and tool.get("name"):
            by_name[str(tool["name"])] = tool
    out: list = []
    for tool in marked or []:
        if not isinstance(tool, dict) or not tool.get("name"):
            out.append(tool)
            continue
        entry = dict(tool)
        name = str(entry["name"])
        old = by_name.get(name)
        if old is None:
            out.append(entry)
            continue
        if is_quarantined(old):
            for key in QUARANTINE_KEYS:
                if key in old:
                    entry[key] = old[key]
            entry["quarantine"] = "ask"
            if not isinstance(entry.get("quarantine_baseline"), dict):
                entry["quarantine_baseline"] = _baseline(old)
            entry.setdefault("quarantined_at", now)
        elif name in fresh:
            entry["quarantine"] = "ask"
            entry["quarantined_at"] = now
            entry["quarantine_baseline"] = _baseline(old)
        else:
            for key in REVIEW_KEYS:
                if key in old:
                    entry[key] = old[key]
        out.append(entry)
    return out


def text_diff(old: str, new: str, old_label: str, new_label: str) -> str:
    """A unified diff of two texts, '' when equal, cut at DIFF_LIMIT characters."""
    if old == new:
        return ""
    out = "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                         fromfile=old_label, tofile=new_label, lineterm=""))
    if len(out) > DIFF_LIMIT:
        out = out[:DIFF_LIMIT] + "\n... (diff cut)"
    return out


def _pretty(schema: Any) -> str:
    return json.dumps(schema if isinstance(schema, dict) else None, sort_keys=True, indent=2,
                      ensure_ascii=False)


def review_item(entry: dict) -> dict:
    """Baseline -> now for one quarantined stored entry: both hash pairs and both text diffs."""
    base = entry.get("quarantine_baseline")
    base = base if isinstance(base, dict) else {}
    schema_now = _schema_hash(entry)
    description_now = _description_hash(entry)
    new_text = entry.get("description") if isinstance(entry.get("description"), str) else ""
    return {
        "name": str(entry.get("name") or ""),
        "quarantine": "ask",
        "quarantined_at": entry.get("quarantined_at"),
        "schema_changed": base.get("schema_sha256") != schema_now,
        "description_changed": base.get("description_sha256") != description_now,
        "previous_schema_sha256": base.get("schema_sha256"),
        "schema_sha256": schema_now,
        "previous_description_sha256": base.get("description_sha256"),
        "description_sha256": description_now,
        "description_diff": text_diff(str(base.get("description") or ""), new_text,
                                      "description before", "description now"),
        "schema_diff": text_diff(_pretty(base.get("inputSchema")),
                                 _pretty(entry.get("inputSchema")),
                                 "input schema before", "input schema now"),
    }


def _short(digest: Any) -> str:
    return str(digest or "none")[:12]


def change_question(server_name: str, item: dict) -> str:
    """The card a person answers: the tool, which hashes moved, and the text diffs.

    A person cannot review a hash alone, so the diffs (when the stored entries hold them) are
    part of the question itself, under one headline line."""
    lines = [f"Tool changed: {server_name} / {item['name']}"]
    if item.get("description_changed"):
        lines.append(f"Description changed: {_short(item.get('previous_description_sha256'))}"
                     f" -> {_short(item.get('description_sha256'))}")
    if item.get("schema_changed"):
        lines.append(f"Input schema changed: {_short(item.get('previous_schema_sha256'))}"
                     f" -> {_short(item.get('schema_sha256'))}")
    lines.append("Until this is answered every call of the tool asks a person. Approve accepts "
                 "the new definition; Reject keeps it quarantined and turns the tool Off.")
    for key in ("description_diff", "schema_diff"):
        if item.get(key):
            lines.extend(["", str(item[key])])
    return "\n".join(lines)


def changed_tool_decisions(server_name: str, quarantined: Any, *,
                           server_id: Optional[str] = None) -> list[dict]:
    """One decision payload per quarantined tool. Nothing is written here.

    ``quarantined`` items are :func:`review_item` dicts (baseline -> now, with diffs) or bare
    :func:`compare_catalog` dicts; either way the card names the tool and both hash pairs."""
    decisions: list[dict] = []
    for item in quarantined or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        name = str(item["name"])
        recommendation = {
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
        }
        if server_id:
            recommendation["server_id"] = server_id
        for key in ("description_diff", "schema_diff", "quarantined_at"):
            if item.get(key):
                recommendation[key] = item[key]
        decisions.append({
            "question": change_question(server_name, {**item, "name": name}),
            "recommendation": recommendation,
        })
    return decisions
