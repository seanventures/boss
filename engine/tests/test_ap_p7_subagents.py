"""AP-P7 (BOSSAPP-1058): deepagents-level depth — sub-agents, supervisors, handoffs, the single
writer, effort caps, write_todos and the rubric (docs/design/AGENT_PLATFORM.md §4.5, §5 Phase 7).

The acceptance tests of §5 Phase 7:

  (a) a research brief: a lead with 3 parallel read-only sub-agents vs a single-agent baseline,
      over a P4 dataset, token multiple reported          test_a_research_lead_vs_single_agent
  (b) a writer sub-agent on the parent's branch is refused test_b_*
  (c) a sub-agent interrupt surfaces in the parent thread's inbox and resumes the child
                                                            test_c_* (memory) + the Postgres one
  (d) revoking the parent's Passport cascades to every child run in one transaction
                                                            test_ap_p7_identity_cascade.py
  (e) a runaway "spawn 50" is capped by the spec, the cap in the trace
                                                            test_e_runaway_spawn_50_is_capped

Everything runs on the scripted engine (real-money model spend is frozen).
"""
from __future__ import annotations

import json
import os
import threading
import uuid

import pytest

from boss_engine import kernel
from boss_engine.kernel import presets
from boss_engine.kernel.interrupts import MemoryInterrupts, interrupt
from boss_engine.kernel.planning import Planning, Rubric, contract_misses
from boss_engine.kernel.scripted import Call, Calls, Reply, Say, ScriptedEngine
from boss_engine.kernel.subagents import (Coordination, MemoryBranches, SubagentDef,
                                          Subagents, cap_report, from_spec)
from boss_engine.kernel.toolset import FunctionToolSet, Tool
from boss_engine.kernel.trace import MemorySpanSink
from boss_engine.kernel.types import ToolSchema

OBJ = {"type": "object", "properties": {"q": {"type": "string"}, "x": {"type": "string"}},
       "additionalProperties": True}
BUDGET = {"enabled": True, "max_tokens": 50_000}


@pytest.fixture(autouse=True)
def _server_key(monkeypatch):
    """Hash-bound approvals are signed with the server key (argument_binding v2)."""
    from boss_engine.identity import authn_support

    monkeypatch.setattr(authn_support, "_SERVER_KEY", b"p7-test-key-" * 4)


def _tools(log=None, *, extra=()):
    log = log if log is not None else []

    def search(args):
        log.append(("search", args.get("q")))
        return {"hits": [f"fact about {args.get('q')}"]}

    def deploy(args):
        log.append(("deploy", args.get("x")))
        return {"ok": True}

    return FunctionToolSet([
        Tool(ToolSchema("search", "search the corpus", OBJ), search),
        Tool(ToolSchema("deploy", "deploy", OBJ, kind="write", idempotency="keyed"), deploy),
        *extra])


def _spec(script, middleware, *, tools=None, sink=None, interrupts=None, steps=8, **kw):
    engine = script if isinstance(script, ScriptedEngine) else ScriptedEngine(list(script))
    return kernel.RunSpec(engine=engine, tools=tools or _tools(), policies=presets.agent_loop(steps),
                          project_id="prj_p7", system="You are the lead.",
                          messages=[{"role": "user", "content": "brief"}],
                          middleware=tuple(middleware), sink=sink, interrupts=interrupts, **kw)


def _mw(defs=(), **co):
    coordination = Coordination(**{**BUDGET, "general_purpose": False, **co})
    return Subagents(coordination, defs={d.ref: d for d in defs})


def researcher(**kw):
    return SubagentDef(ref="researcher", description="reads the corpus", **kw)


def lead_and_children(lead_script, child):
    """One engine for a lead and its children: the lead (offered `task`) plays `lead_script`
    in order; a child plays `child` (an item, or fn(messages, tools) -> item)."""
    script = list(lead_script)

    def brain(messages, tools):
        if tools and "task" in tools:
            return script.pop(0) if script else Say("(lead script ended)")
        return child(messages, tools) if callable(child) else child

    return Reply(brain)


def _child_brain(messages, tools):
    """A sub-agent: search once for its brief, then report what it found."""
    last = messages[-1]
    if last["role"] == "user":
        return Call("search", {"q": last["content"]})
    return Say(f"REPORT[{messages[1]['content'] if len(messages) > 1 else ''}]: "
               + str(last.get("content"))[:80])


# ── the spec ───────────────────────────────────────────────────────────────────────────────────

class TestTheSpec:
    def _doc(self, **spec):
        return {"apiVersion": "boss.agent/v1", "kind": "Agent", "metadata": {"key": "lead_x"},
                "spec": spec}

    def test_multi_agent_is_off_unless_the_spec_opts_in(self):
        from boss_engine.agents import spec as S

        plain = S.load_spec(self._doc(subagents=[{"ref": "qa"}]))
        assert plain.coordination == {}
        assert from_spec(S.dump_spec(plain)["spec"]) is None, "listed sub-agents alone offer nothing"
        mw = from_spec({"subagents": [{"ref": "qa"}], "coordination": {"enabled": True,
                                                                        "max_tokens": 5000}})
        assert isinstance(mw, Subagents) and "qa" in mw.targets

    def test_an_enabled_supervisor_without_a_budget_is_refused(self):
        from boss_engine.agents import spec as S

        errors = S.validate(self._doc(subagents=[{"ref": "qa"}],
                                      coordination={"enabled": True}))
        assert any(e["path"] == "spec.coordination" and "budget" in e["message"] for e in errors)
        assert S.validate(self._doc(subagents=[{"ref": "qa"}],
                                    coordination={"enabled": True, "max_usd": 1.5})) == []

    def test_two_writers_on_one_branch_and_unknown_modes_are_refused(self):
        from boss_engine.agents import spec as S

        errors = S.validate(self._doc(subagents=[{"ref": "a", "writes": True},
                                                 {"ref": "b", "writes": True}]))
        assert any("one writer per branch" in e["message"] for e in errors)
        assert S.validate(self._doc(subagents=[{"ref": "a", "mode": "shared"}]))
        assert S.validate(self._doc(subagents=[{"ref": "a", "budget_share": 0.7},
                                               {"ref": "b", "budget_share": 0.6}]))

    def test_the_new_fields_round_trip_and_compile(self):
        from boss_engine.agents import spec as S

        doc = self._doc(subagents=[{"ref": "qa", "mode": "fork", "forward_verbatim": True,
                                    "report_tokens": 800}],
                        coordination={"enabled": True, "max_tokens": 9000, "max_parallel": 2},
                        planning={"write_todos": True},
                        self_check={"enabled": True, "max_attempts": 1},
                        output_contract={"sections": ["findings"]})
        spec = S.load_spec(doc)
        assert S.load_spec(S.dump_spec(spec)) == spec
        compiled = S.compile_spec(spec)
        assert compiled.subagents[0]["ref"] == "qa" and compiled.coordination["max_parallel"] == 2
        assert compiled.planning == {"write_todos": True}
        assert S.compiled_as_dict(compiled)["self_check"] == {"enabled": True, "max_attempts": 1}

    def test_a_spec_without_p7_fields_hashes_as_before(self):
        """Every shipped agent that opts into nothing keeps its content hash (no re-publish)."""
        from boss_engine.agents import spec as S

        body = S.dump_spec(S.load_spec(self._doc(mission="m")))["spec"]
        assert not {"subagents", "coordination", "planning", "self_check"} & set(body)


