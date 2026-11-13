"""BOSSAPP-828: without BOSS_RUNNER_URL the dispatch seam is exactly the in-process spawn.

The harness turn used to call `owned_process.run(low_priority(argv), cwd=, input=, capture_output=,
text=, timeout=, start_new_session=, env=)`. Through the seam it must still reach owned_process.run
with those arguments and nothing else — no runner, no extra keyword, no registry write."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from boss_engine.services import cli_harness, owned_process, runner_dispatch


@pytest.fixture
def calls(monkeypatch):
    seen: list[tuple[tuple, dict]] = []

    def fake_run(*args, **kwargs):
        seen.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, "ok", "")

    monkeypatch.setattr(owned_process, "run", fake_run)
    monkeypatch.setattr(runner_dispatch, "HANDOFF_FACTORY",
                        lambda _url: pytest.fail("the runner must not be consulted"))
    return seen


def test_the_seam_is_owned_process_run_when_no_runner_is_configured(monkeypatch, calls):
    monkeypatch.delenv(runner_dispatch.RUNNER_URL_ENV, raising=False)
    out = runner_dispatch.run(["nice", "-n", "19", "codex", "exec", "x"], cwd="/w", input=None,
                              capture_output=True, text=True, timeout=60, start_new_session=True,
                              env={"A": "1"}, session_key="k", collect_files=["/tmp/f"])
    assert out.returncode == 0
    assert calls == [((["nice", "-n", "19", "codex", "exec", "x"],),
                      {"cwd": "/w", "input": None, "capture_output": True, "text": True, "timeout": 60,
                       "start_new_session": True, "env": {"A": "1"}})]


def test_an_empty_runner_url_is_no_runner(monkeypatch, calls):
    monkeypatch.setenv(runner_dispatch.RUNNER_URL_ENV, "  ")
    runner_dispatch.run(["x"], session_key="k")
    assert calls == [((["x"],), {})]


def test_a_call_without_a_turn_key_never_goes_to_the_runner(monkeypatch, calls):
    monkeypatch.setenv(runner_dispatch.RUNNER_URL_ENV, "http://runner.invalid:8095")
    runner_dispatch.run(["x"], timeout=5)
    assert calls == [((["x"],), {"timeout": 5})]


def test_a_harness_turn_reaches_owned_process_run_with_its_old_arguments(tmp_path: Path, monkeypatch, calls):
    monkeypatch.delenv(runner_dispatch.RUNNER_URL_ENV, raising=False)
    spec = cli_harness.BUILTIN_CLI_HARNESSES["grok"]
    monkeypatch.setattr(cli_harness, "detect", lambda *_a: {"ready": True})
    monkeypatch.setattr(cli_harness.shutil, "which", lambda _: "/usr/local/bin/grok")
    monkeypatch.setattr("boss_engine.services.llm_telemetry.record_llm_call", lambda **_kw: None)
    monkeypatch.setattr("boss_engine.services.llm_telemetry.record_agent_turns", lambda *_a, **_kw: None)
    agent = cli_harness.GenericCliCodeAgent(SimpleNamespace(), spec)
    monkeypatch.setattr(agent, "remember_rotated_credential", lambda: False)
    monkeypatch.setattr(agent, "_env", lambda: {"PATH": "/usr/bin"})

    agent.run_role("hello", tmp_path)

    assert len(calls) == 1
    (argv,), kwargs = calls[0]
    assert argv[-1].endswith("hello")
    assert set(kwargs) == {"cwd", "input", "capture_output", "text", "timeout", "start_new_session", "env"}
    assert kwargs["cwd"] == str(tmp_path) and kwargs["capture_output"] is True and kwargs["text"] is True
    assert kwargs["start_new_session"] is True and kwargs["env"] == {"PATH": "/usr/bin"}
