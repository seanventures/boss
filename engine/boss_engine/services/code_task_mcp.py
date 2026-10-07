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

    def move_into(self, sandbox: Any) -> None:
        """Into the run's sandbox directory (sandbox_identity): the agent runs as the run's uid
        and must read its own file, and no other run's uid may (the run directory is 2770)."""
        if not self.path or sandbox is None:
            return
        old_dir = os.path.dirname(self.path)
        directory = _private_dir(sandbox)
        target = os.path.join(directory, "mcp.json")
        shutil.copyfile(self.path, target)
        os.chmod(target, 0o644)
        shutil.rmtree(old_dir, ignore_errors=True)
        from .resource_ledger import release_dir
        release_dir(old_dir, "moved")
        self.path = target

    def cleanup(self) -> None:
        if self.path:
            shutil.rmtree(os.path.dirname(self.path), ignore_errors=True)
            from .resource_ledger import release_dir
            release_dir(os.path.dirname(self.path), "cleanup")


def _private_dir(sandbox: Any) -> str:
    directory = tempfile.mkdtemp(prefix="mcp-", dir=str(sandbox.run_dir))
    os.chmod(directory, 0o2755)
    return directory


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
            # NO PASSTHROUGH (MCP security BCP): the file an agent's harness reads may carry only
            # the upstream server's own auth, never a BOSS session, key, run token or egress
            # credential. A server whose configured auth is one of those is left out.
            from boss_engine.identity.tokens import refuse_passthrough

            headers = refuse_passthrough(headers or {})
        except Exception:  # noqa: BLE001 — a token that cannot be read, or a BOSS credential: leave it out
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
    from .sandbox_identity import current as _sandbox

    sandbox = _sandbox()
    if sandbox is not None:
        # Inside the run's 2770 directory: readable by the run's uid, by no other run's.
        directory = _private_dir(sandbox)
        mode = 0o644
    else:
        directory = tempfile.mkdtemp(prefix="boss-mcp-")
        os.chmod(directory, 0o700)
        mode = 0o600
    from .resource_ledger import claim_dir
    claim_dir(directory)
    path = os.path.join(directory, "mcp.json")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"mcpServers": planned["servers"]}, handle)
    return HarnessMcp(path=path, servers=sorted(planned["servers"]),
                      disallowed=planned["disallowed"])