# ── synchronous sub-agents ─────────────────────────────────────────────────────────────────────

class TestTask:
    def test_isolated_child_sees_only_its_brief_and_returns_a_capped_report(self):
        long = "x" * 9000
        engine = ScriptedEngine([Call("task", {"subagent": "researcher", "brief": "find A"}),
                                 Say(long), Say("done")])
        mw = _mw([researcher(report_tokens=100)])
        result = kernel.run(_spec(engine, [mw]))
        assert result.status == "completed", result.error
        child_req = engine.requests[1]["messages"]
        assert [m["role"] for m in child_req] == ["system", "user"]
        assert child_req[1]["content"] == "find A", "isolated: the brief, nothing of the parent"
        report = result.trace[0]["result"]
        assert report["clipped"] and len(report["report"]) < 600
        assert "report clipped at 100 tokens" in report["report"]
        assert cap_report("short", 10) == ("short", False)

    def test_fork_mode_starts_from_the_parent_conversation(self):
        engine = ScriptedEngine([Say("noted"), Call("task", {"subagent": "researcher",
                                                             "brief": "go on",
                                                             "mode": "fork"}),
                                 Say("child report"), Say("done")])
        mw = _mw([researcher()])
        spec = _spec(engine, [mw])
        first = kernel.run(spec)
        assert first.status == "completed"
        spec2 = _spec(engine, [mw])
        spec2.messages = [{"role": "user", "content": "earlier question"},
                          {"role": "assistant", "content": "earlier answer"},
                          {"role": "user", "content": "now delegate"}]
        kernel.run(spec2)
        child = engine.requests[2]["messages"]
        contents = [m.get("content") for m in child]
        assert "earlier question" in contents and "earlier answer" in contents
        assert contents[-1] == "go on" and not any(m.get("tool_calls") for m in child), \
            "fork: the parent's conversation, without the unanswered turn that asked"

    def test_forward_verbatim_ends_the_parent_with_the_childs_words(self):
        engine = ScriptedEngine([Call("task", {"subagent": "researcher", "brief": "write it"}),
                                 Say("THE CHILD'S EXACT WORDS")])
        mw = _mw([researcher(forward_verbatim=True)])
        result = kernel.run(_spec(engine, [mw]))
        assert result.status == "completed" and result.text == "THE CHILD'S EXACT WORDS"
        assert engine.calls == 2, "no supervisor model call re-words the report"

    def test_parallel_readers_run_at_the_same_time(self):
        barrier = threading.Barrier(3, timeout=10)

        def meet(args):
            barrier.wait()  # only passes when all three children are inside a tool at once
            return {"met": True}

        tools = _tools(extra=(Tool(ToolSchema("meet", "meet", OBJ), meet),))

        def brain(messages, offered):
            if offered and "task" in offered:
                if messages[-1]["role"] == "user":
                    return Calls(*[Call("task", {"subagent": "researcher", "brief": f"part {i}"})
                                   for i in range(3)])
                return Say("merged")
            return Call("meet", {}) if messages[-1]["role"] == "user" else Say("ok")

        mw = _mw([researcher()], max_parallel=3)
        result = kernel.run(_spec(ScriptedEngine([], after=Reply(brain)), [mw], tools=tools))
        assert result.status == "completed", result.error
        assert [t["status"] for t in result.trace] == ["ok"] * 3, \
            "three children met at the barrier: they ran concurrently"

    def test_a_reader_sees_only_read_tools_and_the_child_is_a_child_span(self):
        sink = MemorySpanSink()
        engine = ScriptedEngine([Call("task", {"subagent": "researcher", "brief": "q"}),
                                 Say("final")], after=Reply(_child_brain))
        result = kernel.run(_spec(engine, [_mw([researcher()])], sink=sink))
        assert result.status == "completed"
        offered_to_child = engine.requests[1]["tools"]
        assert "search" in offered_to_child and "deploy" not in offered_to_child
        assert "task" not in offered_to_child, "depth 1: a child does not coordinate"
        spans = list(sink.spans.values())
        task_span = next(s for s in spans if s.kind == "subagent")
        child_run = next(s for s in spans if s.kind == "run" and s.run_id != result.run_id)
        assert child_run.parent_span_id == task_span.span_id
        assert task_span.child_run_id == child_run.run_id
        assert child_run.trace_id == task_span.trace_id

    def test_a_child_stops_at_its_budget_slice(self):
        engine = ScriptedEngine([], after=lead_and_children(
            [Call("task", {"subagent": "researcher", "brief": "q"}), Say("final")],
            Call("search", {"q": "again " * 50})), measure=True)
        mw = Subagents(Coordination(enabled=True, max_tokens=2000, max_subagents=4,
                                    general_purpose=False), defs={"researcher": researcher()})
        result = kernel.run(_spec(engine, [mw], steps=40))
        child = result.trace[0]["result"]
        assert child["status"] == "ok" and child.get("stopped") == "budget_slice"
        assert child["tokens"] >= 500 and child["tokens"] < 1500, \
            "the slice is a quarter of the cap (max_subagents 4), spent then stopped"
        assert result.text == "final"


