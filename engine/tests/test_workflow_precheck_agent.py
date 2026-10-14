"""The MODEL pre-check (services.workflow_precheck_agent): would it have caught the socat failure?

A stub model stands in for every engine — no real model call is made here. What is proven is the
plumbing: evidence in, a tool call answered, a validated (model) finding out with its evidence
attached, malformed output handled, the cost and turn caps enforced, and the same behaviour
whichever engine the router hands back."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from boss_engine.services import model_routing
from boss_engine.services import workflow_precheck_agent as agent
from boss_engine.tools import workflow_precheck as cli

SANDBOX_ERROR = ("sandbox init failed: bwrap: execvp socat: No such file or directory "
                 "(claude_agent_sdk network proxy)")

EVIDENCE = {
    "workflow": {"key": "helpdesk-d1", "name": "HelpDesk Delivery (D1)",
                 "spec": {"steps": [{"id": "build", "binding": {"capability": "build"}},
                                    {"id": "deploy", "binding": {"capability": "deploy"}}]}},
    "coded_precheck": {"steps": [{"step_id": "build", "checks": [
        {"id": "sandbox.worker", "verdict": "ready", "evidence": "worker up"}]}]},
    "step_models": [{"step": "build", "job": "build", "engine": "claude_agent_sdk",
                     "model": "claude-sonnet", "source": "setup-anthropic"}],
    "recent_failures": {"failed_steps": [
        {"workflow_run_id": "wfr_7f3a9c21d0", "step_id": "build", "last_error": SANDBOX_ERROR}]},
    "boss_version": "1.0.0-rc10.6",
}
STEP_IDS = ["build", "deploy"]

SOCAT_FINDING = {"findings": [{
    "step": "build", "title": "Agent SDK sandbox cannot start: socat is not in the image",
    "verdict": "will_fail",
    "evidence": ["wfr_7f3a9c21d0 step build: 'bwrap: execvp socat: No such file or directory'",
                 "installed_binaries: socat missing"],
    "fix": "Add socat to the worker image, or route builds to an engine without the sandbox "
           "proxy.", "who": "person"}]}


def _msg(content: str = "", calls: list[dict] | None = None) -> dict[str, Any]:
    raw = {"role": "assistant", "content": content}
    if calls:
        raw["tool_calls"] = calls
    return {"content": content, "tool_calls": calls, "raw": raw}


class StubModel:
    """A scripted model. `script` items are callables(tools) -> (message, usage), or a fixed
    (message, usage). Records what each turn was offered."""

    def __init__(self, script: list[Any], *, engine: str = "native", cost: int = 1_000,
                 source: str = "setup-anthropic"):
        self.script = list(script)
        self.offered: list[Any] = []
        self.seen: list[list[dict]] = []
        self.cost = cost
        self._boss_choice = model_routing.ModelChoice(engine, "connected", source)
        self._boss_route = None

    def create_chat_completion(self, messages, tools=None, **_: Any):
        self.offered.append(tools)
        self.seen.append(list(messages))
        step = self.script.pop(0) if self.script else _msg(json.dumps({"findings": []}))
        message = step(tools) if callable(step) else step
        return message, {"model": "stub-model", "cost_usd_micros": self.cost}

    def create_text_response(self, prompt, system_prompt=None, **_: Any):  # pragma: no cover
        return "", json.dumps({"findings": []}), {"model": "stub-model"}


def _socat_script() -> list[Any]:
    return [_msg(calls=[{"id": "c1", "name": "installed_binaries",
                         "arguments": json.dumps({"names": ["socat", "bwrap"]})}]),
            _msg(json.dumps(SOCAT_FINDING))]


def _tools() -> tuple[list[dict], list[tuple[str, dict]], Any]:
    calls: list[tuple[str, dict]] = []

    def call_tool(name: str, args: dict) -> dict:
        calls.append((name, args))
        return {"found": {"bwrap": "/usr/bin/bwrap"}, "missing": ["socat"]}
    return [{"type": "function", "function": {"name": "installed_binaries"}}], calls, call_tool


def _run(model: StubModel, **kw: Any) -> dict[str, Any]:
    schemas, _, call_tool = _tools()
    return agent.run_agent(adapter=model, evidence=kw.pop("evidence", EVIDENCE), tools=schemas,
                           call_tool=kw.pop("call_tool", call_tool), step_ids=STEP_IDS, **kw)


# ── would it have caught it ───────────────────────────────────────────────────────────────────

def test_a_sandbox_init_error_lands_as_a_model_will_fail_with_its_evidence():
    model = StubModel(_socat_script(), engine="claude_agent_sdk")
    schemas, calls, call_tool = _tools()
    out = agent.run_agent(adapter=model, evidence=EVIDENCE, tools=schemas, call_tool=call_tool,
                          step_ids=STEP_IDS)
    assert calls == [("installed_binaries", {"names": ["socat", "bwrap"]})]
    assert out["stopped"] is None and out["turns"] == 2 and out["tool_calls"] == 1
    [finding] = out["findings"]
    assert finding["source"] == "(model)" and finding["step"] == "build"
    assert finding["verdict"] == "will_fail" and finding["grounded"] is True
    assert any("wfr_7f3a9c21d0" in e for e in finding["evidence"])
    assert out["counts"] == {"will_fail": 1, "risk": 0}
    # The evidence pack reached the model: the sandbox error was in what it was shown.
    assert "execvp socat" in model.seen[0][1]["content"]
    # The tool result went back as a tool message.
    assert any(m.get("role") == "tool" and "socat" in m["content"] for m in model.seen[1])


def test_the_findings_merge_as_model_lines_counted_on_their_own():
    report = {"workflow_name": "HelpDesk Delivery (D1)", "checkpoint": "pre_run",
              "project_id": "p1", "steps": [{"step_id": "build", "checks": [
                  {"id": "sandbox.worker", "verdict": "ready", "evidence": "worker up"}]}],
              "summary": {"ready": 1, "fixable": 0, "will_fail": 0, "unknown": 0,
                          "blocking": [], "sentence": "Nothing is known to fail."},
              "explain": []}
    result = _run(StubModel(_socat_script(), engine="claude_agent_sdk"))
    agent.merge(report, result)
    # Separate from the coded checks: not in blocking, its own counts.
    assert report["summary"]["blocking"] == []
    assert report["summary"]["model"] == {"will_fail": 1, "risk": 0}
    text = cli.render(report)
    assert re.search(r"^build\s+\(model\)\s+FAIL\s+Agent SDK sandbox cannot start", text, re.M)
    assert "evidence: wfr_7f3a9c21d0" in text
    assert "COUNTS: ready 1, fixable 0, will_fail 0, unknown 0; model: will_fail 1, risk 0" in text
    assert "model pre-check ran on claude_agent_sdk/stub-model via setup-anthropic" in text
    explained = cli.render_explain(report)
    assert "[will_fail] build:(model) Agent SDK sandbox cannot start" in explained
    assert "evidence: installed_binaries: socat missing" in explained
    assert json.loads(json.dumps(report, default=str))["model"]["findings"][0]["evidence"]


# ── malformed output ──────────────────────────────────────────────────────────────────────────

def test_malformed_output_is_asked_for_once_more_then_given_up():
    model = StubModel([_msg("I think the build might fail."), _msg("Still prose, sorry.")])
    out = _run(model)
    assert out["stopped"] == "malformed" and out["findings"] == [] and out["turns"] == 2
    assert model.seen[1][-1]["content"] == agent.FORMAT_AGAIN


def test_a_retry_that_returns_json_is_used():
    model = StubModel([_msg("prose"), _msg("```json\n" + json.dumps(SOCAT_FINDING) + "\n```")])
    out = _run(model)
    assert out["stopped"] is None and len(out["findings"]) == 1


def test_bad_items_are_dropped_or_downgraded():
    answer = {"findings": [
        "not an object",
        {"step": "build", "title": "no evidence at all", "verdict": "will_fail"},
        {"step": "build", "title": "invented evidence", "verdict": "will_fail",
         "evidence": ["log line: 'kernel panic in module xyzzy at 0xdeadbeef'"], "fix": "x"},
        {"step": "nowhere", "title": "odd verdict", "verdict": "maybe",
         "evidence": [{"source": "recent failure", "quote": "bwrap: execvp socat"}],
         "who": "robot"},
        {"title": ""},
    ]}
    out = _run(StubModel([_msg(json.dumps(answer))]))
    reasons = sorted(d["reason"] for d in out["dropped"])
    assert reasons == ["no evidence", "no title", "not an object"]
    by_title = {f["title"]: f for f in out["findings"]}
    invented = by_title["invented evidence"]
    assert invented["verdict"] == "risk" and invented["grounded"] is False
    odd = by_title["odd verdict"]
    assert odd["verdict"] == "risk" and odd["step"] == "(run)" and odd["named_step"] == "nowhere"
    assert odd["who"] == "person" and odd["grounded"] is True


def test_a_model_that_errors_is_a_result_not_a_crash():
    class Broken(StubModel):
        def create_chat_completion(self, messages, tools=None, **_: Any):
            raise RuntimeError("provider 529 overloaded")
    out = _run(Broken([]))
    assert out["stopped"] == "error" and "529" in out["error"] and out["findings"] == []


# ── caps ──────────────────────────────────────────────────────────────────────────────────────

def _always_a_tool_call(tools: Any) -> dict[str, Any]:
    if tools:
        return _msg(calls=[{"id": "cx", "name": "installed_binaries",
                            "arguments": '{"names": ["socat"]}'}])
    return _msg(json.dumps(SOCAT_FINDING))


def test_the_cost_cap_forces_the_answer_and_never_starts_a_turn_past_it():
    model = StubModel([_always_a_tool_call] * 10, cost=300_000)
    out = _run(model, max_usd_micros=500_000, max_turns=8)
    # Turn 1 spent 0.30; another at that price would cross 0.50, so turn 2 answers, no tools.
    assert out["turns"] == 2 and model.offered[0] and model.offered[1] is None
    assert model.seen[1][-1]["content"] == agent.ANSWER_NOW
    assert out["cost_usd_micros"] == 600_000 and len(out["findings"]) == 1
    assert "$0.60" in out["line"]


def test_the_cost_cap_stops_it_cold():
    model = StubModel([_msg("prose")] * 10, cost=900_000)
    out = _run(model, max_usd_micros=500_000)
    assert out["stopped"] == "cost_cap" and out["turns"] == 1 and out["findings"] == []
    assert "stopped: cost cap" in out["line"]


def test_the_turn_cap_ends_on_an_answer():
    model = StubModel([_always_a_tool_call] * 10)
    out = _run(model, max_turns=3)
    assert out["turns"] == 3 and model.offered[:2] == [model.offered[0]] * 2
    assert model.offered[2] is None and len(out["findings"]) == 1


def test_caps_come_from_the_caller_then_the_settings():
    class S:
        workflow_precheck_agent_max_cents = 25
        workflow_precheck_agent_max_turns = 4
    assert agent.caps_for(None, S(), "p1") == (250_000, 4)
    assert agent.caps_for(None, S(), "p1", max_usd=0.1, max_turns=2) == (100_000, 2)


# ── model-agnostic: the same behaviour whichever engine the router returns ───────────────────

@pytest.mark.parametrize("engine", ["claude_agent_sdk", "claude_code", "codex", "openrouter",
                                    "native"])
def test_behaviour_is_identical_whichever_engine_the_router_returns(monkeypatch, engine):
    seen: dict[str, Any] = {}

    def fake_route(connection, settings, project_id, *, purpose=None):
        seen["purpose"] = purpose
        return StubModel(_socat_script(), engine=engine, source=f"setup-{engine}")

    def fake_governed(adapter, connection, settings, project_id, *, skill_id, ref):
        seen["governed"] = (skill_id, ref)
        return adapter
    monkeypatch.setattr(model_routing, "project_chat_adapter", fake_route)
    monkeypatch.setattr(model_routing, "governed", fake_governed)
    adapter, raw = agent.routed_adapter(object(), object(), "p1", ref="precheck:d1:pre_run")
    out = _run(adapter, max_usd_micros=500_000)
    assert seen["purpose"].kind == "agent" and seen["purpose"].step == "workflow_precheck"
    assert seen["governed"] == ("model.precheck_agent", "precheck:d1:pre_run")
    assert [(f["step"], f["verdict"], f["title"]) for f in out["findings"]] == [
        ("build", "will_fail", SOCAT_FINDING["findings"][0]["title"])]
    assert out["line"].startswith(f"model pre-check ran on {engine}/stub-model via setup-{engine}")


def test_no_vendor_sdk_is_imported():
    source = Path(agent.__file__).read_text()
    assert not re.search(r"^\s*(?:import|from)\s+(?:anthropic|openai|claude_agent_sdk)\b",
                         source, re.M)


def test_a_refused_route_reports_and_never_raises(monkeypatch):
    class NoNested:
        engine = None

        def begin_nested(self):
            raise RuntimeError("no database here")

    def refuse(*a: Any, **k: Any):
        raise RuntimeError("Local means local: no local model is connected")
    monkeypatch.setattr(model_routing, "project_chat_adapter", refuse)
    report = {"workflow_key": "d1", "steps": [], "summary": {"blocking": []}, "explain": []}
    agent.precheck_with_model(None, object(), project_id="p1", report=report,
                              spec={"steps": []}, run_input={}, connection=NoNested(),
                              max_usd=0.5, max_turns=2)
    assert report["model"]["ran"] is False and "Local means local" in report["model"]["line"]
    assert report["summary"]["model"] == {"will_fail": 0, "risk": 0}


# ── tools: fundamentals reads only ────────────────────────────────────────────────────────────

def test_the_agent_holds_only_read_tools():
    from boss_engine.tools import build_discuss_read_registry

    schemas, run = agent.read_tools()
    names = {s["function"]["name"] for s in schemas}
    assert "installed_binaries" in names and {"read_logs", "read_run_turns", "inspect_self",
                                              "get_config", "read_code"} <= names
    registry = build_discuss_read_registry()
    assert all(registry.get(n).kind == "read" for n in names - {"installed_binaries"})
    refused = run(None, "p1", "create_code_task", {"title": "x"})
    assert refused["ok"] is False and "not a read tool" in refused["error"]


def test_installed_binaries_reads_path_and_refuses_odd_names():
    out = agent.installed_binaries(["sh", "definitely-not-a-binary-q9z", "rm -rf /"])
    assert "sh" in out["found"] and out["missing"] == ["definitely-not-a-binary-q9z"]
    assert out["refused"] == ["rm -rf /"]


# ── switches ──────────────────────────────────────────────────────────────────────────────────

def test_the_door_runs_it_only_when_asked():
    class Off:
        workflow_precheck_agent = 0

    class On:
        workflow_precheck_agent = 1
    assert agent.wanted_at_the_door(None, Off(), "p1", {"steps": []}) is False
    assert agent.wanted_at_the_door(None, On(), "p1", {"steps": []}) is True
    assert agent.wanted_at_the_door(None, Off(), "p1", {"precheck_agent": True}) is True
    assert agent.wanted_at_the_door(None, On(), "p1", {"precheck_agent": False}) is False


def test_the_cli_passes_agent_through(monkeypatch):
    from boss_engine.services import workflow_precheck as wp

    seen: dict[str, Any] = {}

    def fake_precheck(engine, settings, **kw):
        seen.update(kw)
        return {"workflow_name": "D1", "checkpoint": "pre_run", "project_id": "p1",
                "steps": [], "fixes": [], "explain": [],
                "summary": {"blocking": [], "sentence": "ok"}}
    import boss_engine.config as config
    import boss_engine.db as db

    monkeypatch.setattr(config, "get_settings", lambda: None)
    monkeypatch.setattr(db, "get_engine", lambda s: None)
    monkeypatch.setattr(wp, "precheck", fake_precheck)
    cli.main(["--workflow", "D1", "--project", "p1"])
    assert seen["agent"] is False
    cli.main(["--workflow", "D1", "--project", "p1", "--agent", "--agent-max-usd", "0.2",
              "--agent-max-turns", "3"])
    assert seen["agent"] is True and seen["agent_max_usd"] == 0.2 and seen["agent_max_turns"] == 3
