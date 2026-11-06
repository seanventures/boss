"""The ambient tick (signals + the Chief of Staff) runs every minute, even mid-build (BOSSAPP-904).

An alert that fires during a 45-minute build must be triaged during it, so the tick lives on the
background thread with the other sweeps that watch work in flight. It only knocks on the API
(which owns the kill switch, the mode and the envelope), keeps its own interval, and a failure
never takes the worker down.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from boss_worker import main as worker  # noqa: E402


class _Client:
    def __init__(self, fail: bool = False):
        self.calls = 0
        self.fail = fail

    def ambient_tick(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("api down")
        return {"mode": "propose", "projects": {"p1": {"counts": {"proposed": 1}}}}


def test_the_ambient_tick_is_on_the_background_thread_not_behind_the_idle_guard():
    assert "_tick_ambient(client)" in inspect.getsource(worker._run_background_sweeps)
    loop = inspect.getsource(worker.main)
    assert "_tick_ambient" not in loop[loop.index("while not _STOPPING:"):]


def test_it_knocks_once_a_minute(monkeypatch):
    client = _Client()
    monkeypatch.setattr(worker, "_last_ambient_tick_monotonic", 0.0)
    clock = iter([1000.0, 1030.0, 1061.0])
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(clock))
    for _ in range(3):
        worker._tick_ambient(client)
    assert client.calls == 2


def test_a_failing_tick_never_takes_the_worker_down(monkeypatch):
    monkeypatch.setattr(worker, "_last_ambient_tick_monotonic", 0.0)
    worker._tick_ambient(_Client(fail=True))


def test_the_client_posts_to_the_ambient_tick_route():
    from boss_worker.control_plane import ControlPlaneClient

    assert '"/v1/ambient/tick"' in inspect.getsource(ControlPlaneClient.ambient_tick)
