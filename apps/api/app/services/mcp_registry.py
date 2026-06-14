from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id


def _row(r) -> dict[str, Any]:
    return {
        "id": r["id"],
        "name": r["name"],
        "url": r["url"],
        "transport": r["transport"],
        "risk_tier": int(r["risk_tier"]),
        "data_boundary": r["data_boundary"],
        "enabled": bool(r["enabled"]),
        "description": r["description"],
        "created_at": r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else r["created_at"],
    }


def list_mcp_servers(connection: Connection, project_id: str, enabled_only: bool = False) -> list[dict]:
    sql = (
        "SELECT id, name, url, transport, risk_tier, data_boundary, enabled, description, created_at "
        "FROM mcp_servers WHERE project_id = :p"
    )
    if enabled_only:
        sql += " AND enabled = true"
    sql += " ORDER BY created_at"
    rows = connection.execute(text(sql), {"p": project_id}).mappings().all()
    return [_row(r) for r in rows]


def register_mcp_server(
    connection: Connection,
    project_id: str,
    name: str,
    url: str,
    *,
    transport: str = "http",
    risk_tier: int = 1,
    data_boundary: str = "internal",
    description: Optional[str] = None,
    actor: str = "system",
) -> dict:
    server_id = make_id("mcp")
    connection.execute(
        text(
            """
            INSERT INTO mcp_servers
              (id, project_id, name, url, transport, risk_tier, data_boundary, description)
            VALUES (:id, :p, :name, :url, :transport, :risk, :db, :desc)
            """
        ),
        {
            "id": server_id,
            "p": project_id,
            "name": name,
            "url": url,
            "transport": transport,
            "risk": int(risk_tier),
            "db": data_boundary,
            "desc": description,
        },
    )
    emit_event(
        connection,
        project_id,
        "mcp.server_registered",
        actor,
        "mcp_server",
        server_id,
        {"name": name, "url": url, "risk_tier": int(risk_tier)},
    )
    row = connection.execute(
        text(
            "SELECT id, name, url, transport, risk_tier, data_boundary, enabled, description, created_at "
            "FROM mcp_servers WHERE id = :id"
        ),
        {"id": server_id},
    ).mappings().first()
    return _row(row)


def set_mcp_enabled(connection: Connection, project_id: str, server_id: str, enabled: bool, actor: str = "system") -> Optional[dict]:
    row = connection.execute(
        text(
            "UPDATE mcp_servers SET enabled = :e WHERE id = :id AND project_id = :p "
            "RETURNING id, name, url, transport, risk_tier, data_boundary, enabled, description, created_at"
        ),
        {"e": bool(enabled), "id": server_id, "p": project_id},
    ).mappings().first()
    if row is None:
        return None
    emit_event(
        connection, project_id, "mcp.server_toggled", actor, "mcp_server", server_id,
        {"enabled": bool(enabled)},
    )
    return _row(row)


def delete_mcp_server(connection: Connection, project_id: str, server_id: str, actor: str = "system") -> bool:
    row = connection.execute(
        text("DELETE FROM mcp_servers WHERE id = :id AND project_id = :p RETURNING id"),
        {"id": server_id, "p": project_id},
    ).mappings().first()
    if row is None:
        return False
    emit_event(connection, project_id, "mcp.server_removed", actor, "mcp_server", server_id, {})
    return True


def ensure_mcp_server(
    connection: Connection,
    project_id: str,
    name: str,
    url: str,
    *,
    risk_tier: int = 1,
    data_boundary: str = "internal",
    description: Optional[str] = None,
) -> dict:
    """Idempotently register a server by (project, name) — used by bootstrap so the
    built-in BOSS MCP servers are always present without duplicating on restart."""
    existing = connection.execute(
        text("SELECT id FROM mcp_servers WHERE project_id = :p AND name = :n"),
        {"p": project_id, "n": name},
    ).mappings().first()
    if existing:
        row = connection.execute(
            text(
                "UPDATE mcp_servers SET url = :url, risk_tier = :risk, data_boundary = :db, description = :desc "
                "WHERE id = :id "
                "RETURNING id, name, url, transport, risk_tier, data_boundary, enabled, description, created_at"
            ),
            {"url": url, "risk": int(risk_tier), "db": data_boundary, "desc": description, "id": existing["id"]},
        ).mappings().first()
        return _row(row)
    return register_mcp_server(
        connection, project_id, name, url,
        risk_tier=risk_tier, data_boundary=data_boundary, description=description, actor="bootstrap",
    )
