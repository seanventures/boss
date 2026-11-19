import pytest

from boss_engine.services import harness_failures as failures
from boss_engine.services.governed_code_task_service import write_tests_exhausted_message


DENIAL = "bwrap: No permissions to create a new namespace, likely because the kernel does not allow non-privileged user namespaces."


@pytest.mark.parametrize("engine", ["codex", "claude_code"])
@pytest.mark.parametrize("message", [
    DENIAL,
    "bwrap: Creating new namespace failed: Operation not permitted",
    "bwrap: Creating new namespace failed: Permission denied",
])
def test_namespace_denial_is_a_sandbox_failure(engine, message):
    verdict = failures.classify(message, engine=engine, where="tool_result", ok=False)
    assert verdict is not None
    assert verdict.kind == "sandbox_unavailable"
    assert "seccomp and AppArmor" in verdict.fix
    assert "GitHub repository will not fix" in verdict.fix


def test_source_search_is_not_a_namespace_failure():
    assert failures.classify(
        f"tests/fixture.py:10:{DENIAL}", engine="codex", where="tool_result", ok=True,
    ) is None


@pytest.mark.parametrize("stronger_lane", [False, True])
def test_missing_test_files_does_not_invent_a_cause(stronger_lane):
    message = write_tests_exhausted_message(stronger_lane=stronger_lane)
    assert "likely" not in message
    assert "cause is not established" in message
    assert "No application test result" in message