# ── (b) the single writer ──────────────────────────────────────────────────────────────────────

class TestSingleWriter:
    def _run(self, defs, calls, *, branches, parent_writes=True):
        engine = ScriptedEngine([Calls(*calls), Say("done")], after=Say("child done"))
        mw = Subagents(Coordination(**BUDGET, general_purpose=False),
                       defs={d.ref: d for d in defs}, branch="boss/code/run1",
                       parent_writes=parent_writes, branches=branches)
        return kernel.run(_spec(engine, [mw]))

    def test_b_a_writer_subagent_on_the_parents_branch_is_refused(self):
        result = self._run([SubagentDef(ref="coder", writes=True)],
                           [Call("task", {"subagent": "coder", "brief": "edit"})],
                           branches=MemoryBranches())
        entry = result.trace[0]
        assert entry["status"] == "refused"
        assert entry["result"]["cap"] == "single_writer"
        assert "boss/code/run1" in entry["result"]["message"]

    def test_readers_on_the_branch_and_a_writer_on_its_own_branch_are_allowed(self):
        branches = MemoryBranches()
        result = self._run([SubagentDef(ref="reviewer"),
                            SubagentDef(ref="docs", writes=True, branch="boss/docs/run1")],
                           [Call("task", {"subagent": "reviewer", "brief": "read"}),
                            Call("task", {"subagent": "docs", "brief": "write docs"})],
                           branches=branches)
        assert [t["status"] for t in result.trace] == ["ok", "ok"]
        assert branches.holder("boss/docs/run1") is None, "a finished writer lets go"

    def test_a_branch_another_live_run_writes_is_refused(self):
        branches = MemoryBranches()
        branches.claim("boss/docs/run1", "arun_somebody_else")
        result = self._run([SubagentDef(ref="docs", writes=True, branch="boss/docs/run1")],
                           [Call("task", {"subagent": "docs", "brief": "x"})],
                           branches=branches, parent_writes=False)
        assert result.trace[0]["result"]["cap"] == "single_writer"
        assert "arun_somebody_else" in result.trace[0]["result"]["message"]


# ── (e) effort scaling ─────────────────────────────────────────────────────────────────────────

class TestEffortCaps:
    def test_e_runaway_spawn_50_is_capped_and_the_cap_is_in_the_trace(self):
        sink = MemorySpanSink()
        spawn_50 = Calls(*[Call("task", {"subagent": "researcher", "brief": f"look at {i}"})
                           for i in range(50)])
        engine = ScriptedEngine([], after=lead_and_children(
            [spawn_50, spawn_50, Say("answered with what I have")], Say("child report")))
        mw = Subagents(Coordination(enabled=True, max_tokens=100_000, max_subagents=5,
                                    max_parallel=3, general_purpose=False),
                       defs={"researcher": researcher()})
        result = kernel.run(_spec(engine, [mw], sink=sink, steps=6))
        assert result.status == "completed"
        ran = [t for t in result.trace if t["status"] == "ok"]
        capped = [t for t in result.trace if t["status"] == "refused"]
        assert len(ran) == 5, "max_subagents: five children in all, never 100"
        assert len(ran) + len(capped) == 100
        caps = {t["result"]["cap"] for t in capped}
        assert caps == {"max_parallel", "max_subagents"}
        events = [e for e in result.events if e.kind == "subagent.capped"]
        assert len(events) == 95 and events[0].data["cap"] == "max_parallel"
        cap_spans = [s for s in sink.spans.values() if s.kind == "subagent"
                     and s.status == "refused"]
        assert len(cap_spans) == 95
        assert all(s.output["content"]["error"] == "subagent_cap" for s in cap_spans)
        assert result.events and engine.calls == 3 + 5, "only the five children spent a call"

    def test_disabled_no_budget_and_depth_are_caps_too(self):
        def one(co):
            engine = ScriptedEngine([Call("task", {"subagent": "researcher", "brief": "b"}),
                                     Say("x")], after=Say("r"))
            mw = Subagents(co, defs={"researcher": researcher()})
            return kernel.run(_spec(engine, [mw])).trace[0]["result"]

        assert one(Coordination(enabled=False, max_tokens=9))["cap"] == "disabled"
        assert one(Coordination(enabled=True))["cap"] == "no_budget"
        deep = Subagents(Coordination(**BUDGET, max_depth=1), defs={"researcher": researcher()},
                         depth=1)
        engine = ScriptedEngine([Call("task", {"subagent": "researcher", "brief": "b"}),
                                 Say("x")])
        assert kernel.run(_spec(engine, [deep])).trace[0]["result"]["cap"] == "max_depth"

    def test_depth_two_lets_a_child_coordinate_inside_its_slice(self):
        def brain(messages, offered):
            depth_marker = messages[0]["content"]
            if "task" in (offered or []) and messages[-1]["role"] == "user":
                return Call("task", {"subagent": "researcher", "brief": "deeper"})
            return Say("leaf" if "task" not in (offered or []) else "mid " + depth_marker[:5])

        mw = Subagents(Coordination(enabled=True, max_tokens=60_000, max_depth=2,
                                    general_purpose=False), defs={"researcher": researcher()})
        result = kernel.run(_spec(ScriptedEngine([], after=Reply(brain)), [mw]))
        assert result.status == "completed"
        spawned = [e for e in result.events if e.kind == "subagent.started"]
        assert len(spawned) == 1 and spawned[0].data["depth"] == 1
        assert result.trace[0]["status"] == "ok"

    def test_the_token_cap_counts_every_childs_spend(self):
        engine = ScriptedEngine([], after=lead_and_children(
            [Call("task", {"subagent": "researcher", "brief": "a"}),
             Call("task", {"subagent": "researcher", "brief": "b"}), Say("done")],
            Say("r" * 4000)), measure=True)
        mw = Subagents(Coordination(enabled=True, max_tokens=1000, max_subagents=2,
                                    general_purpose=False),
                       defs={"researcher": researcher(budget_share=1.0)})
        result = kernel.run(_spec(engine, [mw], steps=10))
        assert result.trace[0]["status"] == "ok"
        assert result.trace[1]["result"]["cap"] == "max_tokens", \
            "the first child spent the whole token budget"


