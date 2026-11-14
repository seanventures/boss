"""AP-P0 (BOSSAPP-1051) acceptance (c): the capability tick commits PER STEP.

The run-capabilities endpoint ran `workflow_capability_runner.tick` in ONE transaction per
project (the KNOWN HAZARD, pre-mortem 2026-10-13): a step idle past Postgres's
idle-in-transaction limit got the session ended, and every sibling step of the project rolled
back to `ready` and ran again — repeating its message, its PR, wherever the effect was not
keyed.

Here the limit is real and short: the tick's sessions get `idle_in_transaction_session_timeout`
of 1.5 s, and one of three runs in one project has a step that sits idle for 3 s inside the
tick. Under `tick_per_step` only that step rolls back; the two siblings stay done and their
messages are sent exactly once, across two ticks. The legacy single-transaction `tick` is run on
the same shape to show the hazard this removes (everything rolled back, everything resent).
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import workflow_capability_runner as runner

from .test_workflow_capability_runner import (  # noqa: F401 - fixtures used by name
    TEST_DB,
    Ctx,
    RecordingRegistry,
    _publish,
    _start,
    _steps,
    eng,
    project,
)

IDLE_MS = 1500


class SlowOnceTool:
    """A step's tool (the `cost_check` capability's `get_costs`) that records every call -- its
    side effect, a "message" sent -- and, for the run named "slow", sits idle past the session's
    idle limit the first time it is asked."""

    def __init__(self):
        self.name, self.kind, self.description, self.parameters = "get_costs", "write", "x", {}
        self.sent: list[str] = []
        self.slowed = False

    def handler(self, ctx, *, project_id, actor, args):
        message = str(args.get("since") or "")
        self.sent.append(message)
        if message == "slow" and not self.slowed:
            self.slowed = True
            time.sleep(IDLE_MS / 1000 * 2)
        return {"spend_usd": 0}


@pytest.fixture()
def short_idle_engine():
    tick_engine = create_engine(
        TEST_DB, future=True,
        connect_args={"options": f"-c idle_in_transaction_session_timeout={IDLE_MS}"})
    yield tick_engine
    tick_engine.dispose()


def _three_runs(eng, project):  # noqa: F811
    runs = {}
    with eng.begin() as c:
        for name in ("first", "slow", "third"):
            _, version = _publish(c, project, f"p0_{name}", [
                {"id": "n", "kind": "capability",
                 "binding": {"capability": "cost_check", "inputs": {"since": name}},
                 "next": []},
            ], "n")
            runs[name] = _start(c, project, version["id"], {})["id"]
            # Oldest-touched first: the slow run sits between two siblings.
            c.execute(text("UPDATE workflow_runs SET updated_at = now() - make_interval(secs => :s) "
                           "WHERE id = :r"), {"s": {"first": 30, "slow": 20, "third": 10}[name],
                                              "r": runs[name]})
    return runs


def _status(eng, runs):  # noqa: F811
    with eng.connect() as c:
        return {name: _steps(c, run_id)["n"]["status"] for name, run_id in runs.items()}


def test_an_idle_step_rolls_back_alone_and_nothing_is_sent_twice(
        eng, project, monkeypatch, short_idle_engine):  # noqa: F811
    notify = SlowOnceTool()
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([notify]))
    runs = _three_runs(eng, project)

    first = runner.tick_per_step(short_idle_engine, Ctx(eng), project_id=project)
    after_first = _status(eng, runs)
    assert after_first["first"] == "succeeded" and after_first["third"] == "succeeded", after_first
    assert after_first["slow"] == "ready", after_first          # its claim rolled back with it
    assert [p["outcome"] for p in first["performed"] if p["run_id"] == runs["slow"]] \
        == ["rolled_back"], first

    with eng.begin() as c:   # the retry is due now, not at the wall clock's next minute
        c.execute(text("UPDATE workflow_step_runs SET run_after = now() - interval '1 second' "
                       "WHERE workflow_run_id = :r AND status = 'ready'"), {"r": runs["slow"]})
    runner.tick_per_step(short_idle_engine, Ctx(eng), project_id=project)
    assert set(_status(eng, runs).values()) == {"succeeded"}
    # Each sibling's message went out exactly once; only the step that died was asked again.
    assert notify.sent.count("first") == 1 and notify.sent.count("third") == 1, notify.sent
    assert notify.sent.count("slow") == 2, notify.sent


def test_the_single_transaction_tick_rolled_back_every_sibling(
        eng, project, monkeypatch, short_idle_engine):  # noqa: F811
    """The hazard, measured: the same shape on the legacy `tick` loses the siblings' work."""
    notify = SlowOnceTool()
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([notify]))
    runs = _three_runs(eng, project)
    with pytest.raises(Exception):
        with short_idle_engine.begin() as c:
            runner.tick(c, Ctx(eng), project_id=project)
    assert set(_status(eng, runs).values()) == {"ready"}       # nothing kept
    assert "first" in notify.sent                              # but the message went out


def test_the_endpoint_ticks_per_step():
    from pathlib import Path

    main = (Path(__file__).resolve().parents[2] / "apps/api/app/main.py").read_text()
    start = main.index("def run_delivery_capabilities(")
    body = main[start:main.index("\n@app.", start)]
    assert "workflow_capability_runner.tick_per_step(" in body
    assert "workflow_capability_runner.tick(" not in body
