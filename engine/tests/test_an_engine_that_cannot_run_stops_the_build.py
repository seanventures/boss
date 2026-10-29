"""An engine that cannot run stops the build at once, with the harness's verdict.

crun_f103065efc52457fb60c ran on claude_agent_sdk whose Claude CLI sandbox could not start: 19 of
206 turns came back "Sandbox is required but failed to initialize: Sandbox dependencies not
available: socat not installed. Restart to retry." The agent never ran a command, wrote code
blind for fifteen minutes, and the build ended `blocked` on the app's own typecheck. These pin
(engines stubbed, no model call anywhere):

  * ONE classifier tells the harness from the work: the socat message, a sandbox that will not
    start, a refused login or account, a crashed CLI — but never a failing npm test, nor a grep
    that merely prints the words; each engine adds its own words through the registry;
  * each engine stops the session at the FIRST occurrence: the SDK's message stream, the CLI
    stream (`_drain` ends the child), a generic CLI's output (codex);
  * the run's latch: every later session of that run refuses with no spend, and the verdict
    reads "BOSS could not run <engine>: <message>; fix: <fix>." — needs_human, retryable after
    the fix, never `infra` (an automatic retry meets the same engine).
"""
from __future__ import annotations

import contextvars
import dataclasses
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

import pytest

from boss_engine.config import Settings
from boss_engine.services import agent_sdk_engine as sdk
from boss_engine.services import code_agent as ca
from boss_engine.services import harness_failures as hf
from boss_engine.services.agent_turns import StreamParser
from boss_engine.services.harness_registry import FailurePattern

SOCAT = ("Sandbox is required but failed to initialize: Sandbox dependencies not available: "
         "socat not installed. Restart to retry.")
NPM_TEST_FAILED = ("> helpdesk@1.0.0 test\n> vitest run\n\n FAIL  src/app.test.ts > creates a "
                   "ticket\nAssertionError: expected 404 to be 201\n Test Files  1 failed (1)\n"
                   "npm ERR! Test failed.  See above for more details.")
KEY = "sk-ant-api03-" + "k" * 48            # shaped like an API key; not a real one


# ── the classifier ───────────────────────────────────────────────────────────────────────────

def test_the_socat_message_is_the_harness_and_names_the_install():
    said = hf.classify(SOCAT, engine="claude_agent_sdk", where="tool_result", ok=False)
    assert said is not None and said.kind == "missing_runtime"
    assert said.engine == "claude_agent_sdk"
    assert "socat not installed" in said.message and "Restart to retry" not in said.message
    # No engine declares socat any more (the SDK engine no longer starts the CLI's sandbox:
    # model_proxy), so the fix names the binary without a package hint.
    assert "install socat where BOSS runs claude_agent_sdk" in said.fix
    assert said.sentence().startswith("BOSS could not run claude_agent_sdk: Sandbox is required")
    assert "; fix: install socat" in said.sentence()


def test_an_ordinary_failing_command_is_the_agents_work_not_the_harness():
    for engine in ("claude_agent_sdk", "claude_code", "codex"):
        for where in ("tool_result", "stream", "engine"):
            assert hf.classify(NPM_TEST_FAILED, engine=engine, where=where, ok=False) is None
        assert hf.classify("error TS7016: Could not find a declaration file for module 'pg'.",
                           engine=engine, where="tool_result", ok=False) is None


def test_a_command_that_merely_prints_the_words_is_not_the_harness():
    grep = f"engine/boss_engine/services/engine_startup.py:8:    {SOCAT}"
    assert hf.classify(grep, engine="claude_code", where="tool_result", ok=True) is None
    # A successful result whose FIRST line is the harness's own sentence still is one.
    assert hf.classify(SOCAT, engine="claude_code", where="tool_result", ok=True) is not None
    # Login / account words are only believed from the engine itself, never a tool's output.
    assert hf.classify("401 invalid x-api-key", engine="claude_code", where="tool_result",
                       ok=False) is None
    assert hf.classify("Credit balance is too low", engine="claude_code", where="tool_result",
                       ok=False) is None


@pytest.mark.parametrize("engine, text, kind", [
    ("claude_code", "claude code agent exited 1: Not logged in · Please run /login",
     "auth_refused"),
    ("claude_agent_sdk", "claude agent sdk session failed: Credit balance is too low",
     "credit_refused"),
    ("codex", "codex exited -11: Segmentation fault (core dumped)", "cli_crashed"),
    ("claude_code", "claude CLI not found. Install Claude Code and ensure `claude` is on PATH.",
     "missing_runtime"),
])
def test_the_engines_own_final_error_is_classified(engine, text, kind):
    said = hf.classify(text, engine=engine, where="engine")
    assert said is not None and said.kind == kind