# ── (c) interrupts propagate (in memory; the Postgres inbox test is below) ─────────────────────

def _asking_tools(log):
    def send(args):
        log.append(("send", args.get("x")))
        return {"sent": args.get("x")}

    def ask(args):
        return interrupt("approval", {"summary": f"send {args.get('x')}?"})

    return _tools(log, extra=(Tool(ToolSchema("send_email", "send", OBJ, kind="write",
                                              idempotency="keyed"), ask, send),))


class TestInterruptPropagation:
    def test_c_a_childs_interrupt_waits_in_the_parents_name_and_resumes_the_child(self):
        log: list = []
        ports = MemoryInterrupts()
        writer = SubagentDef(ref="mailer", writes=True)
        mw = lambda: Subagents(Coordination(**BUDGET, general_purpose=False),  # noqa: E731
                               defs={"mailer": writer})
        engine = ScriptedEngine([Call("task", {"subagent": "mailer", "brief": "mail it"}),
                                 Call("send_email", {"x": "invoice-7"})])
        spec = _spec(engine, [mw()], tools=_asking_tools(log), interrupts=ports)
        held = kernel.run(spec)
        assert held.status == "suspended", held.error
        assert log == [], "nothing was sent before a person answered"
        opened = ports.opened[0]
        assert opened["run_id"] == held.run_id, "the Decision is the PARENT's (its inbox)"
        assert opened["payload"]["subagent"]["target"] == "mailer"
        item = held.pending["interrupts"][0]
        assert item["subagent"]["child_tool"] == "send_email" and item["tool"] == "task"
        approval = {**opened["binding"], "decision_id": opened["decision_id"], "approved": True}

        # The parent and child resume on one engine: the child's next turn, then the parent's.
        spec2 = _spec(ScriptedEngine([Say("CHILD: sent invoice-7"), Say("PARENT: done")]),
                      [mw()], tools=_asking_tools(log), interrupts=ports)
        spec2.run_id = held.run_id
        resumed = kernel.resume(spec2, answers={item["id"]: approval})
        assert resumed.status == "completed", resumed.error
        assert log == [("send", "invoice-7")], "the child's held call ran once, on the approval"
        task = next(t for t in resumed.trace if t["tool"] == "task" and t.get("resumed"))
        assert task["result"]["report"] == "CHILD: sent invoice-7"
        assert resumed.text == "PARENT: done"

    def test_a_changed_child_call_is_refused_by_the_childs_own_binding(self):
        log: list = []
        ports = MemoryInterrupts()
        mw = lambda: Subagents(Coordination(**BUDGET, general_purpose=False),  # noqa: E731
                               defs={"mailer": SubagentDef(ref="mailer", writes=True)})
        engine = ScriptedEngine([Call("task", {"subagent": "mailer", "brief": "mail"}),
                                 Call("send_email", {"x": "a"})])
        held = kernel.run(_spec(engine, [mw()], tools=_asking_tools(log), interrupts=ports))
        opened, item = ports.opened[0], held.pending["interrupts"][0]
        forged = {**opened["binding"], "args_sha256": "0" * 64,
                  "decision_id": opened["decision_id"], "approved": True}
        spec2 = _spec(ScriptedEngine([Say("PARENT: the child was refused")]), [mw()],
                      tools=_asking_tools(log), interrupts=ports)
        spec2.run_id = held.run_id
        out = kernel.resume(spec2, answers={item["id"]: forged})
        assert log == [], "an approval that does not bind the child's call releases nothing"
        task = next(t for t in out.trace if t["tool"] == "task" and t.get("resumed"))
        assert task["status"] == "error"

    def test_a_declined_child_call_reaches_the_child(self):
        log: list = []
        ports = MemoryInterrupts()
        mw = lambda: Subagents(Coordination(**BUDGET, general_purpose=False),  # noqa: E731
                               defs={"mailer": SubagentDef(ref="mailer", writes=True)})
        held = kernel.run(_spec(ScriptedEngine([Call("task", {"subagent": "mailer",
                                                              "brief": "mail"}),
                                                Call("send_email", {"x": "a"})]),
                                [mw()], tools=_asking_tools(log), interrupts=ports))
        item = held.pending["interrupts"][0]
        spec2 = _spec(ScriptedEngine([Say("CHILD: it was declined"), Say("PARENT: ok")]),
                      [mw()], tools=_asking_tools(log), interrupts=ports)
        spec2.run_id = held.run_id
        out = kernel.resume(spec2, answers={item["id"]: {"decision_id": item["decision_id"],
                                                         "rejected": True}})
        assert out.status == "completed" and log == []
        task = next(t for t in out.trace if t["tool"] == "task" and t.get("resumed"))
        assert task["result"]["report"] == "CHILD: it was declined"


# ── planning and the rubric ────────────────────────────────────────────────────────────────────

class TestPlanningAndRubric:
    def test_write_todos_keeps_the_plan_in_run_state_and_the_trace(self):
        sink = MemorySpanSink()
        todos = [{"content": "read the brief", "status": "completed"},
                 {"content": "search", "status": "in_progress"}]
        engine = ScriptedEngine([Call("write_todos", {"todos": todos}), Say("done")])
        result = kernel.run(_spec(engine, [Planning()], sink=sink))
        assert result.status == "completed"
        assert result.trace[0]["result"]["in_progress"] == 1
        assert [e.data["todos"] for e in result.events if e.kind == "todos.updated"] == [todos]
        span = next(s for s in sink.spans.values() if s.name == "write_todos")
        assert span.input["arguments"]["todos"] == todos
        plain = ScriptedEngine([Say("x")])
        kernel.run(_spec(plain, []))
        assert "write_todos" not in (plain.requests[0]["tools"] or []), "opt-in only"

    def test_the_rubric_asks_again_until_the_contract_is_met(self):
        contract = {"sections": ["findings", "recommendation"], "artifacts": ["RiskReport"]}
        engine = ScriptedEngine([Say("just some words"),
                                 Say("## Findings\nx\n## Recommendation\ny\nRisk report attached")])
        result = kernel.run(_spec(engine, [Rubric(contract, max_attempts=2)]))
        assert result.status == "completed" and result.text.startswith("## Findings")
        checks = [e.data for e in result.events if e.kind == "rubric.result"]
        assert [c["passed"] for c in checks] == [False, True]
        assert "SELF-CHECK" in engine.requests[1]["messages"][-1]["content"]

    def test_the_rubric_is_bounded_and_records_what_is_still_missing(self):
        engine = ScriptedEngine([Say("no"), Say("still no")])
        result = kernel.run(_spec(engine, [Rubric({"sections": ["findings"]}, max_attempts=1)]))
        assert result.status == "completed" and engine.calls == 2
        assert any("findings" in f.message for f in result.findings)

    def test_contract_checks(self):
        assert contract_misses("**Findings**: a", {"sections": ["findings"]}) == []
        assert contract_misses('{"a": 1}', {"schema": {"type": "object", "required": ["b"],
                                                       "properties": {"b": {}}}})
        assert contract_misses('{"b": 1}', {"schema": {"type": "object", "required": ["b"],
                                                       "properties": {"b": {}}}}) == []


