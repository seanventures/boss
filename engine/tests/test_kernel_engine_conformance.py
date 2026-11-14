"""THE KERNEL CONFORMANCE SUITE, AGAINST EVERY REGISTERED ENGINE (AP-P1b acceptance (a)).

test_kernel_conformance.py holds each OLD loop equal to its kernel preset on the scripted
engine. This file holds the KERNEL equal to itself across engines: the same neutral script is
played through every engine in `engine.REGISTERED_ENGINES` — the scripted conformance engine,
OpenAIChatEngine (every OpenAI-compatible lane, and the CLI brains' adapters on the agent-step
path), AnthropicMessagesEngine (over a faked Messages API, the wire shape asserted by
test_anthropic_engine.py) and TextEngine (meeting seats; tool-less) — and every engine must
produce the same run: status, answer, tool trace, steps, and the governance around it (each
attempt receipted and reserved, a held call suspended and resumed once, a cancel honoured).

A new Engine class in the kernel package that is not registered fails here, and a registered
one without a driver below fails too, so no engine reaches a surface without the suite.
"""
from __future__ import annotations

import importlib
import inspect
import json
import pkgutil

import pytest

from boss_engine import kernel
from boss_engine.kernel import presets
from boss_engine.kernel.checkpoint import MemoryStore
from boss_engine.kernel.engine import REGISTERED_ENGINES
from boss_engine.kernel.scripted import Call, Fail, Say, ScriptedEngine
from boss_engine.kernel.toolset import FunctionToolSet, Tool
from boss_engine.kernel.types import ToolSchema

READ = {"type": "object", "properties": {"run": {"type": "string"}}, "required": ["run"],
        "additionalProperties": False}
TRANSIENT = "transient"


def read_logs(args):
    return {"ok": True, "lines": [f"run {args['run']}: 3 errors"]}


# ── One driver per registered engine: a neutral script in, that engine out ────────────────────

def scripted(script):
    return ScriptedEngine([Fail(RuntimeError("529 overloaded_error")) if i == TRANSIENT else i
                           for i in script])


def openai_chat(script):
    # The adapter face of the scripted engine is exactly an OpenAI-compatible adapter's.
    return kernel.OpenAIChatEngine(scripted(script), name="openai_chat")


def anthropic_messages(script):
    from tests.test_anthropic_engine import FakeMessagesAPI, error, say, tool_use

    items = []
    for n, item in enumerate(script):
        if item == TRANSIENT:
            items.append(error(529, "overloaded_error", "Overloaded"))
        elif isinstance(item, Say):
            items.append(say(item.text))
        else:
            items.append(tool_use(item.name, item.args, call_id=f"toolu_{n}"))
    return FakeMessagesAPI(items).engine()


class _TextAdapter:
    def __init__(self, script):
        self.script = list(script)

    def create_text_response(self, prompt, system_prompt=None):
        item = self.script.pop(0)
        if item == TRANSIENT:
            raise RuntimeError("529 overloaded_error")
        if not isinstance(item, Say):
            raise AssertionError("a text engine was scripted a tool call")
        return "ref", item.text, {"model": "text-test", "prompt_tokens": 10,
                                  "completion_tokens": 5}


def text(script):
    return kernel.TextEngine(_TextAdapter(script))


DRIVERS = {"scripted": scripted, "openai_chat": openai_chat,
           "anthropic_messages": anthropic_messages, "text": text}
TOOLED = [name for name in REGISTERED_ENGINES if name != "text"]


def test_every_engine_class_in_the_kernel_is_registered_and_driven():
    """An Engine is a class with `complete(request)` and `traits`; GovernedEngine is a wrapper
    around one (the budget middleware applies the same gate), not an engine of its own."""
    import boss_engine.kernel as package

    found = set()
    for info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"boss_engine.kernel.{info.name}")
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if obj.__module__ == module.__name__ and name.endswith("Engine") \
                    and callable(getattr(obj, "complete", None)):
                found.add(f"{obj.__module__}:{name}")
    found.discard("boss_engine.kernel.governance:GovernedEngine")
    found.discard("boss_engine.kernel.engine:Engine")  # the Protocol itself
    assert found == set(REGISTERED_ENGINES.values()), found ^ set(REGISTERED_ENGINES.values())
    assert set(DRIVERS) == set(REGISTERED_ENGINES)


def run(engine, policies, *, tools=None, **kw):
    if tools is None:
        # A tool-less engine's run offers no tools (a meeting seat's never does).
        tools = FunctionToolSet([] if isinstance(engine, kernel.TextEngine) else [
            Tool(ToolSchema("read_logs", "Read a run's logs.", READ), read_logs)])
    return kernel.run(kernel.RunSpec(
        engine=engine, tools=tools,
        policies=policies, project_id="p", system="You are the test agent.",
        messages=[{"role": "user", "content": "Check the run."}], sleep=lambda s: None, **kw))