def test_each_engine_contributes_its_own_words_through_the_registry():
    codex = "Error: Landlock ruleset is not supported by this kernel"
    # codex's own words (cli_harness.CODEX_FAILURE_PATTERNS) — not a claude engine's.
    assert hf.classify(codex, engine="codex", where="stream").kind == "sandbox_unavailable"
    assert hf.classify(codex, engine="claude_code", where="stream") is None
    own = "Sandbox is required but failed to initialize: bwrap: setting up uid map denied"
    for engine in ("claude_code", "claude_agent_sdk"):
        said = hf.classify(own, engine=engine, where="tool_result", ok=False)
        assert said is not None and said.kind == "sandbox_unavailable"
        assert "bubblewrap and socat" in said.fix
    # The generic words hold for every engine, registered or not.
    generic = "error: failed to initialize sandbox: namespaces unavailable"
    for engine in ("codex", "claude_code", "grok", "some_new_cli"):
        assert hf.classify(generic, engine=engine, where="stream").kind == "sandbox_unavailable"


def test_a_failure_pattern_names_a_known_kind():
    with pytest.raises(ValueError):
        FailurePattern("tests_failed", r"npm err", "fix the tests")


# ── the verdict, read back where it is shown ─────────────────────────────────────────────────

def test_the_verdict_is_needs_human_retryable_and_settled_not_infra():
    from boss_engine.services.code_task_service import classify_pipeline_exception
    from boss_engine.services.model_failures import explain, settled_failure

    sentence = hf.classify(SOCAT, engine="claude_agent_sdk", where="tool_result",
                           ok=False).sentence()
    reason = f"{sentence} [substrate=local]"
    # "sandbox" alone used to read as infra -> an automatic retry onto the same broken engine.
    assert classify_pipeline_exception(reason) == ("needs_human", "harness_unavailable")
    said = explain(reason)
    assert said["kind"] == "harness_failure" and said["retry"] is True
    assert said["headline"].startswith("BOSS could not run claude_agent_sdk:")
    assert "install socat" in said["action"].lower()
    assert settled_failure(reason) == reason          # the sentence is not wrapped twice


# ── the run's latch ──────────────────────────────────────────────────────────────────────────

def test_a_failure_in_one_session_refuses_every_later_session_of_the_run():
    with hf.run_scope("crun_test_latch"):
        hf.raise_if_tripped()                           # nothing yet
        seat_ctx = contextvars.copy_context()           # a review seat's thread

        def seat():
            with pytest.raises(hf.HarnessFailure):
                hf.check_turn({"direction": "tool_result", "content": SOCAT, "ok": False},
                              engine="claude_agent_sdk")

        t = threading.Thread(target=seat_ctx.run, args=(seat,))
        t.start()
        t.join()
        assert hf.tripped("crun_test_latch").kind == "missing_runtime"
        with pytest.raises(hf.HarnessFailure) as again:
            hf.raise_if_tripped()
        assert "socat" in str(again.value)
        # The pipeline's handler finds it even through an exception that swallowed it.
        assert hf.failure_of(RuntimeError("tsc failed"), "crun_test_latch") is not None
    # A new execution of the run starts clear; outside a run nothing is remembered.
    with hf.run_scope("crun_test_latch"):
        hf.raise_if_tripped()
    with pytest.raises(hf.HarnessFailure):
        hf.check_turn({"direction": "tool_result", "content": SOCAT, "ok": False},
                      engine="claude_code")
    assert hf.tripped("crun_test_latch") is None
    hf.raise_if_tripped()


def test_an_engines_final_error_trips_the_run_but_is_raised_unchanged():
    with hf.run_scope("crun_test_final"):
        said = hf.note_engine_error("claude agent sdk session failed: Credit balance is too low",
                                    engine="claude_agent_sdk")
        assert said.kind == "credit_refused"
        assert hf.tripped("crun_test_final") is said
        assert hf.note_engine_error(NPM_TEST_FAILED, engine="claude_code") is None
    hf.reset("crun_test_final")


# ── the SDK engine: its message stream ───────────────────────────────────────────────────────

@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: Any = None
    is_error: Optional[bool] = None


@dataclass
class TextBlock:
    text: str


@dataclass
class AssistantMessage:
    content: list
    model: str = "claude-sonnet-5"
    usage: Optional[dict] = None
    session_id: Optional[str] = None


@dataclass
class UserMessage:
    content: Any


@dataclass
class ResultMessage:
    subtype: str = "success"
    duration_ms: int = 10
    duration_api_ms: int = 9
    is_error: bool = False
    num_turns: int = 9
    session_id: str = "sess_1"
    total_cost_usd: Optional[float] = 0.5
    usage: Optional[dict] = None
    result: Optional[str] = "Implemented the app."
    model_usage: Optional[dict] = None
    api_error_status: Optional[int] = None
    errors: Optional[list] = None
    stop_reason: Optional[str] = None