# ── supervisors (D13) ──────────────────────────────────────────────────────────────────────────

class TestSupervisors:
    def test_the_three_panel_topologies_are_valid_supervisor_specs(self):
        from boss_engine.agents import spec as S
        from boss_engine.agents import supervisors as sup

        pr = sup.producer_reviewer("release_review", producer="software_engineer",
                                   reviewers=["qa", "security_reviewer"], rounds=2)
        fo = sup.fan_out("delivery_council", members=["qa", "cto", "chief_architect"])
        bd = sup.bounded_debate("arch_debate", debaters=["cto", "chief_architect"], rounds=3)
        for doc, topology in ((pr, "producer_reviewer"), (fo, "fan_out"),
                              (bd, "bounded_debate")):
            assert S.validate(doc) == []
            co = doc["spec"]["coordination"]
            assert co["topology"] == topology and co["enabled"] and co["max_tokens"]
            assert from_spec(doc["spec"]) is not None
        assert [s["ref"] for s in pr["spec"]["subagents"] if s.get("writes")] == \
            ["software_engineer"], "producer_reviewer: exactly one writer"
        assert not any(s.get("writes") for s in fo["spec"]["subagents"] + bd["spec"]["subagents"])
        assert pr["spec"]["coordination"]["max_subagents"] == 6
        assert bd["spec"]["coordination"]["max_subagents"] == 6
        assert fo["spec"]["coordination"]["max_parallel"] == 3

    def test_every_shipped_studio_panel_converts(self):
        import yaml

        from boss_engine.agents import spec as S
        from boss_engine.agents import supervisors as sup
        from boss_engine.agents.migration import specpack_root

        panels = sorted((specpack_root() / "panels").glob("*.yaml"))
        assert panels
        for path in panels:
            doc = sup.from_panel(yaml.safe_load(path.read_text()))
            assert S.validate(doc) == [], path.name

    def test_a_fan_out_supervisor_runs_its_members_in_parallel(self):
        from boss_engine.agents import supervisors as sup

        doc = sup.fan_out("council", members=["qa", "cto", "chief_architect"])
        mw = from_spec(doc["spec"])

        def brain(messages, offered):
            if offered and "task" in offered:
                if messages[-1]["role"] == "user":
                    return Calls(*[Call("task", {"subagent": m, "brief": "review X"})
                                   for m in ("qa", "cto", "chief_architect")])
                return Say("## Findings\n## Dissent\n## Evidence\n## Recommendation")
            return Say("member view")

        result = kernel.run(_spec(ScriptedEngine([], after=Reply(brain)), [mw]))
        assert result.status == "completed" and [t["status"] for t in result.trace] == ["ok"] * 3

    def test_the_captains_spec_is_its_roster(self):
        import yaml

        from boss_engine.agents import supervisors as sup
        from boss_engine.agents.migration import role_yaml_to_doc, specpack_root

        role = yaml.safe_load((specpack_root() / "roles" / "engineering_captain.yaml").read_text())
        doc = role_yaml_to_doc(role)
        assert doc["spec"]["subagents"] == sup.captain_subagents()
        assert doc["spec"]["coordination"]["enabled"] is False, "opt-in: shipped off"
        writers = [s["ref"] for s in doc["spec"]["subagents"] if s.get("writes")]
        assert writers == ["software_engineer"]
        cos = role_yaml_to_doc(yaml.safe_load(
            (specpack_root() / "roles" / "chief_of_staff.yaml").read_text()))
        assert cos["spec"]["coordination"]["async"] is True
        assert {s["ref"] for s in cos["spec"]["subagents"]} == {
            s["ref"] for s in sup.CHIEF_OF_STAFF_SUBAGENTS}
        names = {p.stem for p in (specpack_root() / "roles").glob("*.yaml")}
        assert {s["ref"] for s in doc["spec"]["subagents"] + cos["spec"]["subagents"]} <= names


# ── the catalogue ──────────────────────────────────────────────────────────────────────────────

def test_the_coordination_tools_are_catalogued_and_never_listed_over_mcp():
    from boss_engine.kernel.subagents import PERMISSION, RUN_ONLY_TOOLS
    from boss_engine.mcp import server as mcp_server
    from boss_engine.tools.builtin import build_default_registry

    reg = build_default_registry()
    specs = {s.name: s for s in reg.specs()}
    for name in RUN_ONLY_TOOLS:
        assert name in specs, name
        assert specs[name].exposure.surfaces == frozenset({"agent_step"})
    assert specs["task"].permission == PERMISSION == "agent:invoke"
    assert specs["transfer_to_agent"].exposure.surfaces == frozenset({"chat"})
    assert mcp_server._RUN_ONLY_TOOLS == RUN_ONLY_TOOLS


