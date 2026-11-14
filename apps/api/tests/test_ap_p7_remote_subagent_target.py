"""AP-P7 × AP-P5: a REMOTE agent as a sub-agent target (kernel/subagents through
services/subagent_runs.RemoteTarget → P5's services/remote_subagents adapter).

A supervisor's `task` reaches a fake A2A v1.0 agent: the remote's report is the tool result, its
reported tokens count against the supervisor's budget, its span sits UNDER the `task` span of the
parent's trace, and its cost is a ledger row of the parent run. `start_subagent` /
`check_subagent` drive the same remote asynchronously, as an A2A Task.
"""
from __future__ import annotations

import httpx
from sqlalchemy import text

from boss_engine import kernel
from boss_engine.kernel import presets
from boss_engine.kernel.ledger import DbSink
from boss_engine.kernel.scripted import Call, Say, ScriptedEngine
from boss_engine.kernel.subagents import Coordination, Subagents
from boss_engine.kernel.toolset import FunctionToolSet
from boss_engine.services import remote_subagents as rs
from boss_engine.services.subagent_runs import RemoteTarget

from test_ap_p5_remote_subagents import PROJECT, FakeA2A  # noqa: E402 (rootdir-relative)


def _supervisor(engine, fake, **co):
    ref = rs.RemoteAgentRef(protocol="a2a", url="https://scanner.example/a2a",
                            name="cve-scanner", auth_header="Bearer remote-key")
    target = RemoteTarget("cve_scanner", ref, engine=engine, project_id=PROJECT, timeout=30,
                          transport=httpx.MockTransport(fake), sleep=lambda s: None)
    return Subagents(Coordination(enabled=True, max_tokens=50_000, general_purpose=False, **co),
                     targets={"cve_scanner": target})


def _run(engine, script, mw):
    return kernel.run(kernel.RunSpec(
        engine=ScriptedEngine(script), tools=FunctionToolSet([]),
        policies=presets.agent_loop(6), project_id=PROJECT, system="You delegate.",
        messages=[{"role": "user", "content": "is payments safe?"}], middleware=(mw,),
        sink=DbSink(engine), sleep=lambda s: None, actor="agent:lead"))


def test_a_remote_a2a_agent_answers_a_task_call_under_the_task_span(engine):
    fake = FakeA2A()
    result = _run(engine, [Call("task", {"subagent": "cve_scanner", "brief": "Scan payments."}),
                           Say("done")], _supervisor(engine, fake))
    assert result.status == "completed", result.error
    out = result.trace[0]["result"]
    assert out["status"] == "ok" and out["report"] == "Three CVEs; two are patched."
    assert out["tokens"] == 900 + 120, "the remote's reported tokens count against the budget"
    with engine.connect() as c:
        spans = {r["kind"] + ":" + r["name"]: dict(r) for r in c.execute(text(
            "SELECT span_id, parent_span_id, kind, name, trace_id FROM trace_spans "
            "WHERE run_id = :r"), {"r": result.run_id}).mappings()}
        cost = c.execute(text("SELECT cost_usd_micros FROM llm_calls WHERE run_id = :r AND "
                              "stage = 'remote_agent'"), {"r": result.run_id}).scalars().all()
    task_span, remote = spans["subagent:task"], spans["subagent:remote:cve-scanner"]
    assert remote["parent_span_id"] == task_span["span_id"], "the remote is a child of `task`"
    assert remote["trace_id"] == task_span["trace_id"]
    assert cost == [4_200]


def test_a_remote_agent_runs_in_the_background_as_an_a2a_task(engine):
    fake = FakeA2A()
    mw = _supervisor(engine, fake, async_tools=True)
    result = _run(engine, [Call("start_subagent", {"subagent": "cve_scanner",
                                                   "brief": "Scan payments."}),
                           Call("check_subagent", {"task_id": "rt_1"}),
                           Call("check_subagent", {"task_id": "rt_1"}), Say("done")], mw)
    assert result.status == "completed", result.error
    started = result.trace[0]["result"]["task"]
    assert started["id"] == "rt_1" and started["contextId"] == "rc_1"
    assert started["metadata"]["target"] == "cve_scanner"
    last = result.trace[2]["result"]["task"]
    assert last["status"]["state"] == "TASK_STATE_COMPLETED"
    assert fake.methods[0] == "SendMessage" and "GetTask" in fake.methods
