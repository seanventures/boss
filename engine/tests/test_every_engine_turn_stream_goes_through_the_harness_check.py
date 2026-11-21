"""Every engine's turn stream goes through the harness check — by construction, not by memory.

An enterprise install (2026-11) ran Codex with its inner sandbox engaged: every command answered
`bwrap: No permissions to create a new namespace`, the test author wrote nothing, and BOSS said
"Test author produced no test files ... likely an ambiguous task". The harness detector existed;
the Codex driver was the one driver whose turns never reached it (the ACP engines, gemini_cli
and opencode, were not wired either). The class fix:

  * the engine contract hands EVERY session a checked turn sink (harness_failures.TurnSink), and
    re-raises what it saw even when a driver swallowed it;
  * every driver hands its turns over through `harness_failures.deliver_turns` (no driver calls
    `on_turn` itself), which checks each turn exactly once;
  * a sandbox-unavailable verdict latches for the run and stops it with the evidence before the
    write_tests stage can turn "could not run" into "no test files" or let the app's existing
    suite stand in for tests nobody could write (#253 keeps working for an author that ran).
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from boss_engine.services import engine_contract as ec
from boss_engine.services import harness_failures as hf

SERVICES = Path(hf.__file__).parent
BWRAP = ("bwrap: No permissions to create a new namespace, likely because the kernel does not "
         "allow non-privileged user namespaces.")
BWRAP_TURN = {"direction": "tool_result", "tool_name": "Bash", "content": BWRAP, "ok": False}


class _SwallowingEngine:
    """A driver that hands its turns to `on_turn` and swallows whatever it raises — the shape
    of every best-effort progress callback before this change."""

    engine = "some_future_engine"

    def __init__(self, turns):
        self.turns = turns
        self.sessions = 0

    def run_role(self, prompt, cwd, *, read_only=False, on_turn=None, **_):
        self.sessions += 1
        for turn in self.turns:
            try:
                if on_turn:
                    on_turn(turn)
            except Exception:  # noqa: BLE001 — exactly what the old drivers did
                pass
        return {"summary": "Test author wrote nothing.", "turns": 1}


def test_a_driver_that_swallows_the_check_still_stops_with_sandbox_unavailable(tmp_path):
    agent = ec.ContractedAgent(_SwallowingEngine([BWRAP_TURN]))
    with pytest.raises(hf.HarnessFailure) as stopped:
        agent.run_role("Write the tests.", tmp_path)
    assert stopped.value.verdict.kind == "sandbox_unavailable"
    assert "No permissions to create a new namespace" in stopped.value.verdict.message
    assert "ENGINE_SANDBOX.md" in stopped.value.verdict.fix


def test_the_verdict_latches_for_the_run_and_no_later_session_starts(tmp_path):
    inner = _SwallowingEngine([BWRAP_TURN])
    agent = ec.ContractedAgent(inner)
    with hf.run_scope("crun_shared_turn_path"):
        with pytest.raises(hf.HarnessFailure):
            agent.run_role("Write the tests.", tmp_path)
        assert hf.tripped("crun_shared_turn_path").kind == "sandbox_unavailable"
        with pytest.raises(hf.HarnessFailure):
            hf.raise_if_tripped()          # every later session of the run refuses at once


def test_the_callers_callback_sees_the_turn_and_each_turn_is_checked_once(tmp_path, monkeypatch):
    checked, seen = [], []
    real = hf.check_turn
    monkeypatch.setattr(hf, "check_turn", lambda t, **kw: (checked.append(t), real(t, **kw)))
    ok_turn = {"direction": "tool_result", "tool_name": "Bash", "content": "fine", "ok": True}

    class Delivering(_SwallowingEngine):
        def run_role(self, prompt, cwd, *, read_only=False, on_turn=None, **_):
            hf.deliver_turns(self.turns, on_turn, engine=self.engine, writer=not read_only)
            return {"summary": "ok"}

    ec.ContractedAgent(Delivering([ok_turn, ok_turn])).run_role("x", tmp_path,
                                                               on_turn=seen.append)
    assert len(checked) == 2 and len(seen) == 2


def test_a_driver_run_without_the_contract_is_still_checked():
    with pytest.raises(hf.HarnessFailure):
        hf.deliver_turns([BWRAP_TURN], None, engine="codex")


def test_a_grep_that_finds_the_words_is_not_a_failure(tmp_path):
    grep = {"direction": "tool_result", "tool_name": "Bash", "ok": True,
            "content": f"engine/x.py:12:{BWRAP}"}
    ec.ContractedAgent(_SwallowingEngine([grep])).run_role("x", tmp_path)


# ── The source contract: no driver calls on_turn itself ──────────────────────────────────────

def _calls_on_turn(tree: ast.AST) -> list[int]:
    return [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "on_turn"]


def test_no_driver_calls_on_turn_itself_every_turn_goes_through_deliver_turns():
    offenders = {}
    for path in sorted(SERVICES.rglob("*.py")):
        if path.name == "harness_failures.py":
            continue
        lines = _calls_on_turn(ast.parse(path.read_text(encoding="utf-8")))
        if lines:
            offenders[str(path.relative_to(SERVICES))] = lines
    assert not offenders, (
        "call harness_failures.deliver_turns(turns, on_turn, engine=…, writer=…) instead of "
        f"on_turn(...): {offenders}")


@pytest.mark.parametrize("driver", ["code_agent.py", "agent_sdk_engine.py",
                                    "codex_code_agent.py", "acp_code_agent.py"])
def test_every_streaming_driver_delivers_through_the_shared_path(driver):
    source = (SERVICES / driver).read_text(encoding="utf-8")
    assert "harness_failures.deliver_turns(" in source
    assert "harness_failures.check_turn(" not in source     # checked once, in the shared path


def test_the_acp_connection_lets_a_harness_failure_end_the_session():
    from boss_engine.services.acp_code_agent import AcpConnection

    conn = AcpConnection.__new__(AcpConnection)

    def handler(message):
        hf.deliver_turns([BWRAP_TURN], None, engine="gemini_cli")

    conn.handler = handler
    with pytest.raises(hf.HarnessFailure):
        conn.serve({"method": "session/update", "params": {}})


def test_write_tests_stops_on_the_harness_before_no_test_files_or_the_existing_suite():
    source = (SERVICES / "governed_code_task_service.py").read_text(encoding="utf-8")
    stop = source.index('_stop_on_harness_failure("write_tests")')
    assert stop < source.index("test_files = existing_suite_stands(_app_tests)")
    assert stop < source.index('reason_code="no_test_files"')


# ── The stream is never a refused login ──────────────────────────────────────────────────────

def test_a_cli_diagnostic_with_a_401_never_refuses_the_login():
    line = json.dumps({"type": "item.completed", "item": {
        "type": "error", "message": "MCP client for `boss` failed to start: 401 Unauthorized"}})
    hf.check_stream(line, engine="codex", writer=True)             # does not raise
    assert hf.classify("MCP client for `boss` failed to start: 401 Unauthorized",
                       engine="codex", where="engine") is None
    assert hf.classify("npm error code E401\nnpm error 401 Unauthorized - GET "
                       "https://registry.example.com/a", engine="codex", where="engine") is None
    # The engine's own refusal still is one.
    assert hf.classify("unexpected status 401 Unauthorized: invalid_refresh_token",
                       engine="codex", where="engine").kind == "auth_refused"


def test_an_mcp_line_is_never_the_providers_transport():
    from boss_engine.services.harness_refusal import is_provider_refusal

    mcp = hf.HarnessVerdict("codex", "auth_refused", "401", "x", "engine",
                            transport_text="ERROR codex_core::mcp: MCP server `kb` codex_api:: "
                                           "returned 401 Unauthorized")
    own = hf.HarnessVerdict("codex", "auth_refused", "401", "x", "engine",
                            transport_text="ERROR codex_api::endpoint: 401 Unauthorized")
    assert not is_provider_refusal(mcp)
    assert is_provider_refusal(own)


@pytest.mark.parametrize("error, code", [
    ("npm error code E401\nnpm error Unable to authenticate", "registry_access"),
    ("ERR_PNPM_FETCH_401 GET https://registry.example.com/x: Unauthorized - 401",
     "registry_access"),
    ("npm error code E407 407 Proxy Authentication Required", "registry_access"),
    ("MCP server kb answered 401 Unauthorized", "connector_login"),
    ("codex: 401 Unauthorized: invalid_refresh_token", "model_login"),
])
def test_someone_elses_401_is_not_the_models_login(error, code):
    from boss_engine.services import failure_owner

    assert failure_owner.classify(error)[1] == code
