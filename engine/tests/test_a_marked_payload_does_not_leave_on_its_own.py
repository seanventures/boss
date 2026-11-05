"""Content from outside the operator does not leave on its own (BOSSAPP-820).

A fetched page, an external tool result, or an untrusted document is marked. A later call that
would send that text to an external provider or a write tool waits; the send does not run.
The operator's own words are not marked, and they still leave. Approval is what lets the
marked payload leave.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from boss_engine.agent.engine_agent import EngineAgent
from boss_engine.services import agent_loop
from boss_engine.services.low_trust import Ledger, release, remember
from boss_engine.tools.registry import ToolRegistry, ToolSpec

PAYLOAD = "marked-payload-must-not-leave-820"


@pytest.mark.parametrize(
    "origin,sink",
    [
        ("fetched_page", "external_provider"),
        ("tool_result", "external_provider"),
        ("document", "write"),
    ],
)
def test_a_marked_payload_does_not_leave_on_its_own(origin, sink):
    ledger = Ledger()
    marked = remember(PAYLOAD, origin=origin, ledger=ledger)
    assert marked["untrusted"] is True
    assert marked["trust_origin"] == origin
    assert marked["content"] == PAYLOAD

    left: list = []

    def send(args):
        left.append(args)
        return {"ok": True, "echo": args}

    held = release({"body": PAYLOAD}, sink=sink, ledger=ledger, send=send)
    assert held["status"] == "awaiting_approval"
    assert held["sent"] is False
    assert left == [], "a marked payload left without approval"

    own = {"body": "the operator wrote this and it is not marked"}
    sent = release(own, sink=sink, ledger=ledger, send=send)
    assert sent["sent"] is True
    assert left == [own]

    released = release({"body": PAYLOAD}, sink=sink, ledger=ledger, send=send, approved=True)
    assert released["sent"] is True
    assert left[-1] == {"body": PAYLOAD}


def test_the_agent_does_not_run_a_write_that_carries_a_marked_payload():
    sent: list = []

    def handler(ctx, *, project_id, actor, args):
        sent.append(args)
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="publish_note",
            description="write a note",
            parameters={"type": "object", "properties": {"body": {"type": "string"}}},
            handler=handler,
            kind="write",
            permission="project:read",
        )
    )
    agent = EngineAgent(ctx=object(), registry=registry)
    remember(PAYLOAD, origin="document", ledger=agent._ledger)
    pending: list = []
    result = agent._run_tool(
        "publish_note",
        {"body": PAYLOAD},
        project_id="prj",
        actor="agent",
        pending=pending,
        allow_writes=True,
    )
    assert sent == [], "the write ran with a marked payload"
    assert result["status"] == "awaiting_approval"
    assert result.get("sent") is False
    assert pending and pending[0]["tool"] == "publish_note"


OWN = "the operator wrote this and it is not marked"


class _Pages:
    """One reviewed server. fetch_page brings a page in; post_note would send it out."""

    def __init__(self, _settings):
        pass

    def list_tools(self, url, headers=None):
        return [
            {
                "name": "fetch_page",
                "description": "Fetch a page",
                "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}},
            },
            {
                "name": "post_note",
                "description": "Send a note to an external provider",
                "inputSchema": {"type": "object", "properties": {"body": {"type": "string"}}},
            },
        ]


class _Script:
    """Ask for the page, then try to forward either the page or the operator's own words."""

    def __init__(self, sink: str, body: str):
        self.sink = sink
        self.body = body
        self.turn = 0

    def create_chat_completion(self, messages, tools=None):
        usage = {"model": "test", "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        self.turn += 1
        names = [tool["function"]["name"] for tool in tools or []]
        if self.turn == 1:
            name = next(item for item in names if "fetch_page" in item)
            args = json.dumps({"url": "https://example.invalid/page"})
        elif self.turn == 2:
            if self.sink == "write":
                name = "create_followup_task"
                args = json.dumps({"title": "note", "description": self.body})
            else:
                name = next(item for item in names if "post_note" in item)
                args = json.dumps({"body": self.body})
        else:
            raw = {"role": "assistant", "content": "done"}
            return {"raw": raw, "content": "done", "tool_calls": []}, usage
        raw = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": f"c{self.turn}",
                    "type": "function",
                    "function": {"name": name, "arguments": args},
                },
            ],
        }
        return (
            {
                "raw": raw,
                "content": "",
                "tool_calls": [{"id": f"c{self.turn}", "name": name, "arguments": args}],
            },
            usage,
        )


