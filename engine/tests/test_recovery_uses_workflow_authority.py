import contextlib
from types import SimpleNamespace

import pytest

from boss_engine.services import workflow_capability_runner as runner
from boss_engine.tools.registry import CURRENT_TOOL_AUTH


@pytest.fixture
def recovery(monkeypatch):
    monkeypatch.setattr(runner.step_recovery, "enabled", lambda *args: True)
    monkeypatch.setattr(runner.workflows, "_run_spec", lambda *args: {
        "steps": [{"id": "build", "kind": "capability"}]})
    monkeypatch.setattr(runner.step_recovery, "agent_of", lambda *args: {
        "name": "Helm", "role": "engineering_captain"})
    return lambda: runner._recovery_begin(
        object(), SimpleNamespace(settings=object()), project_id="project_one",
        run_id="wfr_one", step_id="build", kind="build", output={},
        evidence={"failure": {"why": "test author failed"}})


def test_recovery_decides_inside_the_recorded_step_authority(monkeypatch, recovery):
    verified = object()
    previous = CURRENT_TOOL_AUTH.get()

    @contextlib.contextmanager
    def authority(ctx, connection, project, context, step, role):
        assert project == "project_one"
        assert context == {"run_id": "wfr_one"}
        assert step == {"id": "build.recovery"}
        assert role == "engineering_captain"
        token = CURRENT_TOOL_AUTH.set(verified)
        try:
            yield verified
        finally:
            CURRENT_TOOL_AUTH.reset(token)

    def decide(*args, **kwargs):
        assert CURRENT_TOOL_AUTH.get() is verified
        return {"action": "retry"}

    monkeypatch.setattr(runner, "_step_authority", authority)
    monkeypatch.setattr(runner.step_recovery, "decide", decide)
    assert recovery()[0] == {"action": "retry"}
    assert CURRENT_TOOL_AUTH.get() is previous


def test_ended_authority_cannot_start_recovery(monkeypatch, recovery):
    def denied(*args, **kwargs):
        raise runner.CapabilityExecutionError("authority has ended; renew it")

    def unexpected(*args, **kwargs):
        pytest.fail("recovery must not run after an authority refusal")

    monkeypatch.setattr(runner, "_step_authority", denied)
    monkeypatch.setattr(runner.step_recovery, "decide", unexpected)
    with pytest.raises(runner.CapabilityExecutionError, match="renew"):
        recovery()