def test_an_async_task_is_an_a2a_task():
    from boss_engine.protocols.a2a_types import Task
    from boss_engine.services.subagent_runs import task_of

    waiting = task_of({"id": "arun_c", "thread_id": "thr_c", "status": "awaiting_approval",
                       "pending": {"decision_id": "dec_1", "interrupt_id": "int_1"},
                       "finalize_ctx": {"subagent": {"target": "sre"}}, "parent_run_id": "p"})
    assert waiting["id"] == "arun_c" and waiting["contextId"] == "thr_c"
    assert waiting["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    data = [p["data"] for p in waiting["status"]["message"]["parts"] if "data" in p][0]
    assert data == {"decision_id": "dec_1", "interrupt_id": "int_1"}
    done = Task.from_dict(task_of({"id": "arun_c", "thread_id": "thr_c", "status": "completed",
                                   "output_text": "the report"}))
    assert done.state.terminal and done.artifacts[0].parts[0].text == "the report"


# ═══ On Postgres ═══════════════════════════════════════════════════════════════════════════════

TEST_DB = os.environ.get("BOSS_TEST_DATABASE_URL",
                         "postgresql+psycopg://boss:boss@localhost:5432/boss_test")


@pytest.fixture(scope="module")
def eng():
    from sqlalchemy import create_engine, text

    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM agent_branch_writers LIMIT 1"))
            c.execute(text("SELECT 1 FROM agent_checkpoints LIMIT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable or without the AP-P7 schema: {error}")
    return engine


@pytest.fixture()
def project(eng):
    from sqlalchemy import text

    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
        if wid is None:
            pytest.skip("the test database has no workspace")
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'subagents', :s)"), {"i": pid, "w": wid, "s": pid})
        c.execute(text(
            "INSERT INTO project_members (id, project_id, member_id, display_name, member_type, "
            "role_keys, permissions) VALUES (:i, :p, 'ceo', 'ceo', 'human', '[\"ceo\"]'::jsonb, "
            "'[\"decision_resolve\"]'::jsonb) ON CONFLICT DO NOTHING"),
            {"i": f"pm_{pid}_ceo", "p": pid})
    yield pid
    with eng.begin() as c:
        runs = [r[0] for r in c.execute(text("SELECT id FROM agent_runs WHERE project_id = :p"),
                                        {"p": pid}).all()]
        c.execute(text("DELETE FROM agent_checkpoints WHERE run_id = ANY(:r) OR run_id LIKE :k"),
                  {"r": runs, "k": f"arun_{pid}%"})
        c.execute(text("DELETE FROM trace_spans WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM agent_subagent_notifications WHERE project_id = :p"),
                  {"p": pid})
        c.execute(text("DELETE FROM agent_branch_writers WHERE project_id = :p"), {"p": pid})
        for table in ("dataset_experiment_results", "dataset_experiments", "dataset_examples",
                      "datasets", "agent_acts", "jobs", "decisions", "events", "agent_runs",
                      "agent_threads", "roles", "project_members"):
            try:
                with c.begin_nested():
                    c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": pid})
            except Exception:  # noqa: BLE001 — a table this database lacks
                pass
        c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})


def _parent_row(c, project, run_id, thread_id):
    from boss_engine.services import agent_loop

    agent_loop._save_run(c, run_id, project, None, "running", [], [], agent_loop._zero_usage(),
                         {"thread_id": thread_id, "role_key": "engineering_captain"}, step=0)
    from sqlalchemy import text

    c.execute(text("UPDATE agent_runs SET thread_id = :t WHERE id = :r"),
              {"t": thread_id, "r": run_id})
    c.execute(text("UPDATE agent_threads SET active_run_id = :r, run_state = 'busy' "
                   "WHERE id = :t"), {"r": run_id, "t": thread_id})


def test_c_on_postgres_the_interrupt_is_in_the_parent_threads_inbox_and_resumes_the_child(
        eng, project):
    from sqlalchemy import text

    from boss_engine.config import get_settings
    from boss_engine.kernel.durable import PgCheckpointStore
    from boss_engine.services import agent_protocol, thread_runs
    from boss_engine.services.decisions import DecisionService
    from boss_engine.services.interrupts import DecisionInterrupts, answers_for_run

    log: list = []
    run_id = f"arun_{project}_lead"
    mw = lambda: Subagents(Coordination(**BUDGET, general_purpose=False),  # noqa: E731
                           defs={"mailer": SubagentDef(ref="mailer", writes=True)})
    with eng.begin() as c:
        tid = thread_runs.ensure_thread(c, project_id=project, actor="ceo",
                                        external_ref="p7-inbox", agent_id="engineering_captain")
        _parent_row(c, project, run_id, tid)
        spec = _spec(ScriptedEngine([Call("task", {"subagent": "mailer", "brief": "mail"}),
                                     Call("send_email", {"x": "INV-9"})]), [mw()],
                     tools=_asking_tools(log), interrupts=DecisionInterrupts(c, project_id=project),
                     store=PgCheckpointStore(c, thread_id=tid), persist=True)
        spec.run_id, spec.project_id, spec.thread_id = run_id, project, tid
        held = kernel.run(spec)
        assert held.status == "suspended", held.error
        thread_runs.finish(c, run_id, "awaiting_approval")
    with eng.begin() as c:
        inbox = agent_protocol._decisions_for(c, run_id)
        assert len(inbox) == 1 and inbox[0]["status"] == "pending", \
            "the child's question is in the PARENT thread's inbox"
        rec = c.execute(text("SELECT recommendation FROM decisions WHERE id = :d"),
                        {"d": inbox[0]["decision_id"]}).scalar()
        assert rec["run_id"] == run_id and rec["tool"] == "send_email"
        assert c.execute(text("SELECT run_state FROM agent_threads WHERE id = :t"),
                         {"t": tid}).scalar() == "interrupted"
        DecisionService(get_settings()).transition(c, inbox[0]["decision_id"], action="approve",
                                                   actor="ceo", note=None)
        queued = c.execute(text("SELECT payload->>'run_id' FROM jobs WHERE project_id = :p "
                                "AND kind = 'agent.resume'"), {"p": project}).scalars().all()
        assert queued == [run_id], "the answer resumes the PARENT run (which resumes the child)"
    with eng.begin() as c:
        store = PgCheckpointStore(c, thread_id=tid)
        from boss_engine.services.agent_loop import _waits_on_a_subagent

        assert _waits_on_a_subagent(store.load(run_id).pending), \
            "the worker's resume routes it to the kernel, which hands the answer to the child"
        answers = answers_for_run(c, store.load(run_id).pending)
        spec2 = _spec(ScriptedEngine([Say("CHILD: INV-9 sent"), Say("LEAD: done")]), [mw()],
                      tools=_asking_tools(log), interrupts=DecisionInterrupts(c, project_id=project),
                      store=store, persist=True)
        spec2.run_id, spec2.project_id, spec2.thread_id = run_id, project, tid
        done = kernel.resume(spec2, answers=answers)
    assert done.status == "completed", done.error
    assert log == [("send", "INV-9")]
    assert done.text == "LEAD: done"


