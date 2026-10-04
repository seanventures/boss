"""Every MCP server BOSS seeds is served by the Community compose (skills-mcp RC3, test 5).

THE INCIDENT. BOSS seeded boss-devops-reference, web-intel and doc-render at 127.0.0.1 ports the
Community compose never started — inside the API container 127.0.0.1 is the API itself — so
every install opened on three dead servers, and fifteen roles held tools that could not answer.
"""
from __future__ import annotations

import pathlib
from urllib.parse import urlparse

import yaml

from boss_engine.bootstrap import BUILTIN_MCP_SERVERS

ROOT = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "scripts" / "install" / "community-compose.yml"


def test_every_seeded_host_is_a_compose_service_and_none_is_loopback():
    services = set((yaml.safe_load(COMPOSE.read_text()) or {})["services"])
    for server in BUILTIN_MCP_SERVERS:
        host = urlparse(server["url"]).hostname
        assert host not in ("127.0.0.1", "localhost", "::1"), server
        assert host in services, f"{server['name']} points at {host}, which compose never starts"


def test_the_service_serves_each_seeded_port():
    from tools.mcp_tools_mux import SERVERS

    served = {name: port for name, _module, port in SERVERS}
    for server in BUILTIN_MCP_SERVERS:
        assert urlparse(server["url"]).port == served[server["name"]], server["name"]


def test_the_service_builds_from_the_image_that_has_chromium():
    spec = (yaml.safe_load(COMPOSE.read_text()) or {})["services"]["mcp-tools"]
    assert spec["build"]["dockerfile"] == "Dockerfile.community"
    dockerfile = (ROOT / "Dockerfile.community").read_text()
    assert "FROM python-runtime AS mcp-tools" in dockerfile
    assert "tools.mcp_tools_mux" in dockerfile
