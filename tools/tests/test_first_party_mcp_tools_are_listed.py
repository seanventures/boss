"""The first-party MCP servers list the tools this tree implements.

A live tools/list on 2026-10-21 was empty for boss-devops-reference, web-intel and
doc-render while the API still marked them reachable. A probe that succeeds with no
tools overwrites the stored list. This drives those servers' real HTTP handlers through
the same MCP client the probe uses, so a catalogue that drops an implemented tool fails
here. It does not add tools and it does not call a live server.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from boss_engine.services.mcp_client import MCPClient, forget_sessions
from tools import mcp_demo_server, mcp_doc_render_server, mcp_web_intel_server

#: What each server offers, in the order its tools/list returns them.
OFFERED = {
    "boss-devops-reference": (
        mcp_demo_server,
        ["cloud_reference_pricing", "engineering_runbook"],
    ),
    "web-intel": (
        mcp_web_intel_server,
        ["fetch_page", "study_site", "render_mockup", "search_web"],
    ),
    "doc-render": (
        mcp_doc_render_server,
        ["render_markdown_pdf"],
    ),
}


@pytest.fixture()
def speak(monkeypatch):
    forget_sessions()

    def install(app):
        # The probe's client calls httpx.post. Point that at this server's app — no socket.
        client = TestClient(app)

        def post(url, **kwargs):
            kwargs.pop("timeout", None)
            return client.post(httpx.URL(url).path or "/mcp", **kwargs)

        monkeypatch.setattr("boss_engine.services.mcp_client.httpx.post", post)

    yield install
    forget_sessions()


@pytest.mark.parametrize("name", list(OFFERED))
def test_tools_list_returns_the_implemented_tools(speak, name):
    module, want = OFFERED[name]
    speak(module.app)
    listed = [tool["name"] for tool in MCPClient(None).list_tools("http://mcp-tools/mcp")]
    assert listed == want
    missing = [tool for tool in want if tool not in module.TOOL_FNS]
    assert missing == []
