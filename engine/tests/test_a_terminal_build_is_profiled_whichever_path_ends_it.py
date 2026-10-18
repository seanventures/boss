"""A build's profile and scorecard are recorded when its status turns terminal, on every path.

The rc10.8 release gate failed "build profile recorded" on crun_16c5e245: the PR path wrote
`completed` at 19:31:42, then went on to the auto-preview, and the profile — emitted only from
the build runner's `finally` (agent_acts.settle_code_task) — was still not written minutes
later, while the gate was already reading a finished run. The record now hangs on the terminal
transition (services/run_terminal.py), and the `finally` is only the backstop.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from sqlalchemy import text

from .test_build_profile import _profiles, _seed

ROOT = Path(__file__).resolve().parents[2]


def _scorecards(eng, run_id):
    with eng.connect() as c:
        return c.execute(text("SELECT terminal_state FROM run_scorecards WHERE unit_id = :r"),
                         {"r": run_id}).scalars().all()


def _pr_path_terminal_write(eng, run_id):
    """What the governed PR path does once the pull request exists: `_fenced_status` writes
    `completed` in its own transaction, then pr_opened and the retrospective are recorded."""
    from boss_engine.services.core import emit_event
    from boss_engine.services.run_fencing import guarded_status

    with eng.begin() as c:
        assert guarded_status(c, run_id, None, "completed",
                              "phase='done', pull_request_url=:u, pull_request_number=:n",
                              {"u": "https://github.com/acme/app/pull/232", "n": 232})
    with eng.begin() as c:
        pid = c.execute(text("SELECT project_id FROM code_task_runs WHERE id = :r"),
                        {"r": run_id}).scalar()
        emit_event(c, pid, "code_task.pr_opened", "boss", "code_task_run", run_id,
                   {"pull_request_url": "https://github.com/acme/app/pull/232"})
        emit_event(c, pid, "code_task.retrospective", "boss", "code_task_run", run_id, {})


def test_a_build_that_completes_through_the_pr_path_gets_exactly_one_build_profile():
    from ._obs_seed import pg_engine

    from boss_engine.services.agent_acts import settle_code_task

    eng = pg_engine()
    run_id = _seed(eng, "running")
    _pr_path_terminal_write(eng, run_id)
    # The runner is still busy after the PR (auto-preview, follow-ups): its `finally` has not
    # run, and the run is already completed. Its profile is there anyway.
    events = _profiles(eng, run_id)
    assert len(events) == 1 and events[0]["status"] == "completed"
    assert events[0]["stages"]["implement"]["output_tokens"] == 1000
    assert _scorecards(eng, run_id) == ["completed"]
    # Then the runner returns, and its `finally` settles again — still exactly one.
    settle_code_task(eng, run_id)
    settle_code_task(eng, run_id)
    assert len(_profiles(eng, run_id)) == 1


def test_a_run_ended_outside_its_runner_is_profiled_too():
    """The shepherd closing a dead build has no runner `finally` behind it at all."""
    from ._obs_seed import pg_engine

    from boss_engine.services.shepherd_service import _fail_run

    eng = pg_engine()
    run_id = _seed(eng, "running")
    with eng.begin() as c:
        assert _fail_run(c, run_id, "nothing can advance it") == 1
    events = _profiles(eng, run_id)
    assert [e["status"] for e in events] == ["failed"]


def test_a_running_build_records_nothing_and_a_rolled_back_end_leaves_no_profile():
    from ._obs_seed import pg_engine

    from boss_engine.services.core import emit_event
    from boss_engine.services.run_fencing import guarded_status

    eng = pg_engine()
    run_id = _seed(eng, "running")
    with eng.begin() as c:
        pid = c.execute(text("SELECT project_id FROM code_task_runs WHERE id = :r"),
                        {"r": run_id}).scalar()
        emit_event(c, pid, "code_task.stage_started", "boss", "code_task_run", run_id, {})
    assert _profiles(eng, run_id) == []

    class Abort(Exception):
        pass

    try:
        with eng.begin() as c:
            guarded_status(c, run_id, None, "failed")
            raise Abort
    except Abort:
        pass
    # The profile was written in the status's own transaction, and went with it.
    assert _profiles(eng, run_id) == []


def test_a_skipped_profile_says_why(caplog):
    from ._obs_seed import pg_engine

    from boss_engine.services.build_profile import emit_build_profile

    eng = pg_engine()
    run_id = _seed(eng, "running")
    with caplog.at_level(logging.INFO, logger="boss_engine.services.build_profile"):
        assert emit_build_profile(eng, run_id) is None
    assert any(run_id in r.getMessage() and "not terminal yet" in r.getMessage()
               for r in caplog.records)


_STATUS_WRITE = re.compile(r"UPDATE code_task_runs SET status\s*=\s*('?)(\w+|:\w+)")


def test_every_status_write_reaches_the_terminal_hook():
    """Every raw write of a build's status is followed by `on_status_written`, or by a
    `code_task.*` event on the run (which runs the same hook from core.emit_event). A new write
    site that does neither fails here instead of silently losing the profile again."""
    missing = []
    for path in sorted((ROOT / "engine" / "boss_engine").rglob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            m = _STATUS_WRITE.search(line)
            if not m or "`" in line[:m.start()]:
                continue                       # prose about SQL, not SQL
            if m.group(1) and m.group(2) in ("running", "queued"):
                continue                       # not an end
            window = "\n".join(lines[i:i + 26])
            if "on_status_written(" in window or re.search(
                    r"emit_event\([^)]*?(\"code_task\.|cf\.OPENED_EVENT)", window, re.S):
                continue
            missing.append(f"{path.relative_to(ROOT)}:{i + 1}")
    assert missing == [], missing


def test_the_state_machine_write_runs_the_hook():
    src = (ROOT / "engine/boss_engine/services/run_fencing.py").read_text(encoding="utf-8")
    body = src[src.index("def guarded_status("):src.index("def template_hold(")]
    assert "on_status_written(connection, run_id)" in body
