from dataclasses import replace
import pytest

from boss_engine.services import governed_code_task_service as gov
from boss_engine.services import verification_target as vt
from boss_engine.services.sandbox import CommandResult


def result(*, timed_out=True, stderr="", exit_code=None):
    return CommandResult(command=vt.NPM_INSTALL_CMD, argv=[], exit_code=exit_code,
                         duration_ms=300000, timed_out=timed_out, stdout="", stderr=stderr)


def test_timeout_runs_once_per_command_and_workspace():
    circuit = vt.InstallCircuitBreaker()
    calls = []

    def execute():
        calls.append(True)
        return result()

    first, reused = circuit.run(vt.NPM_INSTALL_CMD, "/workspace", execute)
    assert not reused and first.timed_out
    second, reused = circuit.run(vt.NPM_INSTALL_CMD, "/workspace", execute)
    assert reused and not second.ok and second.duration_ms == 0
    assert "Tests did not run" in second.stderr
    assert len(calls) == 1
    circuit.run(vt.NPM_INSTALL_CMD, "/other", execute)
    assert len(calls) == 2
    vt.InstallCircuitBreaker().run(vt.NPM_INSTALL_CMD, "/workspace", execute)
    assert len(calls) == 3


def test_dependency_conflicts_remain_repairable():
    circuit = vt.InstallCircuitBreaker()
    failed = result(timed_out=False, exit_code=1, stderr="npm error ERESOLVE conflicting peer")
    circuit.run(vt.NPM_INSTALL_CMD, "/workspace", lambda: failed)
    success, reused = circuit.run(vt.NPM_INSTALL_CMD, "/workspace",
                                 lambda: replace(failed, exit_code=0, stderr=""))
    assert success.ok and not reused


def test_tls_failure_does_not_ask_coder_to_change_dependency_versions():
    failure = vt.install_failure(result(timed_out=False, exit_code=1,
                                stderr="npm error code UNABLE_TO_VERIFY_LEAF_SIGNATURE"))
    assert vt.install_environment_failure(failure)
    assert not vt.install_rounds(lambda: (failure, ""), lambda prompt: 1 / 0)
    assert "Resolve the dependency conflict" not in vt.install_repair_evidence(failure)


def test_install_timeout_opens_failing_draft_without_cold_code_repair():
    failure = vt.install_failure(result())
    faults = gov.classify_gate_faults(failing=["written_tests_pass"],
                                     evidence={"install_failure": failure})
    assert faults == {"written_tests_pass": "HARNESS"}
    from boss_engine.services import checks_failing
    assert checks_failing.stop_or_open("block", ["written_tests_pass"], faults=faults) == checks_failing.ACTION
    assert checks_failing.environment_stop(["written_tests_pass"], faults=faults) == "environment_only"
    assert checks_failing.stop_or_open("block", ["written_tests_pass"], faults=faults,
                                      open_pr=False) == "block"
    assert checks_failing.stop_or_open("block", ["no_committed_secrets"], faults=faults) == "block"


def test_real_test_failure_stays_work():
    assert gov.classify_gate_faults(failing=["written_tests_pass"], evidence={}) == {
        "written_tests_pass": "WORK"}


@pytest.mark.parametrize("code", ["E401", "E403"])
def test_registry_access_failure_is_not_a_code_repair(code):
    failure = vt.install_failure(result(timed_out=False, exit_code=1,
                                       stderr=f"npm error code {code}"))
    assert vt.install_environment_failure(failure)
    assert not vt.install_rounds(lambda: (failure, ""), lambda prompt: 1 / 0)