def _blind_session(first_result: str, *, is_error: bool = True) -> list:
    """What crun_f103065e's sessions looked like: every command answered by the harness, and
    the agent carrying on writing code it could not run."""
    out = []
    for i in range(6):
        out += [AssistantMessage([ToolUseBlock(f"tu_{i}", "Bash", {"command": "npm install"})]),
                UserMessage([ToolResultBlock(f"tu_{i}", first_result if i == 0 else SOCAT,
                                             is_error)]),
                AssistantMessage([TextBlock("The sandbox is unavailable; writing it anyway."),
                                  ToolUseBlock(f"tw_{i}", "Write", {"file_path": f"f{i}.ts"})]),
                UserMessage([ToolResultBlock(f"tw_{i}", "ok")])]
    return out + [ResultMessage()]


@pytest.fixture()
def sdk_stub(monkeypatch):
    """The SDK and its owned transport stubbed: messages replayed, consumption counted."""
    seen = {"messages": 0, "sessions": 0, "script": []}

    def query(*, prompt, options, transport=None):
        seen["sessions"] += 1

        async def gen():
            for m in seen["script"]:
                seen["messages"] += 1
                yield m
        return gen()

    monkeypatch.setattr(sdk, "_query", query)
    monkeypatch.setattr(sdk, "sdk_available", lambda: True)
    monkeypatch.setattr(sdk, "make_owned_transport", lambda *a, **k: None)
    turns: list = []
    calls: list = []
    monkeypatch.setattr(ca, "record_agent_turns",
                        lambda rows, **kw: turns.extend(rows) or len(rows))
    monkeypatch.setattr(ca, "record_llm_call", lambda **kw: calls.append(kw))
    monkeypatch.setattr("boss_engine.services.cli_harness.stored_credential",
                        lambda settings, name, **kw: KEY if name == "ANTHROPIC_API_KEY" else None)
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "BOSS_CODE_AGENT_ENGINE"):
        monkeypatch.delenv(name, raising=False)
    seen["turns"], seen["calls"] = turns, calls
    return seen


def _sdk_agent(monkeypatch):
    agent = sdk.agent_class()(Settings().model_copy(update={"claude_code_oauth_token": "",
                                                            "anthropic_api_key": None}))
    monkeypatch.setattr(agent, "_options", lambda **kw: object())
    return agent


def test_the_sdk_session_stops_at_the_first_socat_result(monkeypatch, tmp_path, sdk_stub):
    sdk_stub["script"] = _blind_session(SOCAT)
    agent = _sdk_agent(monkeypatch)
    with hf.run_scope("crun_test_sdk"):
        with pytest.raises(hf.HarnessFailure) as stopped:
            agent.run_role("Implement the HelpDesk app.", tmp_path)
        verdict = stopped.value.verdict
        assert verdict.engine == "claude_agent_sdk" and verdict.kind == "missing_runtime"
        assert "install socat" in verdict.fix
        # Stopped at the first occurrence: the tool_use and its result, nothing after.
        assert sdk_stub["messages"] == 2
        assert [t["direction"] for t in sdk_stub["turns"]] == ["tool_call", "tool_result"]
        # The next session of the run — a repair, a review seat — never starts.
        with pytest.raises(hf.HarnessFailure):
            agent.run_role("Repair the failing typecheck.", tmp_path)
        assert sdk_stub["sessions"] == 1
    hf.reset("crun_test_sdk")


def test_an_sdk_session_whose_npm_test_fails_runs_to_its_end(monkeypatch, tmp_path, sdk_stub):
    sdk_stub["script"] = _blind_session(NPM_TEST_FAILED)[:4] + [ResultMessage()]
    agent = _sdk_agent(monkeypatch)
    with hf.run_scope("crun_test_sdk_ok"):
        out = agent.run_role("Implement the HelpDesk app.", tmp_path)
        assert out["summary"] == "Implemented the app."
        assert sdk_stub["messages"] == 5
        assert hf.tripped("crun_test_sdk_ok") is None


# ── the claude_code engine: its live CLI stream ──────────────────────────────────────────────

def test_the_cli_stream_ends_the_child_at_the_harness_line():
    """`_drain` (claude_code's reader): a consumer raising HarnessFailure ends the child there,
    not when the agent finally gives up."""
    script = ("import sys, time\n"
              "print('{\"type\":\"system\"}', flush=True)\n"
              "print('SANDBOX', flush=True)\n"
              "time.sleep(30)\n"
              "print('never', flush=True)\n")
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    seen: list[str] = []

    def on_line(line: str) -> None:
        seen.append(line.strip())
        if line.strip() == "SANDBOX":
            hf.check_turn({"direction": "tool_result", "content": SOCAT, "ok": False},
                          engine="claude_code")

    started = time.monotonic()
    with pytest.raises(hf.HarnessFailure) as stopped:
        ca._drain(child, timeout=60, on_line=on_line)
    assert time.monotonic() - started < 15
    assert child.poll() is not None and "never" not in seen
    assert stopped.value.verdict.engine == "claude_code"