def test_async_subagents_are_durable_runs_shaped_as_a2a_tasks_and_tell_their_parent(eng, project):
    from sqlalchemy import text

    from boss_engine.kernel.subagents import Subagents
    from boss_engine.services import thread_runs
    from boss_engine.services.subagent_runs import DbAsyncSubagents

    run_id = f"arun_{project}_cos"
    with eng.begin() as c:
        tid = thread_runs.ensure_thread(c, project_id=project, actor="ceo",
                                        external_ref="p7-async", agent_id="chief_of_staff")
        _parent_row(c, project, run_id, tid)
        port = DbAsyncSubagents(c, project_id=project, actor="agent:chief_of_staff")
        mw = Subagents(Coordination(**BUDGET, async_tools=True, general_purpose=False),
                       defs={"sre": SubagentDef(ref="sre", asynchronous=True)}, async_port=port)
        engine = ScriptedEngine([Call("start_subagent", {"subagent": "sre",
                                                         "brief": "why is checkout 500ing?"}),
                                 Say("started the SRE; I'll report back")])
        spec = _spec(engine, [mw])
        spec.run_id, spec.project_id, spec.thread_id = run_id, project, tid
        started = kernel.run(spec)
        assert started.status == "completed", started.error
        task = started.trace[0]["result"]["task"]
        child = task["id"]
        assert task["status"]["state"] == "TASK_STATE_SUBMITTED"
        assert task["contextId"] and task["contextId"] != tid, "the child has its own thread"
        row = c.execute(text("SELECT parent_run_id, trigger, status, job_id, agent_key FROM "
                             "agent_runs WHERE id = :r"), {"r": child}).mappings().first()
        assert row["parent_run_id"] == run_id and row["trigger"] == "subagent"
        assert row["status"] == "queued" and row["job_id"], "admitted WITH its job (P2)"
        assert row["agent_key"] == "sre"
        assert [t["id"] for t in port.list(run_id)] == [child]
        thread_runs.finish(c, run_id, "completed")  # the parent ended before the child

        # The worker finishes the child: the parent's thread is told, event-driven.
        c.execute(text("UPDATE agent_runs SET status = 'completed', output_text = 'DB pool "
                       "exhausted' WHERE id = :r"), {"r": child})
        thread_runs.finish(c, child, "completed")
        note = c.execute(text("SELECT state, delivered_to, report FROM "
                              "agent_subagent_notifications WHERE child_run_id = :r"),
                         {"r": child}).mappings().first()
        assert note["state"] == "TASK_STATE_COMPLETED" and note["report"] == "DB pool exhausted"
        woken = c.execute(text("SELECT id, trigger, messages FROM agent_runs WHERE thread_id = :t "
                               "AND trigger = 'subagent_notify'"), {"t": tid}).mappings().first()
        assert woken is not None and note["delivered_to"] == woken["id"], \
            "a parent that already ended is woken on its thread with the news"
        assert "DB pool exhausted" in json.dumps(woken["messages"])
        assert port.check(child)["status"]["state"] == "TASK_STATE_COMPLETED"
        assert port.check(child)["artifacts"][0]["parts"][0]["text"] == "DB pool exhausted"


def test_a_running_parent_reads_the_news_at_its_next_step_and_can_update_and_cancel(
        eng, project):
    from sqlalchemy import text

    from boss_engine.services import thread_runs
    from boss_engine.services.subagent_runs import DbAsyncSubagents

    run_id = f"arun_{project}_lead2"
    with eng.begin() as c:
        tid = thread_runs.ensure_thread(c, project_id=project, actor="ceo",
                                        external_ref="p7-async2", agent_id="chief_of_staff")
        _parent_row(c, project, run_id, tid)
        port = DbAsyncSubagents(c, project_id=project, actor="agent:chief_of_staff")
        seen: dict = {}

        def brain(messages, offered):
            news = [m["content"] for m in messages if m["role"] == "user"
                    and "[sub-agent update]" in str(m["content"])]
            if not seen.get("started"):
                seen["started"] = True
                return Calls(Call("start_subagent", {"subagent": "sre", "brief": "a"}),
                             Call("start_subagent", {"subagent": "finops_sentinel",
                                                     "brief": "b"}))
            if not seen.get("finished"):
                seen["finished"] = True
                kids = [t["id"] for t in port.list(run_id)]
                seen["kids"] = kids
                c.execute(text("UPDATE agent_runs SET status = 'completed', output_text = "
                               "'found it' WHERE id = :r"), {"r": kids[0]})
                thread_runs.finish(c, kids[0], "completed")
                return Calls(Call("update_subagent", {"task_id": kids[1], "message": "faster"}),
                             Call("cancel_subagent", {"task_id": kids[1]}))
            seen["news"] = news
            return Say("done")

        mw = Subagents(Coordination(**BUDGET, async_tools=True, general_purpose=False,
                                    max_parallel=2),
                       defs={"sre": SubagentDef(ref="sre"),
                             "finops_sentinel": SubagentDef(ref="finops_sentinel")},
                       async_port=port)
        spec = _spec(ScriptedEngine([], after=Reply(brain)), [mw])
        spec.run_id, spec.project_id, spec.thread_id = run_id, project, tid
        result = kernel.run(spec)
        assert result.status == "completed", result.error
        assert any("found it" in n for n in seen["news"]), \
            "the finished child's report arrived as a message at the parent's next step"
        assert any("canceled" in n for n in seen["news"]), "and the cancelled one said so"
        update, cancel = result.trace[2]["result"]["task"], result.trace[3]["result"]["task"]
        assert update["id"] != seen["kids"][1], "an update is a new run on the child's thread"
        assert cancel["status"]["state"] == "TASK_STATE_CANCELED"


