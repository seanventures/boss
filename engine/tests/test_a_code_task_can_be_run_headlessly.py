"""BOSS must be runnable from a pipeline, not only from a chat window.

Factory ships `droid exec -o json|stream-json` with real exit codes, and that is how their
agent reaches CI. BOSS had `call create_code_task` plus hand-rolled polling, which is not an
interface — it is a request to write one. (Warp is weaker here than Factory: their CLI is a
TUI with no structured output at all, so matching Factory beats Warp outright.)

`boss-engine code` is governed by construction: it goes through create_code_task, so the
sandbox, gates, receipts and budget reservation all apply. There is deliberately no
ungoverned fast path — an ungoverned BOSS is just a worse coding agent.
"""

from __future__ import annotations

import json

import pytest

from boss_engine.__main__ import build_parser, _cmd_code


class _FakeRegistry:
    """Replays a scripted sequence of get_code_task responses."""

    def __init__(self, phases: list[dict], start: dict | None = None) -> None:
        self.start = start or {"ok": True, "run_id": "crun_test"}
        self.phases = list(phases)
        self.calls: list[tuple[str, dict]] = []

    def invoke(self, name, ctx, *, project_id, actor, args):
        self.calls.append((name, args))
        if name == "create_code_task":
            return self.start
        return {"run": self.phases.pop(0) if self.phases else self.phases_last()}

    def phases_last(self):
        return {"phase": "done", "status": "done"}


@pytest.fixture()
def wire(monkeypatch):
    def _wire(registry):
        import boss_engine.__main__ as main_mod
        import boss_engine.tools as tools_mod

        monkeypatch.setattr(tools_mod, "build_default_registry", lambda: registry)
        monkeypatch.setattr(main_mod, "_ctx", lambda: object())
        return registry
    return _wire


def _args(instructions="add a flag", **kw):
    argv = ["code", instructions]
    for k, v in kw.items():
        flag = "--" + k.replace("_", "-")
        if v is True:
            argv.append(flag)
        elif v is False or v is None:
            continue
        elif isinstance(v, list):
            for item in v:
                argv += [flag, str(item)]
        else:
            # `v not in (None, False)` would drop 0, because 0 == False in Python — which
            # silently left --poll-interval at its 5s default and made these tests sleep.
            argv += [flag, str(v)]
    return build_parser().parse_args(argv)


def test_a_finished_run_exits_zero(wire, capsys):
    wire(_FakeRegistry([{"phase": "implement", "status": "running"},
                        {"phase": "done", "status": "done"}]))
    assert _cmd_code(_args(poll_interval=0)) == 0


@pytest.mark.parametrize("status", ["failed", "blocked", "cancelled"])
def test_a_run_that_did_not_ship_exits_nonzero(wire, status):
    """A pipeline step that cannot fail is not a gate."""
    wire(_FakeRegistry([{"phase": "final", "status": status}]))
    assert _cmd_code(_args(poll_interval=0)) == 1


def test_completed_no_pr_is_a_success(wire):
    """The gate ran and the work landed without needing a PR. Not a failure."""
    wire(_FakeRegistry([{"phase": "final", "status": "completed_no_pr"}]))
    assert _cmd_code(_args(poll_interval=0)) == 0


def test_a_task_that_will_not_start_exits_one(wire, capsys):
    reg = _FakeRegistry([], start={"ok": False, "error": "no repository bound"})
    wire(reg)
    assert _cmd_code(_args(poll_interval=0)) == 1
    assert "no repository bound" in capsys.readouterr().err


def test_stream_json_emits_one_object_per_line_as_it_goes(wire, capsys):
    wire(_FakeRegistry([{"phase": "spec", "status": "running"},
                        {"phase": "implement", "status": "running"},
                        {"phase": "done", "status": "done"}]))
    assert _cmd_code(_args(output="stream-json", poll_interval=0)) == 0
    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    events = [e["event"] for e in lines]
    assert events[0] == "started"
    assert events[-1] == "finished"
    assert "phase" in events, "a consumer must see progress before the run ends"
    assert [e["phase"] for e in lines if e["event"] == "phase"] == [
        "spec", "implement", "done"]


def test_a_phase_is_reported_once_not_on_every_poll(wire, capsys):
    """Polling is an implementation detail. Repeating the same phase every 5s would make
    the stream unreadable and tell a consumer nothing new."""
    wire(_FakeRegistry([{"phase": "implement", "status": "running"}] * 4
                       + [{"phase": "done", "status": "done"}]))
    _cmd_code(_args(output="stream-json", poll_interval=0))
    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert [e["phase"] for e in lines if e["event"] == "phase"] == ["implement", "done"]


def test_json_output_is_a_single_parseable_object(wire, capsys):
    wire(_FakeRegistry([{"phase": "done", "status": "done"}]))
    assert _cmd_code(_args(output="json", poll_interval=0)) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["status"] == "done" and parsed["run_id"] == "crun_test"


def test_no_wait_starts_the_task_and_returns(wire):
    reg = _FakeRegistry([])
    wire(reg)
    assert _cmd_code(_args(no_wait=True, poll_interval=0)) == 0
    assert [n for n, _ in reg.calls] == ["create_code_task"], "must not poll"


def test_a_timeout_is_a_failure_and_says_the_run_continues(wire, capsys):
    wire(_FakeRegistry([{"phase": "implement", "status": "running"}] * 50))
    assert _cmd_code(_args(timeout=0, poll_interval=0)) == 1
    assert "still going" in capsys.readouterr().err


def test_the_flags_reach_create_code_task(wire):
    reg = _FakeRegistry([{"phase": "done", "status": "done"}])
    wire(reg)
    _cmd_code(_args("make it fast", title="Speed", repo="acme/app", profile="strict",
                    base_branch="dev", criteria=["it is fast"],
                    test_command=["pytest -q"], poll_interval=0))
    _, payload = reg.calls[0]
    assert payload["title"] == "Speed"
    assert payload["repository"] == "acme/app"
    assert payload["profile"] == "strict"
    assert payload["base_branch"] == "dev"
    assert payload["acceptance_criteria"] == ["it is fast"]
    assert payload["test_commands"] == ["pytest -q"]


def test_an_omitted_flag_is_not_sent_as_an_empty_string(wire):
    """An empty `repository` would override the project's primary repo with nothing."""
    reg = _FakeRegistry([{"phase": "done", "status": "done"}])
    wire(reg)
    _cmd_code(_args(poll_interval=0))
    _, payload = reg.calls[0]
    for absent in ("repository", "profile", "base_branch", "acceptance_criteria",
                   "test_commands"):
        assert absent not in payload


def test_the_title_defaults_to_the_instructions(wire):
    reg = _FakeRegistry([{"phase": "done", "status": "done"}])
    wire(reg)
    _cmd_code(_args("x" * 200, poll_interval=0))
    _, payload = reg.calls[0]
    assert payload["title"] == "x" * 80
