"""Set the model connection and default model on many agents, in one action.

The pin is roster_model_assignments. The roles row is not written: name,
lineage, autonomy, skills and boundaries stay as they were. One receipt
records the action. A row that cannot take the change is listed, and the
rows that already took it stay changed.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .receipts import ReceiptService

_OMIT = object()
_EFFECT = "roster.model_bulk"
_MODEL_MAX = 512

_IDENTITY = """
    SELECT display_name, autonomy_level, reports_to, spec, agent_id, lifecycle,
           fingerprint, identity_version, born_by, sponsor
    FROM roles WHERE project_id = :p AND role_key = :k
"""


class RosterBulkError(Exception):
    """The whole action is refused. Nothing is written."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


class _RowError(Exception):
    """This agent did not change. Other agents in the action still can."""


def apply(
    connection: Connection,
    project_id: str,
    *,
    actor: str,
    dedupe_key: str,
    agents: Sequence[Any],
    connection_id: Any = _OMIT,
    default_model: Any = _OMIT,
) -> dict[str, Any]:
    """Apply one bulk change. A repeat of `dedupe_key` returns the first receipt."""
    who = str(actor or "").strip()
    if not who:
        raise RosterBulkError("Say who is making this change.")
    key = str(dedupe_key or "").strip()
    if not key:
        raise RosterBulkError("A bulk change needs a dedupe key.")
    project = _project(connection, project_id)
    prior = ReceiptService().get_by_dedupe_key(connection, project["org_id"], key)
    if prior is not None:
        return _replay(prior)

    chosen_connection = _optional_text(connection_id, "connection")
    chosen_model = _optional_text(default_model, "model")
    if chosen_connection is None and chosen_model is None:
        raise RosterBulkError("Choose a connection or default model to change.")
    if chosen_model is not None and len(chosen_model) > _MODEL_MAX:
        raise RosterBulkError("A default model is at most 512 characters.")
    parsed = _parse_agents(agents)
    sources = _sources(connection, project_id)
    if chosen_connection is not None and chosen_connection not in sources:
        raise RosterBulkError("That model connection is not connected.")

    receipt, created = ReceiptService().create_or_reuse(
        connection, project["org_id"], project["workspace_id"], project_id, key,
        _EFFECT, {"project_id": project_id, "kind": "roster_model"},
        {"agents": [item["role_key"] for item in parsed],
         "connection_id": chosen_connection, "default_model": chosen_model},
        data_boundary="internal",
    )
    if not created:
        fresh = ReceiptService().get_by_dedupe_key(connection, project["org_id"], key)
        if fresh is not None and fresh.get("status") == "succeeded":
            return _replay(fresh)
        raise RosterBulkError("This bulk change is still open.", status=409)

    applied: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in parsed:
        role_key = item["role_key"]
        if role_key in seen:
            failed.append({"role_key": role_key, "error": "Named twice in this change."})
            continue
        seen.add(role_key)
        try:
            with connection.begin_nested():
                applied.append(_one(
                    connection, project_id, role_key, actor=who,
                    receipt_id=receipt["id"], sources=sources,
                    connection_id=chosen_connection, default_model=chosen_model,
                    expected=item["expected"],
                ))
        except _RowError as exc:
            failed.append({"role_key": role_key, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 — the row is listed, the others stand
            failed.append({
                "role_key": role_key,
                "error": f"This row did not save. {exc.__class__.__name__}.",
            })

    outcome = {"applied": applied, "failed": failed,
               "connection_id": chosen_connection, "default_model": chosen_model}
    ReceiptService().mark_succeeded(connection, receipt["id"], None, outcome)
    return {"receipt_id": receipt["id"], "dedupe_key": key, "replayed": False,
            "applied": applied, "failed": failed}


def snapshot(connection: Connection, project_id: str) -> dict[str, Any]:
    """Agents and connected model sources, for the bench that calls apply."""
    _project(connection, project_id)
    rows = connection.execute(
        text(
            "SELECT r.role_key, r.display_name, r.lifecycle, "
            "a.connection_id, a.default_model "
            "FROM roles r "
            "LEFT JOIN roster_model_assignments a "
            "ON a.project_id = r.project_id AND a.role_key = r.role_key "
            "WHERE r.project_id = :p ORDER BY r.role_key"
        ),
        {"p": project_id},
    ).mappings().all()
    sources = connection.execute(
        text(
            "SELECT source_id, provider, supported_models FROM entitlement_sources "
            "WHERE project_id = :p AND connection_status = 'connected' AND status = 'active' "
            "ORDER BY source_id"
        ),
        {"p": project_id},
    ).mappings().all()
    connections = [
        {
            "id": str(row["source_id"]),
            "label": _source_label(row["provider"], row["source_id"]),
            "models": _model_list(row["supported_models"]),
        }
        for row in sources
    ]
    labels = {item["id"]: item["label"] for item in connections}
    return {
        "agents": [
            {
                "role_key": row["role_key"],
                "name": row["display_name"],
                "lifecycle": row["lifecycle"],
                "connection_id": row["connection_id"],
                "default_model": row["default_model"],
                "connection_label": labels.get(row["connection_id"] or "", "Local credentials"),
            }
            for row in rows
        ],
        "connections": connections,
    }


def _source_label(provider: Any, source_id: Any) -> str:
    raw = str(provider or source_id or "connection").replace("_", " ").replace("-", " ")
    return raw.title()


def _replay(receipt: Mapping[str, Any]) -> dict[str, Any]:
    if receipt.get("status") != "succeeded":
        raise RosterBulkError("This bulk change is still open.", status=409)
    body = receipt.get("result")
    if isinstance(body, str):
        body = json.loads(body)
    body = body or {}
    return {
        "receipt_id": receipt["id"],
        "dedupe_key": receipt["dedupe_key"],
        "replayed": True,
        "applied": list(body.get("applied") or []),
        "failed": list(body.get("failed") or []),
    }


def _project(connection: Connection, project_id: str) -> dict[str, Any]:
    row = connection.execute(
        text(
            "SELECT p.workspace_id, w.org_id FROM projects p "
            "JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :p"
        ),
        {"p": project_id},
    ).mappings().first()
    if row is None:
        raise RosterBulkError(f"No project {project_id}.", status=404)
    return {"workspace_id": row["workspace_id"], "org_id": row["org_id"]}


def _optional_text(value: Any, kind: str) -> Optional[str]:
    if value is _OMIT or value is None:
        return None
    if not isinstance(value, str):
        raise RosterBulkError(
            "The connection must be text." if kind == "connection"
            else "The default model must be text.")
    stripped = value.strip()
    return stripped or None


def _parse_agents(agents: Sequence[Any]) -> list[dict[str, Any]]:
    if not isinstance(agents, (list, tuple)) or not agents:
        raise RosterBulkError("Select at least one agent.")
    parsed = []
    for item in agents:
        if isinstance(item, str):
            role_key, expected = item, None
        elif isinstance(item, Mapping):
            role_key = item.get("role_key")
            expected = item.get("expected")
        else:
            raise RosterBulkError("Each agent is a role key.")
        key = str(role_key or "").strip().lower()
        if not key:
            raise RosterBulkError("An agent needs a role key.")
        if expected is not None and not isinstance(expected, Mapping):
            raise RosterBulkError("expected must name the connection and model you saw.")
        parsed.append({"role_key": key, "expected": None if expected is None else dict(expected)})
    return parsed


def _sources(connection: Connection, project_id: str) -> dict[str, list[str]]:
    rows = connection.execute(
        text(
            "SELECT source_id, supported_models FROM entitlement_sources "
            "WHERE project_id = :p AND connection_status = 'connected' AND status = 'active'"
        ),
        {"p": project_id},
    ).mappings()
    return {str(row["source_id"]): _model_list(row["supported_models"]) for row in rows}


def _model_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item.strip()]


def _one(
    connection: Connection,
    project_id: str,
    role_key: str,
    *,
    actor: str,
    receipt_id: str,
    sources: Mapping[str, list[str]],
    connection_id: Optional[str],
    default_model: Optional[str],
    expected: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    role = connection.execute(
        text("SELECT lifecycle FROM roles WHERE project_id = :p AND role_key = :k FOR UPDATE"),
        {"p": project_id, "k": role_key},
    ).mappings().first()
    if role is None:
        raise _RowError(f"No agent {role_key}.")
    if str(role["lifecycle"] or "") == "retired":
        raise _RowError("Retired agents stay off this change.")
    current = _pin(connection, project_id, role_key)
    if expected is not None:
        seen_connection = current["connection_id"] if current else None
        seen_model = current["default_model"] if current else None
        if seen_connection != expected.get("connection_id") or seen_model != expected.get("default_model"):
            raise _RowError("This agent's connection or model changed.")
    new_connection = connection_id if connection_id is not None else (
        current["connection_id"] if current else None)
    new_model = default_model if default_model is not None else (
        current["default_model"] if current else None)
    offered = sources.get(new_connection) if new_connection else None
    if new_connection and new_connection not in sources:
        raise _RowError("That model connection is not connected.")
    if offered and new_model and new_model not in offered:
        raise _RowError(f"{new_model} is not on {new_connection}.")
    before = _identity(connection, project_id, role_key)
    if current is None:
        connection.execute(
            text(
                "INSERT INTO roster_model_assignments "
                "(project_id, role_key, connection_id, default_model, updated_by, receipt_id) "
                "VALUES (:p, :k, :c, :m, :actor, :receipt)"
            ),
            {"p": project_id, "k": role_key, "c": new_connection, "m": new_model,
             "actor": actor, "receipt": receipt_id},
        )
    else:
        connection.execute(
            text(
                "UPDATE roster_model_assignments "
                "SET connection_id = :c, default_model = :m, updated_by = :actor, "
                "receipt_id = :receipt, updated_at = now() "
                "WHERE project_id = :p AND role_key = :k"
            ),
            {"p": project_id, "k": role_key, "c": new_connection, "m": new_model,
             "actor": actor, "receipt": receipt_id},
        )
    if _identity(connection, project_id, role_key) != before:
        raise _RowError("This change would alter who the agent is.")
    return {"role_key": role_key, "connection_id": new_connection, "default_model": new_model}


def _pin(connection: Connection, project_id: str, role_key: str) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text(
            "SELECT connection_id, default_model FROM roster_model_assignments "
            "WHERE project_id = :p AND role_key = :k"
        ),
        {"p": project_id, "k": role_key},
    ).mappings().first()
    return dict(row) if row else None


def _identity(connection: Connection, project_id: str, role_key: str) -> str:
    role = connection.execute(
        text(_IDENTITY), {"p": project_id, "k": role_key},
    ).mappings().first()
    skills = connection.execute(
        text(
            "SELECT skill_id, source FROM role_skills "
            "WHERE project_id = :p AND role_key = :k ORDER BY skill_id"
        ),
        {"p": project_id, "k": role_key},
    ).mappings().all()
    servers = connection.execute(
        text(
            "SELECT mcp_server_id, tools FROM role_mcp_servers "
            "WHERE project_id = :p AND role_key = :k ORDER BY mcp_server_id"
        ),
        {"p": project_id, "k": role_key},
    ).mappings().all()
    return json.dumps(
        {"role": dict(role) if role else None,
         "skills": [dict(row) for row in skills],
         "servers": [dict(row) for row in servers]},
        sort_keys=True, default=str,
    )