def _drive(monkeypatch, sink: str, body: str):
    """One unbounded run. Returns the drive result plus what actually left."""
    fetched: list = []
    wrote: list = []
    run = {
        "project_id": "project-a",
        "task_id": None,
        "messages": [{"role": "user", "content": "Read the page and pass on what it says."}],
        "transcript": [],
        "usage": {},
        "finalize_ctx": {},
        "step": 0,
    }

    def invoke_mcp(_connection, _settings, _project_id, _server, tool, args, **_kwargs):
        fetched.append((tool.get("name"), dict(args)))
        if tool.get("name") == "fetch_page":
            return {"content": PAYLOAD}
        return {"posted": True}

    def execute(_connection, _project_id, _settings, tool, args, actor="system", task_id=None):
        wrote.append((tool, dict(args)))
        return {"ok": True, "filed": True}

    monkeypatch.setattr(agent_loop, "load_agent_run", lambda *_a, **_k: run)
    monkeypatch.setattr(agent_loop, "project_chat_adapter", lambda *_a, **_k: _Script(sink, body))
    monkeypatch.setattr(agent_loop, "governed", lambda adapter, *_a, **_k: adapter)
    monkeypatch.setattr(agent_loop, "MCPClient", _Pages)
    monkeypatch.setattr(
        agent_loop,
        "list_mcp_servers",
        lambda *_a, **_k: [
            {
                "id": "srv_web",
                "name": "web",
                "url": "http://web.invalid/mcp",
                "risk_tier": 0,
                "enabled": True,
                "provenance": {"trusted": True},
            }
        ],
    )
    monkeypatch.setattr(agent_loop, "auth_headers", lambda *_a, **_k: {})
    monkeypatch.setattr(
        agent_loop,
        "_skill_resolution_for_run",
        lambda *_a, **_k: {"eager": [], "on_demand": []},
    )
    monkeypatch.setattr(agent_loop, "_save_run", lambda *_a, **_k: None)
    monkeypatch.setattr(agent_loop, "emit_event", lambda *_a, **_k: None)
    monkeypatch.setattr(agent_loop, "_invoke_mcp_skill", invoke_mcp)
    monkeypatch.setattr(agent_loop, "execute_agent_tool", execute)
    monkeypatch.setattr(agent_loop, "create_blocking_decision", lambda *_a, **_k: "dec_hold")
    settings = SimpleNamespace(
        boss_edition="community",
        agent_max_steps=4,
        agent_autonomous_risk_max=2,
    )
    result = agent_loop._drive_run(object(), settings, "arun_marked", 4)
    return result, fetched, wrote


@pytest.mark.parametrize("sink", ["external_provider", "write"])
def test_the_run_does_not_send_a_fetched_page_on_its_own(monkeypatch, sink):
    held, fetched, wrote = _drive(monkeypatch, sink, PAYLOAD)
    assert [name for name, _args in fetched] == ["fetch_page"]
    assert wrote == [], "a fetched page left through a write"
    assert held["status"] == "suspended"
    assert held.get("decision_id")

    released, fetched_own, wrote_own = _drive(monkeypatch, sink, OWN)
    assert released["status"] == "completed"
    if sink == "external_provider":
        assert ("post_note", {"body": OWN}) in fetched_own
    else:
        assert ("create_followup_task", {"title": "note", "description": OWN}) in wrote_own
