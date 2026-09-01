"""Sweeps that watch work in flight must not sit behind the job that IS the work.

`run_worker_cycle` blocks for the entire duration of a job — a governed code task is
forty-five minutes of blocking — and every periodic sweep sat below it in the same loop, behind
`if not ran_job`. So the two whose whole purpose is watching work in flight could only run when
there was none.

Measured on wfr_c8578ea1abbf4a1383cc: its `build` step waited 46m47s on a code task that had
already completed, opened its pull request and passed its tests. Driving the same tick by hand
finished the step in 6 seconds. Nothing was wrong with the tick — it was never called.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

# The worker package is not on the path when this suite runs from the repo root, and every
# assertion here is about its source rather than its behaviour.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from boss_worker import main as worker  # noqa: E402


def _loop_body() -> str:
    src = inspect.getsource(worker.main)
    return src[src.index("while not _STOPPING:"):]


def test_the_workflow_tick_runs_on_its_own_thread():
    assert hasattr(worker, "_run_background_sweeps")
    started = inspect.getsource(worker.main)
    assert "threading.Thread(target=_run_background_sweeps" in started
    assert "daemon=True" in started


def test_it_sweeps_the_two_things_that_watch_work_in_flight():
    body = inspect.getsource(worker._run_background_sweeps)
    assert "_advance_delivery_workflows" in body
    assert "_watch_running_tasks" in body


def test_those_sweeps_are_no_longer_behind_the_idle_guard():
    """The bug was the placement, not the code. Leaving a copy on the idle path would also
    double every call for no benefit."""
    idle = _loop_body()
    idle = idle[idle.index("if not ran_job:"):]
    assert "_advance_delivery_workflows(client, settings)" not in idle
    assert "_watch_running_tasks(client)" not in idle


def test_the_sweeper_uses_its_own_client():
    """The main loop is using the other one on another thread, and an HTTP session shared
    across threads is a race nobody wants to debug at 2am."""
    body = inspect.getsource(worker._run_background_sweeps)
    assert "ControlPlaneClient(settings.control_plane_url" in body
    assert "client.close()" in body


def test_a_failing_sweep_never_takes_the_worker_down():
    body = inspect.getsource(worker._run_background_sweeps)
    assert "except Exception" in body


def test_it_stops_when_the_worker_does():
    """A daemon thread that ignores the stop flag holds a deploy open."""
    body = inspect.getsource(worker._run_background_sweeps)
    assert "while not _STOPPING" in body
    assert "if _STOPPING:" in body