def shape(result):
    return {"status": result.status, "text": result.text, "steps": result.steps,
            "exhausted": result.exhausted, "stopped": result.stopped,
            "trace": [(t["tool"], json.dumps(t["args"], sort_keys=True), t["status"])
                      for t in result.trace]}


def same_on_every_engine(engines, script, policies_factory, **kw):
    shapes = {name: shape(run(DRIVERS[name](list(script)), policies_factory(), **kw))
              for name in engines}
    first = next(iter(shapes.values()))
    for name, got in shapes.items():
        assert got == first, (name, got, first)
    return first


# ════ The scenarios ═══════════════════════════════════════════════════════════════════════════

def test_a_plain_answer_on_every_engine():
    got = same_on_every_engine(REGISTERED_ENGINES, [Say("All clear.")],
                               lambda: presets.engine_agent(4))
    assert got["status"] == "completed" and got["text"] == "All clear."


def test_a_tool_call_then_the_answer_on_every_tool_engine():
    got = same_on_every_engine(TOOLED, [Call("read_logs", {"run": "r1"}), Say("r1 is red.")],
                               lambda: presets.agent_loop(4))
    assert got["trace"] == [("read_logs", '{"run": "r1"}', "ok")] and got["steps"] == 2


def test_the_step_cap_answers_without_tools_on_every_tool_engine():
    got = same_on_every_engine(TOOLED, [Call("read_logs", {"run": "r1"}),
                                        Call("read_logs", {"run": "r2"}), Say("Both red.")],
                               lambda: presets.agent_loop(2))
    assert got["exhausted"] == "steps" and got["text"] == "Both red."


def test_a_malformed_call_is_refused_and_repaired_on_every_tool_engine():
    got = same_on_every_engine(TOOLED, [Call("read_logs", {}), Call("read_logs", {"run": "r"}),
                                        Say("done")], lambda: presets.engine_agent(6))
    assert [t[2] for t in got["trace"]] == ["refused", "ok"]


def test_a_transient_error_is_retried_on_every_engine():
    got = same_on_every_engine(REGISTERED_ENGINES, [TRANSIENT, Say("recovered")],
                               lambda: presets.engine_agent(4))
    assert got["text"] == "recovered"


@pytest.mark.parametrize("name", TOOLED)
def test_a_held_call_suspends_and_resumes_once_on_every_tool_engine(name):
    from boss_engine.services.argument_binding import bind_held_call

    ran: list = []
    store = MemoryStore()
    held = FunctionToolSet([Tool(
        ToolSchema("publish", "Publish.", {"type": "object", "properties": {}}, kind="write"),
        lambda a: {"status": "deferred_for_approval", "decision_id": "dec_1"},
        run_approved=lambda a: ran.append(a) or {"ok": True})])
    first = run(DRIVERS[name]([Call("publish", {}), Say("never")]), presets.agent_loop(4),
                tools=held, store=store, run_id=f"krun_{name}")
    assert first.status == "suspended"
    resumed = kernel.resume(kernel.RunSpec(
        engine=DRIVERS[name]([Say("Published.")]), tools=held, policies=presets.agent_loop(4),
        project_id="p", run_id=f"krun_{name}", store=store, sleep=lambda s: None),
        decision={"approved": True, "decision_id": "dec_1", **bind_held_call("publish", {})})
    assert resumed.status == "completed" and resumed.text == "Published." and ran == [{}]


@pytest.mark.parametrize("name", list(REGISTERED_ENGINES))
def test_the_governance_is_the_same_on_every_engine(name):
    log: list = []

    class Gate:
        def open(self):
            log.append("open")

        def settle(self, held, usage):
            log.append("settle")

        def fail(self, held, error):
            log.append("fail")

    result = run(DRIVERS[name]([TRANSIENT, Say("ok")]), presets.engine_agent(4), gate=Gate())
    kinds = [e.kind for e in result.events]
    assert log == ["open", "fail", "open", "settle"]
    assert kinds.count("model.request") == 2 and kinds.count("model.response") == 1
    started = next(e for e in result.events if e.kind == "run.started")
    assert started.data["middleware"][-5:] == ["telemetry", "budget", "authz", "tool_screen",
                                               "audit"]


@pytest.mark.parametrize("name", list(REGISTERED_ENGINES))
def test_a_cancelled_run_never_calls_the_engine(name):
    token = kernel.CancelToken()
    token.cancel(by="test")
    result = run(DRIVERS[name]([Say("never")]), presets.agent_loop(4), cancel=token)
    assert result.status == "cancelled" and result.steps == 0


def test_the_text_engine_refuses_a_request_that_offers_tools():
    result = run(text([Say("x")]), presets.agent_loop(2), tools=FunctionToolSet(
        [Tool(ToolSchema("read_logs", "Read a run's logs.", READ), read_logs)]))
    assert result.status == "failed" and "tool-less" in result.error
