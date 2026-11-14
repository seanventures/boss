"""AP-P5 acceptance (c) on a real database (docs/design/AGENT_PLATFORM.md §5 Phase 5; BOSSAPP-1056).

(c) A remote agent is callable as a BOSS sub-agent, with its cost and a remote span recorded:
    a fake A2A v1.0 agent and a fake Agent Protocol (LangGraph-style) server, each driven from a
    parent kernel run through services/remote_subagents (the adapter AP-P7's `task` tool calls).
    The remote's work joins the parent's trace (the traceparent it receives names the remote
    span), its reported cost is an llm_calls row of the parent run, and the parent's trace
    still reconciles. A remote task that asks for input surfaces as INPUT_REQUIRED, held, and
    an answer resumes it.

The remotes are httpx.MockTransport fakes speaking the wire formats (no official A2A SDK or
LangGraph server is installed in this environment).
"""
from __future__ import annotations

import json

import httpx
from sqlalchemy import text

from boss_engine import kernel
from boss_engine.kernel import presets
from boss_engine.kernel.ledger import DbSink
from boss_engine.kernel.scripted import Say, ScriptedEngine
from boss_engine.kernel.toolset import FunctionToolSet
from boss_engine.kernel.trace import parse_traceparent
from boss_engine.protocols import a2a_types as t
from boss_engine.services import remote_subagents as rs
from boss_engine.services.traces import run_trace

PROJECT = "boss-dogfood"


def _parent(engine) -> str:
    result = kernel.run(kernel.RunSpec(
        engine=ScriptedEngine([Say("delegating")], cost_usd_micros=1_000, model="scripted-v1"),
        tools=FunctionToolSet([]), policies=presets.agent_loop(4), project_id=PROJECT,
        system="You delegate.", messages=[{"role": "user", "content": "research it"}],
        sink=DbSink(engine), sleep=lambda s: None, actor="agent:lead"))
    assert result.status == "completed", result.error
    return result.run_id


def _root(engine, run_id: str) -> dict:
    with engine.connect() as c:
        return dict(c.execute(text("SELECT trace_id, span_id FROM trace_spans WHERE run_id = :r "
                                   "AND kind = 'run'"), {"r": run_id}).mappings().one())


def _remote_span(engine, run_id: str) -> dict:
    with engine.connect() as c:
        return dict(c.execute(text("SELECT * FROM trace_spans WHERE run_id = :r "
                                   "AND kind = 'subagent'"), {"r": run_id}).mappings().one())


def _cost_rows(engine, run_id: str) -> list:
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT engine, stage, model, cost_usd_micros, prompt_tokens, completion_tokens, ok "
            "FROM llm_calls WHERE run_id = :r AND stage = 'remote_agent'"), {"r": run_id}
        ).mappings()]


class FakeA2A:
    """A remote A2A agent: SendMessage → working; GetTask → working, then completed with its
    cost in metadata. With `ask`, the first answer is INPUT_REQUIRED until a follow-up message."""

    def __init__(self, *, ask: bool = False):
        self.ask = ask
        self.polls = 0
        self.traceparents: list = []
        self.methods: list = []
        self.answered = False

    def _task(self, state: str, *, text_: str = "", cost: bool = False) -> dict:
        task = {"id": "rt_1", "contextId": "rc_1", "status": {"state": state}}
        if text_ and state == "TASK_STATE_INPUT_REQUIRED":
            task["status"]["message"] = {"messageId": "m1", "role": "ROLE_AGENT",
                                         "parts": [{"text": text_}]}
        if state == "TASK_STATE_COMPLETED":
            task["artifacts"] = [{"artifactId": "a1", "parts": [{"text": text_}]}]
        if cost:
            task["metadata"] = {"usage": {"cost_usd_micros": 4_200, "input_tokens": 900,
                                          "output_tokens": 120}}
        return task

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.methods.append(body["method"])
        self.traceparents.append(request.headers.get("traceparent"))
        assert request.headers["a2a-version"] == "1.0"
        params = body["params"]
        if body["method"] == "SendMessage":
            if self.ask and params["message"].get("taskId") == "rt_1":
                self.answered = True
                result = {"task": self._task("TASK_STATE_COMPLETED", text_="Shipped after you "
                                             "said yes.", cost=True)}
            elif self.ask:
                result = {"task": self._task("TASK_STATE_INPUT_REQUIRED",
                                             text_="Which region should I scan?")}
            else:
                result = {"task": self._task("TASK_STATE_WORKING")}
        elif body["method"] == "GetTask":
            self.polls += 1
            result = (self._task("TASK_STATE_WORKING") if self.polls < 2 else
                      self._task("TASK_STATE_COMPLETED", text_="Three CVEs; two are patched.",
                                 cost=True))
        else:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "error": {"code": -32601, "message": "nope"}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


