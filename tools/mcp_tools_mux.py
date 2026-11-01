"""mcp-tools — the MCP servers BOSS ships, in one process (skills-mcp spec E3).

BOSS seeds three MCP servers on every project — boss-devops-reference, web-intel and
doc-render — and fifteen roles of the cast hold them. They pointed at 127.0.0.1 ports that
the Community compose never started, so on every install they were dead on arrival: red
"unreachable" badges, and agents offered nothing. This is the process that serves them, run
as the `mcp-tools` compose service from the same image as the API (Chromium for web-intel's
rendering is already there):

    devops-reference  :8765/mcp   (tools/mcp_demo_server.py)
    web-intel         :8770/mcp   (tools/mcp_web_intel_server.py)
    doc-render        :8771/mcp   (tools/mcp_doc_render_server.py)
    itsm              :8772/mcp   (tools/mcp_itsm_mock.py)
    aml-sample        :8773/{case-management,kyc,transactions}/mcp   (tools/mcp_aml_mock.py)
    dq-sample         :8774/{eagle,olympus}/mcp                      (tools/mcp_dq_mock.py)

Run it locally the same way:

    python -m tools.mcp_tools_mux

`BOSS_MCP_TOOLS_BIND` (default 0.0.0.0) is the address the three listen on.
"""
from __future__ import annotations

import asyncio
import os

import uvicorn

#: (server name as BOSS seeds it, module, port). bootstrap.BUILTIN_MCP_SERVERS points at these.
def _ports() -> tuple[int, int, int, int]:
    """BOSS_MCP_TOOLS_PORTS="a,b,c,d" moves them (a machine where the defaults are taken); the
    API reads the same variable for the addresses it seeds (bootstrap.mcp_tools_ports)."""
    raw = os.environ.get("BOSS_MCP_TOOLS_PORTS", "").strip()
    try:
        ports = tuple(int(p) for p in raw.split(",")) if raw else (8765, 8770, 8771, 8772)
    except ValueError:
        ports = (8765, 8770, 8771, 8772)
    return ports if len(ports) == 4 else (8765, 8770, 8771, 8772)  # type: ignore[return-value]


def dq_sample_port() -> int:
    """The DQ sample's connectors (tools/mcp_dq_mock.py: eagle, olympus, one path each).
    BOSS_MCP_DQ_PORT moves it; the API reads the same variable for the addresses the DQ sample
    registers (knowledge/dq_sample.dq_mcp_servers)."""
    try:
        return int(os.environ.get("BOSS_MCP_DQ_PORT", "").strip() or 8774)
    except ValueError:
        return 8774


def aml_sample_port() -> int:
    """The AML sample's connectors (tools/mcp_aml_mock.py: case-management, kyc, transactions,
    one path each). BOSS_MCP_AML_PORT moves it; the API reads the same variable for the
    addresses the AML sample registers (knowledge/aml_sample.AML_MCP_SERVERS)."""
    try:
        return int(os.environ.get("BOSS_MCP_AML_PORT", "").strip() or 8773)
    except ValueError:
        return 8773


SERVERS = tuple(zip(("boss-devops-reference", "web-intel", "doc-render", "itsm"),
                    ("tools.mcp_demo_server", "tools.mcp_web_intel_server",
                     "tools.mcp_doc_render_server", "tools.mcp_itsm_mock"),
                    _ports())) + (("aml-sample", "tools.mcp_aml_mock", aml_sample_port()),
                                  ("dq-sample", "tools.mcp_dq_mock", dq_sample_port()))


async def serve() -> None:
    host = os.environ.get("BOSS_MCP_TOOLS_BIND", "0.0.0.0")
    servers = [
        uvicorn.Server(uvicorn.Config(f"{module}:app", host=host, port=port, log_level="info",
                                      access_log=False))
        for _name, module, port in SERVERS
    ]
    # One stops, all stop: a half-up mcp-tools would leave some agents' tools red with no
    # restart to fix it. compose restarts the whole service.
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for server in servers:
        server.should_exit = True
    await asyncio.gather(*tasks, return_exceptions=True)
    for task in done:
        task.result()


if __name__ == "__main__":
    asyncio.run(serve())
