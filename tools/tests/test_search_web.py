"""web-intel's search_web: one shape from three providers, the key never in the inputs
(skills-mcp E4, test 13).

Which provider and whose key travel with the REQUEST (X-Search-Provider and a bearer token,
set from Skills & MCP › Web search as the server's sign-in), so a key is never in a tool call's
arguments and never in a receipt. Exa answers without a key through its hosted MCP server.
"""
from __future__ import annotations

import httpx
import pytest

from tools import mcp_web_intel_server as web

SHAPE = {"title", "url", "snippet"}


class Recorder:
    def __init__(self, answer):
        self.answer = answer
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return httpx.Response(200, json=self.answer)


@pytest.fixture()
def http(monkeypatch):
    def install(answer):
        recorder = Recorder(answer)
        transport = httpx.MockTransport(recorder)

        def call(method):
            def go(url, **kwargs):
                kwargs.pop("timeout", None)
                with httpx.Client(transport=transport) as client:
                    return getattr(client, method)(url, **kwargs)
            return go

        monkeypatch.setattr(httpx, "post", call("post"))
        monkeypatch.setattr(httpx, "get", call("get"))
        return recorder

    return install


def _with(headers):
    token = web._REQUEST_HEADERS.set(headers)
    return token


def test_exa_with_a_key_uses_its_api(http):
    seen = http({"results": [{"title": "BOSS", "url": "https://boss.example", "text": "x" * 900}]})
    token = _with({"x-search-provider": "exa", "authorization": "Bearer exa-key-1234"})
    try:
        out = web._search_web({"query": "boss", "n": 3})
    finally:
        web._REQUEST_HEADERS.reset(token)
    assert out["provider"] == "exa" and set(out["results"][0]) == SHAPE
    assert len(out["results"][0]["snippet"]) <= 300
    assert seen.calls[0].headers["x-api-key"] == "exa-key-1234"


def test_tavily_and_brave_normalise_to_one_shape(http):
    http({"results": [{"title": "T", "url": "https://t.example", "content": "c"}]})
    token = _with({"X-Search-Provider": "tavily", "Authorization": "Bearer tv-1"})
    try:
        tavily = web._search_web({"query": "q"})
    finally:
        web._REQUEST_HEADERS.reset(token)
    seen = http({"web": {"results": [{"title": "B", "url": "https://b.example",
                                      "description": "d"}]}})
    token = _with({"X-Search-Provider": "brave", "Authorization": "Bearer br-1"})
    try:
        brave = web._search_web({"query": "q", "n": 99})
    finally:
        web._REQUEST_HEADERS.reset(token)
    assert set(tavily["results"][0]) == SHAPE and set(brave["results"][0]) == SHAPE
    assert seen.calls[0].headers["x-subscription-token"] == "br-1"
    assert seen.calls[0].url.params["count"] == "10", "n is capped at 10"


def test_a_provider_that_needs_a_key_says_so(http):
    http({})
    token = _with({"X-Search-Provider": "brave"})
    try:
        out = web._search_web({"query": "q"})
    finally:
        web._REQUEST_HEADERS.reset(token)
    assert "needs an API key" in out["error"]


def test_exa_without_a_key_takes_the_keyless_path(monkeypatch):
    called = {}

    class FakeClient:
        def __init__(self, settings, timeout=20.0):
            pass

        def call_tool(self, url, name, args, headers=None):
            called.update(url=url, name=name, args=args, headers=headers)
            return {"text": "Title: Exa\nURL: https://exa.ai\nText: search for agents\n"}

    import boss_engine.services.mcp_client as client_module

    monkeypatch.setattr(client_module, "MCPClient", FakeClient)
    token = _with({})
    try:
        out = web._search_web({"query": "agents", "n": 2})
    finally:
        web._REQUEST_HEADERS.reset(token)
    assert called["url"] == web.EXA_MCP_URL and called["name"] == "web_search_exa"
    assert out["results"] == [{"title": "Exa", "url": "https://exa.ai",
                               "snippet": "search for agents"}]


def test_search_web_is_offered_with_the_other_tools():
    assert "search_web" in {t["name"] for t in web.TOOLS}
    assert "search_web" in web.TOOL_FNS
