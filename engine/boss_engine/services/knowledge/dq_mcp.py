"""One road to the DQ pipeline's connectors (the DQ Monitor, the Profile Service, the Service Desk): the project's REGISTERED
servers, through the registry's unattended call (an unreviewed or re-pointed server refuses like
any other). A connector that cannot answer is a coverage gap the step records — never a silent
omission and never a failed investigation. Every call made is appended to `calls` as
{mcp, tool, ok}, so the run view shows exactly what each step called."""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.engine import Connection


def call(connection: Connection, project_id: str, server_name: str, tool: str,
         args: dict[str, Any], calls: Optional[list[dict[str, Any]]] = None
         ) -> tuple[Optional[dict[str, Any]], str]:
    from .. import mcp_registry
    from ...config import get_settings

    server = mcp_registry.find_mcp_server(connection, project_id, server_name)
    if server is None or not server.get("enabled", True):
        return None, (f"the {server_name} connector is not registered on this project "
                      "(load the NorthPeak Bank data platform from Knowledge Graph › Documents)")
    record = {"mcp": server_name, "tool": tool, "ok": False}
    if calls is not None:
        calls.append(record)
    try:
        result = mcp_registry.call_tool_unattended(get_settings(), server, tool, args)
    except Exception as error:  # noqa: BLE001 — said as a gap, never fatal
        return None, f"{server_name}.{tool} did not answer: {type(error).__name__}: {error}"[:300]
    if not isinstance(result, dict):
        return None, f"{server_name}.{tool} answered something unreadable"
    if result.get("error"):
        return None, f"{server_name}.{tool}: {result['error']}"[:300]
    record["ok"] = True
    return result, ""