def test_the_claude_code_stream_parser_hands_the_socat_result_to_the_check():
    parser = StreamParser()
    parser.feed('{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1",'
                '"name":"Bash","input":{"command":"npm ci"}}]}}')
    produced = parser.feed('{"type":"user","message":{"content":[{"type":"tool_result",'
                           '"tool_use_id":"t1","is_error":true,"content":"' + SOCAT + '"}]}}')
    with pytest.raises(hf.HarnessFailure):
        for turn in produced:
            hf.check_turn(turn, engine="claude_code")


# ── a generic CLI engine (codex): its output ─────────────────────────────────────────────────

def test_a_codex_session_whose_sandbox_failed_stops_the_stage(monkeypatch, tmp_path):
    from boss_engine.services import cli_harness, llm_telemetry

    spec = dataclasses.replace(cli_harness.BUILTIN_CLI_HARNESSES["codex"], auth_file_secret="",
                               token_env="", final_message_flag="")
    agent = cli_harness.GenericCliCodeAgent(Settings(), spec)
    monkeypatch.setattr(agent, "_resolve_binary", lambda: "codex")
    monkeypatch.setattr(llm_telemetry, "record_llm_call", lambda **kw: None)
    out = {"stdout": "exec bash -lc 'npm test'\nError: Landlock ruleset is not supported\n",
           "stderr": "", "rc": 1}
    runs: list = []

    def run(argv, **kw):
        runs.append(argv)
        return subprocess.CompletedProcess(argv, out["rc"], out["stdout"], out["stderr"])

    monkeypatch.setattr(cli_harness.owned_process, "run", run)
    with hf.run_scope("crun_test_codex"):
        with pytest.raises(hf.HarnessFailure) as stopped:
            agent.run_role("Implement it.", tmp_path)
        assert stopped.value.verdict.engine == "codex"
        assert stopped.value.verdict.kind == "sandbox_unavailable"
        with pytest.raises(hf.HarnessFailure):
            agent.run_role("Repair it.", tmp_path)
        assert len(runs) == 1
    hf.reset("crun_test_codex")
    # Its ordinary failing test is the agent's work: returned, not stopped.
    out.update(stdout=NPM_TEST_FAILED, rc=0)
    with hf.run_scope("crun_test_codex_ok"):
        assert agent.run_role("Implement it.", tmp_path)["exit_code"] == 0


# ── the model's own words are not the harness's (crun_1be7a5f3, 2026-10-29) ──────────────────

def test_a_model_writing_about_sandboxes_does_not_stop_its_build():
    """codex reviewed sandbox code and its review said "failed to start sandbox"; BOSS read the
    review as codex's sandbox failing and ended a healthy build. Event lines count only for
    their errors."""
    import json

    said = "REVIEWED: sandbox_identity.py. If bwrap is absent it failed to start sandbox; ok."
    stream = "\n".join(json.dumps(e) for e in (
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "item.completed", "item": {"id": "i1", "type": "reasoning", "text": said}},
        {"type": "item.completed", "item": {"id": "i2", "type": "command_execution",
                                            "aggregated_output": said, "exit_code": 0}},
        {"type": "item.completed", "item": {"id": "i3", "type": "agent_message", "text": said}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": said}]}},
        {"type": "result", "is_error": False, "result": said},
        {"type": "turn.completed", "usage": {}},
    ))
    for engine in ("codex", "claude_code", "some_new_cli"):
        assert hf.classify(stream, engine=engine, where="stream") is None


@pytest.mark.parametrize("event", [
    {"type": "error", "message": "error: failed to initialize sandbox: namespaces unavailable"},
    {"type": "turn.failed", "error": {"message": "failed to initialize sandbox: no landlock"}},
    {"type": "item.completed", "item": {"type": "error",
                                        "message": "failed to start sandbox: bwrap denied"}},
    {"type": "result", "is_error": True, "result": "failed to initialize sandbox"},
])
def test_the_harness_own_error_events_still_stop_the_build(event):
    import json

    stream = json.dumps({"type": "item.completed",
                         "item": {"type": "agent_message", "text": "working"}})
    stream += "\n" + json.dumps(event)
    said = hf.classify(stream, engine="codex", where="stream")
    assert said is not None and said.kind == "sandbox_unavailable"
