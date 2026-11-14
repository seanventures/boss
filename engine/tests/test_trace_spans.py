"""AP-P4 (BOSSAPP-1055): every kernel run writes act-parented spans, through a MANDATORY trace
middleware — run, step, model, tool, subagent, interrupt — with W3C ids, so the run is the
parent (`traceparent`) of everything it did. No database here: services/traces.DbSpanSink is
the same contract on Postgres (apps/api/tests/test_ap_p4_traces_and_evals.py).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from boss_engine import kernel
from boss_engine.kernel import presets
from boss_engine.kernel.governance import GOVERNANCE_NAMES
from boss_engine.kernel.middleware import MiddlewareError, build_chain
from boss_engine.kernel.scripted import Call, Say, ScriptedEngine
from boss_engine.kernel.toolset import FunctionToolSet, Tool
from boss_engine.kernel.trace import (CURRENT, MemorySpanSink, Trace, parse_traceparent,
                                      traceparent)
from boss_engine.kernel.types import ToolSchema

ARGS = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"],
        "additionalProperties": False}
REPO = Path(__file__).resolve().parents[2]


def _toolset(**extra):
    tools = [Tool(ToolSchema("read_logs", "Read logs.", ARGS),
                  lambda a: {"ok": True, "lines": [f"{a['q']}: ok"]}),
             Tool(ToolSchema("deploy", "Deploy.", ARGS, kind="write"),
                  lambda a: {"status": "awaiting_approval", "decision_id": "dec_1"},
                  run_approved=lambda a: {"ok": True, "deployed": a["q"]})]
    tools += [Tool(ToolSchema(name, name, ARGS), fn) for name, fn in extra.items()]
    return FunctionToolSet(tools)


def _spec(script, sink, **kw):
    return kernel.RunSpec(engine=ScriptedEngine(list(script), cost_usd_micros=1500),
                          tools=kw.pop("tools", None) or _toolset(),
                          policies=kw.pop("policies", None) or presets.agent_loop(6),
                          project_id="p", system="sys",
                          messages=[{"role": "user", "content": "check the logs"}],
                          sink=sink, sleep=lambda s: None, actor="agent:triage", **kw)


def _by_kind(sink, kind):
    return [s for s in sink.spans.values() if s.kind == kind]


def test_trace_is_mandatory_governance_and_cannot_be_replaced():
    assert GOVERNANCE_NAMES[0] == "trace"
    names = build_chain(_spec([], MemorySpanSink())).names()
    assert names[-len(GOVERNANCE_NAMES):] == list(GOVERNANCE_NAMES)

    class Mine(Trace):
        name = "trace"

    with pytest.raises(MiddlewareError):
        build_chain(_spec([], MemorySpanSink(), middleware=(Mine(),)))


def test_a_run_is_a_tree_of_run_step_model_and_tool_spans():
    sink = MemorySpanSink()
    result = kernel.run(_spec([Call("read_logs", {"q": "r1"}), Say("all good")], sink))
    assert result.status == "completed"
    tree = sink.tree()
    kinds = [(depth, span.kind) for depth, span in tree]
    assert kinds == [(0, "run"), (1, "step"), (2, "model"), (2, "tool"), (1, "step"),
                     (2, "model")]
    run = tree[0][1]
    assert run.parent_span_id is None and run.status == "ok" and run.agent_key == "triage"
    assert {s.trace_id for s in sink.spans.values()} == {run.trace_id}
    assert all(s.run_id == result.run_id for s in sink.spans.values())
    tool = _by_kind(sink, "tool")[0]
    assert tool.name == "read_logs" and tool.status == "ok"
    assert tool.input == {"arguments": {"q": "r1"}}
    assert tool.output["content"]["lines"] == ["r1: ok"]
    assert all(s.ended_at is not None and s.ended_at >= s.started_at for s in sink.spans.values())


def test_each_model_span_names_its_llm_calls_row_by_ledger_seq():
    sink = MemorySpanSink()
    kernel.run(_spec([Call("read_logs", {"q": "r1"}), Say("done")], sink))
    responses = [e for e in sink.events if e.kind == "model.response"]
    models = sorted(_by_kind(sink, "model"), key=lambda s: s.seq)
    # The ledger sink writes one llm_calls row per model.response with (run_id, seq) = the
    # event's: the model span carries the same seq, so its cost is READ, never copied.
    assert [m.seq for m in models] == [e.seq for e in responses]
    assert models[0].output["tool_calls"] == [{"name": "read_logs", "arguments": {"q": "r1"}}]
    assert models[1].output["text"] == "done"
    assert models[0].input["new_messages"][-1]["content"] == "check the logs"
    assert models[0].attributes["usage"]["cost_usd_micros"] == 1500


def test_a_failed_model_attempt_is_its_own_error_span():
    from boss_engine.kernel.scripted import Fail

    class Overloaded(Exception):
        status_code = 529

    sink = MemorySpanSink()
    result = kernel.run(_spec([Fail(Overloaded("overloaded")), Say("ok")], sink))
    assert result.status == "completed"
    models = sorted(_by_kind(sink, "model"), key=lambda s: s.seq)
    assert [m.status for m in models] == ["error", "ok"]
    errors = [e for e in sink.events if e.kind == "model.error"]
    assert models[0].seq == errors[0].seq


def test_a_held_call_is_an_interrupt_until_the_run_resumes():
    sink = MemorySpanSink()
    store = kernel.MemoryCheckpointStore() if hasattr(kernel, "MemoryCheckpointStore") else None
    spec = _spec([Call("deploy", {"q": "v2"}), Say("deployed")], sink, store=store)
    result = kernel.run(spec)
    assert result.status == "suspended"
    interrupt = _by_kind(sink, "interrupt")[0]
    assert interrupt.status == "held" and interrupt.decision_id == "dec_1"
    assert _by_kind(sink, "tool")[0].status == "held"
    assert _by_kind(sink, "run")[0].status == "held"
    pending = result.pending
    resumed = kernel.resume(_spec([Say("deployed")], sink, run_id=result.run_id, store=store),
                            decision={"approved": True, "decision_id": "dec_1",
                                      "args_sha256": pending.get("args_sha256")})
    assert resumed.status == "completed", resumed.error
    interrupt = sink.spans[interrupt.span_id]
    assert interrupt.status == "ok" and interrupt.attributes.get("resumed") is True
    runs = _by_kind(sink, "run")
    assert len(runs) == 1 and runs[0].status == "ok", "a resume continues the SAME run span"
    approved = [s for s in _by_kind(sink, "tool") if s.attributes.get("approved")]
    assert approved and approved[0].status == "ok"


def test_a_run_started_inside_a_tool_call_is_a_child_of_that_tool_span():
    """A subagent run in process: the child's run span is parented to the parent's tool span,
    in the same trace — no plumbing, through trace.CURRENT."""
    sink = MemorySpanSink()
    seen = {}

    def task(args):
        seen["traceparent"] = CURRENT.get()
        child = kernel.run(_spec([Say("child done")], sink))
        return {"ok": True, "run_id": child.run_id}

    parent = kernel.run(_spec([Call("task", {"q": "look"}), Say("parent done")], sink,
                              tools=_toolset(task=task)))
    assert parent.status == "completed"
    sub = _by_kind(sink, "subagent")[0]
    assert sub.name == "task" and seen["traceparent"] == sub.traceparent
    child_run = [s for s in _by_kind(sink, "run") if s.run_id != parent.run_id][0]
    assert child_run.parent_span_id == sub.span_id
    assert child_run.trace_id == sub.trace_id
    assert sub.child_run_id == child_run.run_id
    assert CURRENT.get() is None


def test_a_traceparent_in_the_spec_makes_the_run_its_child():
    sink = MemorySpanSink()
    header = traceparent("4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7")
    kernel.run(_spec([Say("hi")], sink, extra={"traceparent": header}))
    run = _by_kind(sink, "run")[0]
    assert run.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert run.parent_span_id == "00f067aa0ba902b7"
    assert parse_traceparent("00-" + "0" * 32 + "-00f067aa0ba902b7-01") is None
    assert parse_traceparent("garbage") is None


def test_an_mcp_call_inside_a_tool_forwards_the_tool_spans_traceparent():
    from boss_engine.services.mcp_headers_policy import call_headers, current_traceparent

    sink = MemorySpanSink()
    got = {}

    def probe(args):
        got["now"] = current_traceparent()
        got["headers"] = call_headers({"forward": ["traceparent"]})
        return {"ok": True}

    kernel.run(_spec([Call("probe", {"q": "x"}), Say("ok")], sink, tools=_toolset(probe=probe)))
    tool = [s for s in _by_kind(sink, "tool") if s.name == "probe"][0]
    assert got["now"] == tool.traceparent
    assert got["headers"] == {"traceparent": tool.traceparent}
    assert current_traceparent() is None


def test_a_run_without_a_span_sink_still_runs():
    result = kernel.run(_spec([Say("hi")], None))
    assert result.status == "completed"


def test_payloads_are_redacted_before_they_are_stored():
    from boss_engine.services.trace_payloads import redact_payload

    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    value = {"messages": [{"role": "user", "content": f"push with {token} and mail a@b.example"}],
             "password": "hunter2-hunter2", "count": 12}
    clean, counts = redact_payload(value)
    flat = str(clean)
    assert token not in flat and "hunter2" not in flat and "a@b.example" not in flat
    assert clean["count"] == 12 and counts.get("EMAIL") == 1 and counts.get("SECRET")


def test_otel_export_is_off_by_default():
    from boss_engine.services import self_telemetry

    sink = MemorySpanSink()
    kernel.run(_spec([Say("hi")], sink))
    assert self_telemetry.tracing_on() is False
    assert self_telemetry.export_trace_span(_by_kind(sink, "run")[0]) is False


def test_agent_runs_are_counted_for_prometheus():
    from boss_engine.services import self_telemetry as st

    st._STATE["metrics"] = st.Metrics()
    try:
        st.metrics_count_agent_run("triage", "ok", 1200)
        body = st._STATE["metrics"].render()
    finally:
        st._STATE["metrics"] = None
    assert 'boss_agent_runs_total{agent="triage",status="ok"} 1' in body
    assert 'boss_agent_run_duration_ms_total{agent="triage"} 1200' in body


def test_the_bundled_prometheus_scrapes_boss_itself():
    yaml = pytest.importorskip("yaml")
    config = yaml.safe_load((REPO / "scripts" / "install" / "monitoring" / "prometheus.yml")
                            .read_text())
    targets = {t for job in config["scrape_configs"] for sc in job["static_configs"]
               for t in sc["targets"]}
    assert {"api:9464", "worker:9464"} <= targets
    install = (REPO / "install.sh").read_text()
    # With the bundled Prometheus, install.sh turns /metrics on for api and worker, on the
    # compose network only (the port is not published).
    assert 'MON_METRICS_PORT="${MON_METRICS_PORT:-9464}"' in install
    assert "BOSS_METRICS_PORT=${MON_METRICS_PORT}" in install
    compose = yaml.safe_load((REPO / "scripts" / "install" / "community-compose.yml").read_text())
    for service in ("api", "worker"):
        ports = " ".join(str(p) for p in compose["services"][service].get("ports") or [])
        assert "9464" not in ports


def test_the_otel_sdk_ships_in_the_image():
    requirements = (REPO / "apps" / "api" / "requirements.txt").read_text()
    assert "opentelemetry-sdk==" in requirements
    assert "opentelemetry-exporter-otlp-proto-http==" in requirements
