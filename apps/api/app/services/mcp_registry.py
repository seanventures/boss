from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id, normalize_data_boundary


_MCP_COLUMNS = (
    "id, name, url, transport, risk_tier, data_boundary, enabled, description, "
    "publisher, provenance, tool_risk_overrides, created_at"
)


def _validated_risk(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 3:
        raise ValueError("risk_tier must be an integer between 0 and 3")
    return value


def _validated_overrides(value: Optional[dict[str, int]]) -> dict[str, int]:
    overrides = value or {}
    if any(
        not isinstance(name, str)
        or not name.strip()
        or isinstance(risk, bool)
        or not isinstance(risk, int)
        or risk < 0
        or risk > 3
        for name, risk in overrides.items()
    ):
        raise ValueError("tool risk overrides must map tool names to integers 0-3")
    return overrides


def _validated_server_identity(name: str, url: str, transport: str) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("MCP server name must be a non-empty string")
    if not isinstance(transport, str) or not transport.strip():
        raise ValueError("MCP transport must be a non-empty string")
    parsed = urlparse(url) if isinstance(url, str) else None
    if parsed is None or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("MCP server url must be an absolute HTTP(S) URL")


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
        "publisher": r["publisher"] or {},
        "provenance": r["provenance"] or {},
        "tool_risk_overrides": r["tool_risk_overrides"] or {},
        "created_at": r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else r["created_at"],
    }


def list_mcp_servers(connection: Connection, project_id: str, enabled_only: bool = False) -> list[dict]:
    sql = (
        f"SELECT {_MCP_COLUMNS} "
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
    publisher: Optional[dict[str, Any]] = None,
    provenance: Optional[dict[str, Any]] = None,
    tool_risk_overrides: Optional[dict[str, int]] = None,
    actor: str = "system",
) -> dict:
    _validated_server_identity(name, url, transport)
    risk_tier = _validated_risk(risk_tier)
    data_boundary = normalize_data_boundary(data_boundary)
    tool_risk_overrides = _validated_overrides(tool_risk_overrides)
    server_id = make_id("mcp")
    connection.execute(
        text(
            """
            INSERT INTO mcp_servers
              (id, project_id, name, url, transport, risk_tier, data_boundary,
               description, publisher, provenance, tool_risk_overrides)
            VALUES (:id, :p, :name, :url, :transport, :risk, :db, :desc,
                    CAST(:publisher AS JSONB), CAST(:provenance AS JSONB),
                    CAST(:tool_risk_overrides AS JSONB))
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
            "publisher": json.dumps(publisher or {}),
            "provenance": json.dumps(provenance or {}),
            "tool_risk_overrides": json.dumps(tool_risk_overrides or {}),
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
            f"SELECT {_MCP_COLUMNS} "
            "FROM mcp_servers WHERE id = :id"
        ),
        {"id": server_id},
    ).mappings().first()
    return _row(row)


def set_mcp_enabled(connection: Connection, project_id: str, server_id: str, enabled: bool, actor: str = "system") -> Optional[dict]:
    row = connection.execute(
        text(
            "UPDATE mcp_servers SET enabled = :e WHERE id = :id AND project_id = :p "
            f"RETURNING {_MCP_COLUMNS}"
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
    transport: str = "http",
    risk_tier: int = 1,
    data_boundary: str = "internal",
    description: Optional[str] = None,
    publisher: Optional[dict[str, Any]] = None,
    provenance: Optional[dict[str, Any]] = None,
    tool_risk_overrides: Optional[dict[str, int]] = None,
) -> dict:
    """Idempotently register a server by (project, name) — used by bootstrap so the
    built-in BOSS MCP servers are always present without duplicating on restart."""
    _validated_server_identity(name, url, transport)
    risk_tier = _validated_risk(risk_tier)
    data_boundary = normalize_data_boundary(data_boundary)
    tool_risk_overrides = _validated_overrides(tool_risk_overrides)
    existing = connection.execute(
        text("SELECT id FROM mcp_servers WHERE project_id = :p AND name = :n"),
        {"p": project_id, "n": name},
    ).mappings().first()
    if existing:
        row = connection.execute(
            text(
                "UPDATE mcp_servers SET url = :url, transport = :transport, risk_tier = :risk, data_boundary = :db, description = :desc, "
                "publisher = CAST(:publisher AS JSONB), provenance = CAST(:provenance AS JSONB), "
                "tool_risk_overrides = CAST(:tool_risk_overrides AS JSONB) "
                "WHERE id = :id "
                f"RETURNING {_MCP_COLUMNS}"
            ),
            {
                "url": url, "transport": transport, "risk": int(risk_tier), "db": data_boundary,
                "desc": description, "id": existing["id"],
                "publisher": json.dumps(publisher or {}),
                "provenance": json.dumps(provenance or {}),
                "tool_risk_overrides": json.dumps(tool_risk_overrides or {}),
            },
        ).mappings().first()
        return _row(row)
    return register_mcp_server(
        connection, project_id, name, url,
        transport=transport, risk_tier=risk_tier, data_boundary=data_boundary, description=description,
        publisher=publisher, provenance=provenance,
        tool_risk_overrides=tool_risk_overrides, actor="bootstrap",
    )