class FakeAgentProtocol:
    """A LangChain Agent Protocol server (/threads, /runs, /runs/{id}, /runs/{id}/wait)."""

    def __init__(self):
        self.polls = 0
        self.traceparents: list = []
        self.created: list = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.traceparents.append(request.headers.get("traceparent"))
        path = request.url.path
        if path == "/threads" and request.method == "POST":
            return httpx.Response(200, json={"thread_id": "th_9"})
        if path == "/runs" and request.method == "POST":
            self.created.append(json.loads(request.content))
            return httpx.Response(200, json={"run_id": "run_9", "thread_id": "th_9",
                                             "status": "pending", "metadata": {}})
        if path == "/runs/run_9":
            self.polls += 1
            done = self.polls >= 2
            return httpx.Response(200, json={
                "run_id": "run_9", "thread_id": "th_9",
                "status": "success" if done else "pending",
                "metadata": {"cost_usd": 0.0031} if done else {}})
        if path == "/runs/run_9/wait":
            return httpx.Response(200, json={"run": {"run_id": "run_9"}, "messages": [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": [{"type": "text",
                                                   "text": "The graph says: refund approved."}]}]})
        return httpx.Response(404)


def test_c_a_remote_a2a_agent_runs_as_a_sub_agent_with_cost_and_a_remote_span(engine):
    parent = _parent(engine)
    root = _root(engine, parent)
    fake = FakeA2A()
    ref = rs.RemoteAgentRef(protocol="a2a", url="https://scanner.example/a2a", name="cve-scanner",
                            auth_header="Bearer remote-key")
    out = rs.run(engine, project_id=PROJECT, parent_run_id=parent, ref=ref,
                 brief="Scan the payments service for CVEs.", timeout=30,
                 transport=httpx.MockTransport(fake), sleep=lambda s: None)

    task = out["task"]
    assert task["id"] == "rt_1" and task["contextId"] == "rc_1"
    assert task["status"]["state"] == "TASK_STATE_COMPLETED" and not out["timed_out"]
    assert out["report"] == "Three CVEs; two are patched."
    assert out["cost_usd_micros"] == 4_200
    assert fake.methods == ["SendMessage", "GetTask", "GetTask"]

    span = _remote_span(engine, parent)
    assert span["span_id"] == out["span_id"] and span["name"] == "remote:cve-scanner"
    assert span["status"] == "ok" and span["ended_at"] is not None
    assert span["trace_id"] == root["trace_id"] and span["parent_span_id"] == root["span_id"]
    attrs = span["attributes"]
    assert attrs["remote"] is True and attrs["protocol"] == "a2a"
    assert attrs["endpoint_host"] == "scanner.example" and attrs["remote_task_id"] == "rt_1"
    assert attrs["remote_context_id"] == "rc_1" and attrs["state"] == "TASK_STATE_COMPLETED"
    assert "remote-key" not in json.dumps(attrs, default=str)
    # The remote joined BOSS's trace: every request named the remote span.
    assert {parse_traceparent(tp) for tp in fake.traceparents} == \
        {(root["trace_id"], span["span_id"])}

    assert _cost_rows(engine, parent) == [{
        "engine": "a2a", "stage": "remote_agent", "model": "remote:cve-scanner",
        "cost_usd_micros": 4_200, "prompt_tokens": 900, "completion_tokens": 120, "ok": True}]
    with engine.connect() as c:
        tree = run_trace(c, PROJECT, parent)
    kinds = [s["kind"] for s in tree["spans"]]
    assert "subagent" in kinds
    assert tree["totals"]["reconciled"] is True
    assert tree["totals"]["llm_calls_cost_usd_micros"] == 1_000 + 4_200
    assert tree["totals"]["subagents"] == 1


