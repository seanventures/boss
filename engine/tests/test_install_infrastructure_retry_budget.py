"""An install that failed for its ENVIRONMENT is not the code's, and is retried within bounds.

Contributed from an enterprise install (2026-11): behind a TLS-intercepting proxy, or an
internal registry without the sandbox's token, BOSS asked the coder to "resolve the dependency
conflict", ran repair rounds on tests that never ran and re-ran a slow install several times a
build. The class, for every package manager BOSS installs with (npm, pnpm, yarn, pip, Go, Maven):

  * one classifier (suite_environment.classify_install) says which install failures no code
    change can fix — certificate, network, registry credential (E401, proxy 407), a registry or
    egress proxy refusing the registry itself — and which ONE is the coder's after all: a
    package-curation firewall (Artifactory Curation, Nexus Firewall) blocking one version;
  * retries are bounded and keyed on the manifest and lockfiles: a timeout gets one retry with
    double the time within a budget (a cold cache behind a slow proxy), a transient network
    failure one backoff retry; the same failing install is never re-run on the same files;
  * a timeout is the environment's only with network evidence — a hanging postinstall is code.
"""
from dataclasses import replace

import pytest

from boss_engine.services import checks_failing
from boss_engine.services import governed_code_task_service as gov
from boss_engine.services import suite_environment as se
from boss_engine.services import verification_target as vt
from boss_engine.services.sandbox import CommandResult


def result(*, timed_out=False, stderr="", exit_code=1, command=vt.NPM_INSTALL_CMD):
    return CommandResult(command=command, argv=[], exit_code=exit_code,
                         duration_ms=300000 if timed_out else 1000, timed_out=timed_out,
                         stdout="", stderr=stderr)


OK = result(exit_code=0)
TLS = "npm error code UNABLE_TO_VERIFY_LEAF_SIGNATURE\nnpm error unable to verify the first certificate"
DNS = ("npm error code EAI_AGAIN\nnpm error request to https://registry.npmjs.org/x failed, "
       "reason: getaddrinfo EAI_AGAIN registry.npmjs.org")
CURATED = ("npm error code E403\nnpm error 403 Forbidden - GET https://repo.example.com/api/npm/"
           "npm/left-pad/-/left-pad-1.3.0.tgz - download was blocked by the Curation service due "
           "to the following policies violated: critical-cve")


class Runs:
    """execute(limit) for the breaker: plays the given results in order, records each limit."""

    def __init__(self, *results):
        self.results = list(results)
        self.limits = []

    def __call__(self, limit=None):
        self.limits.append(limit)
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


# ── Bounded retries, keyed on what is installed ──────────────────────────────────────────────

def test_a_timeout_is_retried_once_with_double_the_time_then_never_on_the_same_files(tmp_path):
    (tmp_path / "package.json").write_text('{"name":"a"}')
    circuit = vt.InstallCircuitBreaker(budget_seconds=3600)
    runs = Runs(result(timed_out=True, exit_code=None))
    first, reused = circuit.run(vt.NPM_INSTALL_CMD, tmp_path, runs, timeout=300)
    assert not reused and first.timed_out
    assert runs.limits == [300, 600]                       # one retry, double the time
    again, reused = circuit.run(vt.NPM_INSTALL_CMD, tmp_path, runs, timeout=300)
    assert reused and again.duration_ms == 0 and "Tests did not run" in again.stderr
    assert runs.limits == [300, 600]                       # the same files: not re-run
    (tmp_path / "package.json").write_text('{"name":"a","dependencies":{"b":"1"}}')
    circuit.run(vt.NPM_INSTALL_CMD, tmp_path, runs, timeout=300)
    assert len(runs.limits) > 2                            # a manifest change is a new install


def test_a_slow_proxy_that_passes_on_the_retry_passes(tmp_path):
    circuit = vt.InstallCircuitBreaker(budget_seconds=3600)
    runs = Runs(result(timed_out=True, exit_code=None), OK)
    done, reused = circuit.run(vt.NPM_INSTALL_CMD, tmp_path, runs, timeout=300)
    assert done.ok and not reused and circuit.retries == [{"why": "timeout", "timeout": 600}]


def test_the_retry_budget_bounds_the_timeout_retries(tmp_path):
    circuit = vt.InstallCircuitBreaker(budget_seconds=900)
    runs = Runs(result(timed_out=True, exit_code=None))
    circuit.run(vt.NPM_INSTALL_CMD, tmp_path, runs, timeout=300)
    (tmp_path / "package-lock.json").write_text("{}")     # a new install, same build
    circuit.run(vt.NPM_INSTALL_CMD, tmp_path, runs, timeout=300)
    assert runs.limits == [300, 600, 300]                  # the second had no budget left


def test_a_transient_network_failure_gets_one_backoff_retry(tmp_path):
    slept = []
    circuit = vt.InstallCircuitBreaker(backoff_seconds=5, sleep=slept.append)
    runs = Runs(result(stderr=DNS), OK)
    done, _ = circuit.run(vt.NPM_INSTALL_CMD, tmp_path, runs, timeout=300)
    assert done.ok and slept == [5] and len(runs.limits) == 2


