"""A revocable always-allow for one tool (BOSSAPP-817).

Since BOSSAPP-815 an always-allow is a row in ``tool_policies_v2`` with ``source='decision'``
(tool_policy_engine), so the call path reads it: every MCP road decides through
tool_policy_engine.decide. These three helpers are the narrow API older callers used; they write
and read those rows. The legacy ``tool_policies`` table is left in place and no longer written.

The usual way such a row is made is the "Always allow this" option on an approved Ask card
(decisions._handle_approved_follow_on -> tool_policy_engine.grant_from_decision).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.engine import Connection

from .tool_policy_engine import SCHEMA_SQL, create_rule, list_rules, revoke_rule

__all__ = ["SCHEMA_SQL", "grant_always_allow", "list_always_allow", "revoke_always_allow"]


def grant_always_allow(
    connection: Connection,
    *,
    project_id: str,
    server_id: Optional[str],
    tool: str,
    schema_sha256: Optional[str],
    source_decision_id: Optional[str],
    role_key: Optional[str] = None,
    actor: str = "boss",
) -> str:
    """Insert one active always-allow rule and return its id. A second grant inserts another
    rule; it does not update the first. Scoped to ``role_key``'s agent when given, else the
    project; the tier floor still holds at decide time (a tier >= 2 tool keeps asking)."""
    rule = create_rule(connection, project_id, {
        "scope": f"agent:{role_key}" if role_key else "project",
        "priority": 50,
        "effect": "allow",
        "selector": {"server": server_id or "*", "tool": tool},
        "schema_sha256": schema_sha256,
    }, actor=actor, source="decision", source_decision_id=source_decision_id)
    return str(rule["id"])


def revoke_always_allow(connection: Connection, *, project_id: str, policy_id: str,
                        actor: str = "boss") -> None:
    """Mark one project's always-allow rule revoked. Missing rows raise LookupError."""
    revoke_rule(connection, project_id, policy_id, actor=actor)


def list_always_allow(connection: Connection, *, project_id: str) -> list[dict]:
    """Active always-allow rules for this project, newest first."""
    rows = [r for r in list_rules(connection, project_id, source="decision")
            if r["status"] == "active" and r.get("project_id") == project_id]
    rows.sort(key=lambda r: (str(r.get("created_at") or ""), str(r["id"])), reverse=True)
    return [{"id": r["id"], "tool": (r.get("selector") or {}).get("tool"),
             "schema_sha256": r.get("schema_sha256"),
             "source_decision_id": r.get("source_decision_id"),
             "server_id": (r.get("selector") or {}).get("server")} for r in rows]
