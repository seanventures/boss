"""KG P6b: the external_agent step refuses to run without a Knowledge-Graph MCP endpoint.

An outside (a bank's) agent that BOSS connects is handed a KG MCP URL to call back into, read
from `BOSS_KG_MCP_URL` by the step. That variable only reaches the container if the compose
file BOSS ships forwards it; set in `.env` but never listed on the service, the step would go on
refusing to run on an otherwise-correct install. The community compose declares a `kg-mcp`
service for exactly this, so both services that run agent work must point at it.

This pins the join: `scripts/install/community-compose.yml` forwards `BOSS_KG_MCP_URL` to the
api and the worker, at the in-network endpoint the `kg-mcp` service serves.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "scripts" / "install" / "community-compose.yml"

#: Services that run (or lease and run) agent work and so need the KG MCP endpoint.
NEEDS_KG_MCP = ("api", "worker")


@pytest.fixture(scope="module")
def compose() -> str:
    if not COMPOSE.exists():  # pragma: no cover - engine installed standalone
        pytest.skip("the community compose file is not in this checkout")
    return COMPOSE.read_text()


def _service_block(text: str, name: str) -> str:
    """One service's YAML, up to whatever service is declared next (by indentation)."""
    start = text.index(f"\n  {name}:")
    rest = text[start + 1:]
    following = re.search(r"\n  [a-z][a-z0-9-]*:\n", rest)
    return rest[:following.start()] if following else rest


def test_the_compose_declares_a_kg_mcp_service(compose: str) -> None:
    block = _service_block(compose, "kg-mcp")
    assert "serve-mcp" in block, "the kg-mcp service must serve the Knowledge-Graph MCP endpoint"
    assert "8790" in block, "the kg-mcp service must listen on the documented port"


def test_kg_mcp_url_is_forwarded_to_the_services_that_run_agents(compose: str) -> None:
    missing = []
    for service in NEEDS_KG_MCP:
        block = _service_block(compose, service)
        match = re.search(r"BOSS_KG_MCP_URL:\s*(\S+)", block)
        if not match:
            missing.append(f"{service} does not receive BOSS_KG_MCP_URL")
            continue
        value = match.group(1)
        if "kg-mcp" not in value or "8790" not in value:
            missing.append(f"{service} BOSS_KG_MCP_URL does not point at the kg-mcp service: {value}")
    assert not missing, (
        "the external_agent step refuses to run without BOSS_KG_MCP_URL; it must reach the "
        f"services that run agents: {missing}"
    )
