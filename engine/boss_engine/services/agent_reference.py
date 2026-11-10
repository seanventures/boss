"""ONE WAY TO NAME A STORED AGENT REFERENCE (BOSSAPP-954).

A run stores who built it in more than one shape: `finalize_ctx.agent` is a dict
({"agent_id": "agt_…", "role_key": "engineering_captain"}), older rows and some writers keep
it as a JSON string or as `str(dict)` (a Python repr), and others keep a bare agent id, a role
key, `agent:<role_key>` or a name. The change review once printed `str(dict)` and CSS
title-cased it, so the pane read "{'Agent Id': 'Agt 2m0xahx2jf', …}".

`agent_display` reads any of those shapes and answers with the agent's real name (its persona
callsign, else the role's display name), the role as a fallback, and never an id: an id with
no row behind it is "builder", not "Agt 2m0xahx2jf".
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any, Mapping, Optional

from sqlalchemy import text

from .agent_persona import persona_of

FALLBACK = "builder"
_ROLE_KEY = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$|^[a-z][a-z0-9]+$")
_AGENT_ID = re.compile(r"^agt_[A-Za-z0-9]+$")


def parse_reference(ref: Any) -> dict[str, str]:
    """{agent_id?, role_key?, name?} from any stored shape; {} when there is nothing to read."""
    if ref is None:
        return {}
    if isinstance(ref, Mapping):
        out = {}
        for key, names in (("agent_id", ("agent_id", "id")), ("role_key", ("role_key", "role")),
                           ("name", ("name", "display_name", "callsign"))):
            for n in names:
                v = ref.get(n)
                if isinstance(v, str) and v.strip():
                    out[key] = v.strip()
                    break
        if out.get("agent_id") and not _AGENT_ID.match(out["agent_id"]):
            out.pop("agent_id")  # an "id" that is not an agent id names nothing we can look up
        return out
    s = str(ref).strip()
    if not s:
        return {}
    if s[0] in "{[":
        for loads in (json.loads, ast.literal_eval):
            try:
                value = loads(s)
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                continue
            if isinstance(value, Mapping):
                return parse_reference(value)
        return {}  # unreadable structured text is never shown
    if s.startswith("agent:"):
        s = s[len("agent:"):].strip()
    if _AGENT_ID.match(s):
        return {"agent_id": s}
    if _ROLE_KEY.match(s) and "_" in s:
        return {"role_key": s}
    return {"name": s}


def humanize_role(role_key: str) -> str:
    """engineering_captain → Engineering captain (a role key, never an id)."""
    words = role_key.replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else ""


def _role_row(c: Any, project_id: Optional[str], ref: Mapping[str, str]) -> Optional[dict]:
    q = "SELECT role_key, display_name, spec FROM roles WHERE "
    if ref.get("agent_id"):
        row = c.execute(text(q + "agent_id = :a ORDER BY (project_id = :p) DESC LIMIT 1"),
                        {"a": ref["agent_id"], "p": project_id or ""}).mappings().first()
        if row is not None:
            return dict(row)
    if ref.get("role_key") and project_id:
        row = c.execute(text(q + "project_id = :p AND role_key = :r LIMIT 1"),
                        {"p": project_id, "r": ref["role_key"]}).mappings().first()
        if row is not None:
            return dict(row)
    return None


def agent_display(c: Any, project_id: Optional[str], ref: Any,
                  fallback: str = FALLBACK) -> dict[str, Optional[str]]:
    """{name, role, role_key, agent_id} for a stored agent reference. `name` is always
    something a person can read; `role` is the role's display name when it differs."""
    parsed = parse_reference(ref)
    row = _role_row(c, project_id, parsed) if (parsed.get("agent_id") or parsed.get("role_key")) else None
    role_key = (row or {}).get("role_key") or parsed.get("role_key")
    role = None
    callsign = None
    if row is not None:
        spec = row.get("spec") or {}
        if isinstance(spec, str):
            try:
                spec = json.loads(spec or "{}")
            except ValueError:
                spec = {}
        callsign = persona_of({"role_key": role_key, "spec": spec}).get("callsign")
        role = row.get("display_name") or None
    if not role and role_key:
        role = humanize_role(role_key)
    name = parsed.get("name") or (str(callsign).strip() if callsign else None) or role or fallback
    return {"name": name, "role": role if role and role != name else None,
            "role_key": role_key, "agent_id": parsed.get("agent_id")}


def agent_name(c: Any, project_id: Optional[str], ref: Any, fallback: str = FALLBACK) -> str:
    return str(agent_display(c, project_id, ref, fallback)["name"])
