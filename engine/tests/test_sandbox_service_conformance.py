"""BOSSAPP-824: the built Go sandbox-service conforms to sandbox_contract.py.

Unlike test_sandbox_s1_go.py (which mocks the host), this builds the REAL Go
binary, starts it with the process execution backend on a random port, and
drives the REAL Python client through the whole lane:

    submit → status (queued/running → done) → result → release → 404

plus the honesty seams: health reports a live backend truthfully, a frozen
service still 422s coding submits over real HTTP, and a bad token is 401.

Skips cleanly when `go` is not installed. The process backend needs python3 +
git — the same prerequisites as the engine itself.
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from boss_engine.services.sandbox_contract import CONTRACT_VERSION
from boss_engine.services.sandbox_provider import (
    MicroVMClaimsFrozen,
    MicroVMProvider,
    run_echo_exercise,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GO_SERVICE = REPO_ROOT / "services" / "sandbox-service"

pytestmark = pytest.mark.skipif(shutil.which("go") is None, reason="go toolchain not installed")

TOKEN = "conformance-tok"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def service_binary(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("sandbox-service") / "sandbox-service"
    build = subprocess.run(
        ["go", "build", "-o", str(out), "./cmd/sandbox-service"],
        cwd=GO_SERVICE, capture_output=True, text=True, check=False,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return out


def _start_service(binary: Path, tmp: Path, *, frozen: bool) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(tmp),
        "BOSS_SANDBOX_LISTEN": f"127.0.0.1:{port}",
        "BOSS_SANDBOX_TOKEN": TOKEN,
        "BOSS_SANDBOX_BACKEND": "process",
        "BOSS_SANDBOX_WORK_ROOT": str(tmp / "work-root"),
        "BOSS_MICROVM_CLAIMS_FROZEN": "true" if frozen else "false",
    }
    proc = subprocess.Popen(
        [str(binary)], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base}/v1/sandbox/health", timeout=2.0)
            if r.status_code == 200:
                return proc, base
        except Exception:  # noqa: BLE001 — still booting
            pass
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    out = proc.stdout.read() if proc.stdout else ""
    proc.kill()
    raise AssertionError(f"sandbox-service did not come up: {out[:800]}")


@pytest.fixture(scope="module")
def live_service(service_binary, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("svc-live")
    proc, base = _start_service(service_binary, tmp, frozen=False)
    yield base
    proc.kill()


def _settings(base: str, **kwargs) -> SimpleNamespace:
    cfg = dict(
        sandbox_provider="microvm",
        sandbox_agent_url=base,
        sandbox_token=TOKEN,
        sandbox_agent_timeout_seconds=60,
        sandbox_poll_interval_seconds=1,
        microvm_claims_frozen=False,
    )
    cfg.update(kwargs)
    return SimpleNamespace(**cfg)


def test_health_reports_live_process_backend_honestly(live_service):
    r = httpx.get(f"{live_service}/v1/sandbox/health", timeout=5.0)
    body = r.json()
    assert body["contract_version"] == CONTRACT_VERSION
    assert body["ok"] is True
    assert body["execution_ready"] is True
    assert body["backend"] == "process"
    assert body["firecracker_live"] is False  # never invented
    assert body["claims_frozen"] is False


def test_bad_token_is_401(live_service):
    r = httpx.post(
        f"{live_service}/v1/sandbox/tasks",
        json={"contract_version": CONTRACT_VERSION},
        headers={"X-Sandbox-Token": "wrong"},
        timeout=5.0,
    )
    assert r.status_code == 401


def test_echo_exercise_round_trips_through_the_go_service(live_service):
    out = run_echo_exercise(_settings(live_service), timeout_s=60)
    assert out["final_status"] == "done"
    assert out["reached"] is True
    assert "SANDBOX_OK.txt" in out["diff_paths"]


def test_run_code_task_submit_poll_result_release_against_real_service(live_service):
    """The REAL MicroVMProvider path: submit → poll → CodeAgentResult → release."""
    provider = MicroVMProvider(_settings(live_service))
    result = provider.run_code_task(
        {
            "task_id": "crun_conformance",
            "engine": "echo",
            "test_mode": True,  # offline: the guest inits a throwaway repo
            "owner": "example-user",
            "repo": "boss-sandbox",
            "instructions": "prove the contract end to end",
            "acceptance_criteria": ["SANDBOX_OK.txt exists"],
            "test_commands": ["python3 -c 'print(1)'"],
            "timeout": 60,
        },
        tenant_id="default",
    )
    assert result.tests_passed is True
    assert [f.path for f in result.diff] == ["SANDBOX_OK.txt"]
    assert result.diff_summary.files_changed == 1
    assert result.tests and result.tests[0].exit_code == 0
    assert result.engine == "echo"

    sid = provider.last_sandbox_task_id
    assert sid

    # Success release frees the slot; the task is then honestly gone (404).
    provider.release(sid)
    r = httpx.get(
        f"{live_service}/v1/sandbox/tasks/{sid}",
        headers={"X-Sandbox-Token": TOKEN},
        timeout=5.0,
    )
    assert r.status_code == 404
    assert r.json()["reason"] == "sandbox_task_missing"


def test_frozen_service_still_422s_coding_over_real_http(service_binary, tmp_path):
    proc, base = _start_service(service_binary, tmp_path, frozen=True)
    try:
        provider = MicroVMProvider(_settings(base))
        with pytest.raises(MicroVMClaimsFrozen) as ei:
            provider.run_code_task(
                {
                    "task_id": "crun_frozen",
                    "engine": "claude_code",
                    "owner": "example-user",
                    "repo": "boss-sandbox",
                    "instructions": "must be refused",
                },
                tenant_id="default",
            )
        assert ei.value.status_code == 422
        assert ei.value.code == "microvm_claims_frozen"
    finally:
        proc.kill()


def test_submit_body_carries_test_mode_for_the_go_struct():
    """build_submit_request forwards test_mode (the Go SubmitRequest types it)."""
    from boss_engine.services.sandbox_contract import build_submit_request, validate_submit_request

    body = build_submit_request(
        {"owner": "o", "repo": "r", "engine": "echo", "test_mode": True},
        "default",
        task_id="crun_tm",
    )
    assert body["test_mode"] is True
    assert validate_submit_request(body) == []
    assert json.dumps(body)  # stays a JSON-serializable envelope