def test_db_single_writer_registry(eng, project):
    from boss_engine.services import thread_runs
    from boss_engine.services.subagent_runs import DbBranches

    with eng.begin() as c:
        tid = thread_runs.ensure_thread(c, project_id=project, actor="ceo",
                                        external_ref="p7-branch", agent_id="software_engineer")
        _parent_row(c, project, f"arun_{project}_w", tid)
        branches = DbBranches(c, project)
        assert branches.claim("boss/code/r1", f"arun_{project}_w") == ""
        mw = Subagents(Coordination(**BUDGET, general_purpose=False),
                       defs={"coder": SubagentDef(ref="coder", writes=True,
                                                  branch="boss/code/r1")},
                       branches=DbBranches(c, project))
        out = kernel.run(_spec(ScriptedEngine([Call("task", {"subagent": "coder", "brief": "x"}),
                                               Say("ok")]), [mw]))
        assert out.trace[0]["result"]["cap"] == "single_writer"
        thread_runs.finish(c, f"arun_{project}_w", "completed")
        assert branches.holder("boss/code/r1") is None, "an ended run holds nothing"


def test_a_handoff_sets_the_threads_active_agent_and_is_an_act(eng, project):
    from sqlalchemy import text

    from boss_engine.services import handoffs
    from boss_engine.services.agent_service import AgentService

    with eng.begin() as c:
        for key in ("sre", "qa"):
            c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name, spec) "
                           "VALUES (:i, :p, :k, :k, '{}'::jsonb) ON CONFLICT DO NOTHING"),
                      {"i": f"role_{project}_{key}", "p": project, "k": key})
        tid = f"thr_{project}"
        c.execute(text("INSERT INTO agent_threads (id, project_id, actor, surface, agent_role) "
                       "VALUES (:t, :p, 'ceo', 'chat', '')"), {"t": tid, "p": project})
        out = handoffs.transfer(c, project_id=project, thread_id=tid, to_agent="SRE",
                                actor="ceo", reason="a failing deploy")
        assert out["active_agent"] == "sre" and out["act_id"]
        act = c.execute(text("SELECT kind, role_key, how, thread_id, why FROM agent_acts WHERE "
                             "act_id = :a"), {"a": out["act_id"]}).mappings().first()
        assert dict(act) == {"kind": "handoff", "role_key": "sre", "how": "model_pick",
                             "thread_id": tid, "why": "a failing deploy"}
        assert AgentService._handed_to(c, tid) == "sre", "the next chat turn is the SRE's"
        with pytest.raises(handoffs.HandoffError):
            handoffs.transfer(c, project_id=project, thread_id=tid, to_agent="nobody_here",
                              actor="ceo")
        back = handoffs.transfer(c, project_id=project, thread_id=tid, to_agent="boss",
                                 actor="ceo")
        assert back["active_agent"] is None and handoffs.active_agent(c, tid) is None


# ── (a) research: a lead with 3 parallel readers vs one agent ──────────────────────────────────

def test_a_research_lead_vs_single_agent(eng, project):
    """Scripted engine (real-money spend is frozen): the harness, the dataset and the token
    multiple are real; the QUALITY comparison needs a real engine and a judge (pending the
    operator's spend approval) — the scripted answers are equally good by construction."""
    from boss_engine.services import multiagent_eval

    report = multiagent_eval.compare_research(eng, project, actor="ceo")
    lead, base = report["lead"], report["baseline"]
    assert lead["total"] == base["total"] == len(multiagent_eval.BRIEFS)
    assert lead["pass_rate"] == 1.0 and base["pass_rate"] == 1.0
    assert lead["subagents_per_brief"] == 3 and lead["parallel"] is True
    multiple = report["token_multiple"]
    assert 1.0 < multiple < 15.0, multiple
    assert report["judge"]["status"] == "pending_real_engine"
    print(f"\nAP-P7 (a) token multiple lead/single = {multiple:.2f}x "
          f"({lead['tokens']} vs {base['tokens']} tokens over {lead['total']} briefs)")


def test_an_agent_run_gets_coordination_only_when_its_published_spec_opts_in(eng, project):
    """agent_loop wires the P7 middleware from the agent's ACTIVE version: nothing for an agent
    that opts into nothing (every shipped agent today), the sub-agent tools with the database
    ports for a supervisor, and a background child's own budget slice."""
    from sqlalchemy import text

    from boss_engine.agents import supervisors as sup
    from boss_engine.kernel.planning import Planning, Rubric
    from boss_engine.kernel.subagents import SliceGuard, Subagents
    from boss_engine.services.agent_loop import coordination_middleware
    from boss_engine.services.subagent_runs import DbAsyncSubagents, DbBranches, OboExchange

    doc = sup.producer_reviewer("release_lead", producer="software_engineer",
                                reviewers=["qa"], rounds=1)
    with eng.begin() as c:
        for n, (key, body) in enumerate((("release_lead", doc),
                                         ("plain", {"apiVersion": "boss.agent/v1",
                                                    "kind": "Agent",
                                                    "metadata": {"key": "plain"},
                                                    "spec": {"mission": "m"}}))):
            c.execute(text("INSERT INTO agent_spec_versions (id, project_id, agent_key, version, "
                           "spec, content_hash, active, created_by) VALUES (:i, :p, :k, 1, "
                           "CAST(:s AS JSONB), 'sha256:x', true, 'ceo')"),
                      {"i": f"asv_{project}_{n}", "p": project, "k": key,
                       "s": json.dumps(body)})
        assert coordination_middleware(c, project, role="plain", run_id="r") == ()
        stack = coordination_middleware(c, project, role="release_lead", run_id="r",
                                        finalize_ctx={"branch": "boss/code/r"})
        kinds = [type(m) for m in stack]
        assert Planning in kinds and Rubric in kinds and Subagents in kinds
        mw = next(m for m in stack if isinstance(m, Subagents))
        assert isinstance(mw.branches, DbBranches) and isinstance(mw.identity, OboExchange)
        assert isinstance(mw.async_port, DbAsyncSubagents)
        assert mw.branch == "boss/code/r" and mw.parent_writes
        assert set(mw.targets) == {"software_engineer", "qa"}
        child = coordination_middleware(c, project, role="plain", run_id="r", finalize_ctx={
            "subagent": {"token_budget": 4000, "parent_run_id": "p"}})
        assert [type(m) for m in child] == [SliceGuard] and child[0].tokens == 4000
