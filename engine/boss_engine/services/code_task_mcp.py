"""The MCP servers an agent holds, handed to the coding harness it runs (skills-mcp spec E6).

WHY. A code task runs Claude Code in a sandbox, and nothing passed it the MCP servers its agent
holds: giving Strata "Library docs" changed nothing while Strata coded. Now a code task started
AS an agent (`spec.role_key`) writes a per-run `mcp.json` holding exactly that agent's held,
switched-on, reachable and reviewed servers, and the harness is started with
`--strict-mcp-config --mcp-config <file>` — so it sees those servers and nothing else it might
find in its own config.

WHAT IS LEFT OUT, AND WHY
* An unreviewed server: every call to one waits for a person, and an unattended harness cannot
  wait (mcp_registry.needs_person).
* Tools set to Ask (and every tier >= 2 tool, whose floor is Ask): passed as `--disallowedTools`.
  A harness running unattended cannot pause for an approval mid-run (spec §7.6 is the later
  change that would let it). The Kit says so in that tool's tooltip.
* Tools set to Off, and tools outside the agent's grant: disallowed the same way.

The file carries resolved auth headers, so it is written 0600 in a private temporary directory
and removed when the run ends (`HarnessMcp.cleanup`).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy.engine import Connection


@dataclass
class HarnessMcp:
    """A written mcp.json and the tool names the harness must not call."""

    path: str
    servers: list[str]
    disallowed: list[str] = field(default_factory=list)

    def argv(self) -> list[str]:
        if not self.path:
            return ["--strict-mcp-config"]
        out = ["--strict-mcp-config", "--mcp-config", self.path]
        if self.disallowed:
            out += ["--disallowedTools", ",".join(self.disallowed)]
        return out

    def cleanup(self) -> None:
        if self.path:
            shutil.rmtree(os.path.dirname(self.path), ignore_errors=True)


def _cli_name(server_name: str) -> str:
    """Claude Code names a server's tools `mcp__<server>__<tool>`; keep the server key simple."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", server_name).strip("_") or "server"


def plan(connection: Connection, settings: Any, project_id: str, role_key: str
         ) -> dict[str, Any]:
    """{servers: {name: {type, url, headers}}, disallowed: [...]} for one agent. No file."""
    from .agents import held_mcp_servers
    from .mcp_registry import auth_headers, needs_person, tool_policy

    servers: dict[str, Any] = {}
    disallowed: list[str] = []
    for server in held_mcp_servers(connection, project_id, role_key):
        if server.get("reachable") is False or needs_person(server):
            continue
        name = _cli_name(str(server["name"]))
        granted = set(server.get("granted_tools") or [])
        known = [str(t.get("name")) for t in server.get("tools") or [] if t.get("name")]
        for tool in known:
            if (granted and tool not in granted) or tool_policy(server, tool) != "allow":
                disallowed.append(f"mcp__{name}__{tool}")
        if known and all(f"mcp__{name}__{t}" in disallowed for t in known):
            continue  # nothing on it the harness may call
        try:
            headers = auth_headers(connection, settings, project_id, server)
        except Exception:  # noqa: BLE001 — a token that cannot be read: leave the server out
            continue
        servers[name] = {"type": "http", "url": server["url"], **({"headers": headers}
                                                                  if headers else {})}
    return {"servers": servers,
            "disallowed": sorted(d for d in disallowed if d.split("__")[1] in servers)}


def prepare(connection: Connection, settings: Any, project_id: str,
            role_key: Optional[str]) -> Optional[HarnessMcp]:
    """Write the run's mcp.json, or None for a task not run as an agent.

    An agent holding nothing usable gets `--strict-mcp-config` and no config at all, so the
    harness does not fall back on whatever its own home directory configures."""
    role = str(role_key or "").strip().lower()
    if not role:
        return None
    planned = plan(connection, settings, project_id, role)
    if not planned["servers"]:
        return HarnessMcp(path="", servers=[])
    directory = tempfile.mkdtemp(prefix="boss-mcp-")
    os.chmod(directory, 0o700)
    path = os.path.join(directory, "mcp.json")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"mcpServers": planned["servers"]}, handle)
    return HarnessMcp(path=path, servers=sorted(planned["servers"]),
                      disallowed=planned["disallowed"])
