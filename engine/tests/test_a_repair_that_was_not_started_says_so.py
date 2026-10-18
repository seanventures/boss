"""A repair build that was not started is never recorded as the fix (D2, crun_cfb9f2e2).

The deploy agent diagnosed a manifest missing `version: 1` and chose fix_app. The repair's
retry returned the app's More tests attempt already running on the branch (`created: False`)
without the note, and that run was recorded as the fix. The deploy then waited on a build that
never touched the manifest."""
from types import SimpleNamespace

from boss_engine.services import code_task_service as cts
from boss_engine.services import workflow_capability_runner as runner


class _Conn:
    def execute(self, *a, **k):
        return SimpleNamespace(scalar=lambda: "usr_op")


def _start(monkeypatch, created):
    monkeypatch.setattr(runner.workflows, "_run_spec", lambda c, r: {"steps": []})
    import boss_engine.services.step_agents as sa
    monkeypatch.setattr(sa, "resolve", lambda *a, **k: {"agent": None})
    monkeypatch.setattr(runner, "_step_authority",
                        lambda *a, **k: __import__("contextlib").nullcontext())
    monkeypatch.setattr(runner, "_authorize_step_tool", lambda *a, **k: (None, None))
    monkeypatch.setattr(cts, "retry_code_task",
                        lambda *a, **k: {"run_id": "crun_live", "created": created})
    ctx = SimpleNamespace(settings=object(), engine=object(), jobs=object())
    return runner._start_recovery_build(_Conn(), ctx, project_id="p", run_id="wfr_1",
                                        step_id="deploy", build_run_id="crun_root",
                                        note="add version: 1")


def test_a_returned_in_flight_attempt_is_not_the_repair(monkeypatch):
    new_id, why = _start(monkeypatch, created=False)
    assert new_id == "" and "was not started" in why and "crun_live" in why


def test_a_started_repair_is_returned(monkeypatch):
    assert _start(monkeypatch, created=True) == ("crun_live", "")