def test_c_an_agent_protocol_server_runs_as_a_sub_agent_with_cost_and_a_remote_span(engine):
    parent = _parent(engine)
    root = _root(engine, parent)
    fake = FakeAgentProtocol()
    ref = rs.RemoteAgentRef(protocol="agent_protocol", url="https://graphs.example",
                            name="refunds-graph", agent_id="refunds")
    out = rs.run(engine, project_id=PROJECT, parent_run_id=parent, ref=ref,
                 brief="Should order 311 be refunded?", timeout=30,
                 transport=httpx.MockTransport(fake), sleep=lambda s: None)
    assert out["task"]["id"] == "run_9" and out["task"]["contextId"] == "th_9"
    assert out["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert out["report"] == "The graph says: refund approved."
    assert out["cost_usd_micros"] == 3_100
    assert fake.created[0]["agent_id"] == "refunds"
    assert fake.created[0]["input"]["messages"][0]["content"] == "Should order 311 be refunded?"
    span = _remote_span(engine, parent)
    assert span["attributes"]["protocol"] == "agent_protocol"
    assert span["trace_id"] == root["trace_id"]
    assert {parse_traceparent(tp) for tp in fake.traceparents} == \
        {(root["trace_id"], span["span_id"])}
    rows = _cost_rows(engine, parent)
    assert [(r["engine"], r["cost_usd_micros"]) for r in rows] == [("agent_protocol", 3_100)]
    with engine.connect() as c:
        assert run_trace(c, PROJECT, parent)["totals"]["reconciled"] is True


def test_c_a_remote_task_asking_for_input_is_held_then_resumed_by_an_answer(engine):
    parent = _parent(engine)
    fake = FakeA2A(ask=True)
    ref = rs.RemoteAgentRef(protocol="a2a", url="https://scanner.example/a2a", name="scanner")
    transport = httpx.MockTransport(fake)
    out = rs.run(engine, project_id=PROJECT, parent_run_id=parent, ref=ref,
                 brief="Scan it.", timeout=30, transport=transport, sleep=lambda s: None)
    assert out["task"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert out["report"] == "Which region should I scan?" and out["cost_usd_micros"] == 0
    assert _remote_span(engine, parent)["status"] == "held"
    assert _cost_rows(engine, parent) == [], "no cost is booked for a task still waiting"

    # The async view of the same task: check says the same; an answer resumes it.
    checked = rs.update(engine, project_id=PROJECT, ref=ref, handle=out["handle"],
                        answer="eu-west-1", transport=transport)
    assert fake.answered
    assert t.Task.from_dict(checked["task"]).state is t.TaskState.COMPLETED
    span = _remote_span(engine, parent)
    assert span["span_id"] == out["span_id"] and span["status"] == "ok"
    assert [r["cost_usd_micros"] for r in _cost_rows(engine, parent)] == [4_200]
    # Checking a finished task again books nothing twice.
    fake.polls = 5
    again = rs.check(engine, project_id=PROJECT, ref=ref, handle=checked["handle"],
                     transport=transport)
    assert again["report"] == "Three CVEs; two are patched."
    assert len(_cost_rows(engine, parent)) == 1