def test_dependency_conflicts_are_never_suppressed(tmp_path):
    circuit = vt.InstallCircuitBreaker(sleep=lambda s: None)
    failed = result(stderr="npm error code ERESOLVE\nnpm error ERESOLVE could not resolve")
    circuit.run(vt.NPM_INSTALL_CMD, tmp_path, lambda: failed)
    done, reused = circuit.run(vt.NPM_INSTALL_CMD, tmp_path, lambda: replace(failed, exit_code=0,
                                                                            stderr=""))
    assert done.ok and not reused


# ── Which install failures are the environment's ─────────────────────────────────────────────

@pytest.mark.parametrize("output", [
    TLS,
    "pip._vendor.urllib3.exceptions.SSLError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed",
    "go: example.com/m@v1.2.3: x509: certificate signed by unknown authority",
    "[ERROR] PKIX path building failed: unable to find valid certification path",
    DNS,
    "npm error code E401\nnpm error Unable to authenticate, need: Basic realm=\"Artifactory\"",
    "npm error code E407\nnpm error 407 Proxy Authentication Required",
    " ERR_PNPM_FETCH_401  GET https://registry.example.com/a: Unauthorized - 401",
    'error An unexpected error occurred: "https://registry.example.com/a: Request failed \\"401 Unauthorized\\"".',
    "ERROR: HTTP error 401 while getting https://pypi.example.com/simple/requests/",
    "[ERROR] Failed to transfer file https://repo.example.com/maven2/x.pom. status code: 401, reason phrase: Unauthorized (401)",
    "go: example.com/m: reading https://goproxy.example.com/example.com/m/@v/list: 401 Unauthorized",
    "npm error code E403\nnpm error 403 Forbidden - GET https://repo.example.com/api/npm/npm/left-pad",
    "npm error tunneling socket could not be established, statusCode=403",
])
def test_an_environment_install_failure_never_asks_the_coder(output):
    failure = vt.install_failure(result(stderr=output))
    assert vt.install_environment_failure(failure), se.classify_install(output)
    assert not vt.install_rounds(lambda: (failure, output), lambda prompt: 1 / 0)
    said = vt.install_repair_evidence(failure, output)
    assert "Resolve the dependency conflict" not in said
    assert "do not change application code" in said


def test_a_timeout_with_no_network_evidence_is_the_codes(tmp_path):
    failure = vt.install_failure(result(timed_out=True, exit_code=None,
                                        stderr="> postinstall\n> node build-native.js"))
    assert not vt.install_environment_failure(failure)
    assert vt.INSTALL_TIMEOUT_INSTRUCTION in vt.install_repair_evidence(failure)
    assert gov.classify_gate_faults(failing=["written_tests_pass"],
                                    evidence={"install_failure": failure}) == {
        "written_tests_pass": "WORK"}


def test_a_timeout_on_a_slow_network_opens_a_failing_draft_without_code_repair():
    failure = vt.install_failure(result(
        timed_out=True, exit_code=None,
        stderr="npm http fetch GET https://registry.example.com/x attempt 3 failed with ETIMEDOUT"))
    assert vt.install_environment_failure(failure)
    faults = gov.classify_gate_faults(failing=["written_tests_pass"],
                                      evidence={"install_failure": failure})
    assert faults == {"written_tests_pass": "HARNESS"}
    assert checks_failing.stop_or_open("block", ["written_tests_pass"], faults=faults) == \
        checks_failing.ACTION
    assert checks_failing.environment_stop(["written_tests_pass"], faults=faults) == \
        "environment_only"
    assert checks_failing.stop_or_open("block", ["written_tests_pass"], faults=faults,
                                       open_pr=False) == "block"
    assert checks_failing.stop_or_open("block", ["no_committed_secrets"], faults=faults) == "block"


def test_a_real_test_failure_stays_work():
    assert gov.classify_gate_faults(failing=["written_tests_pass"], evidence={}) == {
        "written_tests_pass": "WORK"}


# ── A curation firewall's block is the coder's: one round, an approved version ───────────────

@pytest.mark.parametrize("output, package", [
    (CURATED, "left-pad@1.3.0"),
    ("npm error code E403\nnpm error 403 Forbidden - GET https://repo.example.com/api/npm/npm/"
     "@scope/lib/-/lib-2.0.1.tgz", "@scope/lib@2.0.1"),
    ("go: example.com/m@v1.2.3: reading https://goproxy.example.com/example.com/m/@v/v1.2.3.zip: "
     "403 Forbidden", "example.com/m@v1.2.3"),
    ("ERROR: HTTP error 403 while getting https://pypi.example.com/packages/requests-2.31.0-py3-"
     "none-any.whl", "requests-2.31.0-py3-none-any.whl"),
])
def test_a_curation_block_gets_one_round_to_pick_an_approved_version(output, package):
    failure = vt.install_failure(result(stderr=output))
    assert not vt.install_environment_failure(failure)
    assert vt.registry_policy_blocked(failure) == package
    asked = []
    assert not vt.install_rounds(lambda: (failure, output),
                                 lambda prompt: asked.append(prompt) or True)
    assert len(asked) == 1
    assert package in asked[0] and "do not remove the dependency" in asked[0]
    assert "Resolve the dependency conflict" not in asked[0]


def test_the_install_classifier_extends_the_suite_classifier():
    # One taxonomy: the suite's network errnos and the install's network kind agree.
    assert se.classify_code("EAI_AGAIN")["kind"] == "connect"
    assert se.classify_install(DNS)["kind"] == "network"
    assert se.classify_install("npm error code ERESOLVE") is None


def test_go_module_download_is_an_install():
    assert vt.is_install_command("go mod download")
