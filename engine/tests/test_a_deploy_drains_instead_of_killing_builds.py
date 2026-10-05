"""A deploy drains; it does not kill builds mid-turn (stability spec §4a.5, C6).

One code task on boss-1 went through 3 hand-backs and 4 reclaims in 52 minutes: every deploy
SIGKILLed the API 10 s after SIGTERM and the build restarted from clone.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from boss_engine.services import drain, owned_process  # noqa: E402


@pytest.fixture
def draining():
    owned_process._DRAINING.clear()
    yield
    owned_process._DRAINING.clear()


class _Engine:
    """Records statements; enough for drain() to run without a database."""

    def __init__(self):
        self.sql = []

    def begin(self):
        engine = self

        class _C:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, stmt, params=None):
                engine.sql.append(str(stmt))

                class _R:
                    def mappings(self):
                        return self

                    def first(self):
                        return {"project_id": "p1"}

                    def scalar(self):
                        return 0
                return _R()
        return _C()


def test_draining_ends_every_build_tree_and_refuses_new_ones(draining, monkeypatch):
    monkeypatch.setattr("boss_engine.services.core.emit_event", lambda *a, **k: None)
    with owned_process.owner_scope("code_run", "run_drain"):
        proc = owned_process.popen(["sleep", "60"])
    out = drain.drain(_Engine())
    assert out["interrupted_runs"] == ["run_drain"]
    assert out["trees_ended"] >= 1
    assert proc.poll() is not None
    with owned_process.owner_scope("code_run", "run_drain2"):
        with pytest.raises(owned_process.OwnerCancelled):
            owned_process.popen(["true"])


def test_a_draining_api_leases_nothing(draining):
    from boss_engine.services.jobs import JobLeaseService

    owned_process.begin_drain()
    svc = JobLeaseService.__new__(JobLeaseService)
    assert svc.claim_job(None, "w1", ["code.task"], 60) is None


def test_an_interrupted_build_goes_back_to_the_queue_not_to_failed():
    out = drain.hand_back_interrupted(_Engine(), object(), "run_x", "p1", None)
    assert out["job_deferred"] is True and out["done"] is False
    assert out["status"] == "running"


def test_the_executor_hands_back_rather_than_failing_while_draining():
    import inspect

    from boss_engine.services import governed_code_task_service as g

    src = inspect.getsource(g._run_governed_code_task)
    handler = src[src.index("fail the run gracefully on any agent/model error"):]
    assert handler.index("_owned.draining()") < handler.index("classify_pipeline_exception")
