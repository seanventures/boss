"""BOSS-98 / S1: Go sandbox-service contract + freeze fail-closed + local default.

Seams:
  - versioned submit/poll contract shape (shared golden fixture)
  - Firecracker path fail-closed when claims are frozen (named reason)
  - LocalSubprocessProvider still works (engine has no Go dependency)
  - host.connected is never invented true from the frozen S1 scaffold
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from boss_engine.config import Settings
from boss_engine.services import sandbox_console
from boss_engine.services.sandbox_contract import (
    CONTRACT_VERSION,
    build_submit_request,
    validate_submit_request,
)
from boss_engine.services.sandbox_provider import (
    FIRECRACKER_NOT_ENABLED_MESSAGE,
    FirecrackerNotEnabled,
    LocalSubprocessProvider,
    MicroVMClaimsFrozen,
    MicroVMProvider,
    make_sandbox_provider,
    map_sandbox_http_error,
    microvm_claims_are_frozen,
)
from boss_engine.services.concurrency_governance import go_scale_track

REPO_ROOT = Path(__file__).resolve().parents[2]
GO_SERVICE = REPO_ROOT / "services" / "sandbox-service"
GOLDEN_SUBMIT = GO_SERVICE / "testdata" / "submit_valid.json"


def _settings(**kwargs):
    base = dict(
        sandbox_provider="local",
        sandbox_agent_url="http://127.0.0.1:8078",
        sandbox_token="tok",
        sandbox_agent_timeout_seconds=30,
        sandbox_poll_interval_seconds=1,
        microvm_claims_frozen=True,
        sandbox_validation_allowed_commands="python3,echo,true",
        sandbox_validation_timeout_seconds=10,
        sandbox_validation_output_limit_chars=4000,
        sandbox_root_path=None,
        sandbox_clone_depth=1,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_config_default_keeps_microvm_claims_frozen():
    field = Settings.model_fields["microvm_claims_frozen"]
    assert field.default is True
    tmpl = (REPO_ROOT / "app.env.tmpl").read_text(encoding="utf-8")
    assert "BOSS_MICROVM_CLAIMS_FROZEN=true" in tmpl
    assert "BOSS_MICROVM_CLAIMS_FROZEN=false" not in tmpl


def test_golden_submit_fixture_matches_python_contract():
    body = json.loads(GOLDEN_SUBMIT.read_text(encoding="utf-8"))
    assert body["contract_version"] == CONTRACT_VERSION == "1.0"
    assert validate_submit_request(body) == []
    built = build_submit_request(
        {
            "owner": "paramg",
            "repo": "boss-sandbox",
            "instructions": "do X",
            "acceptance_criteria": ["a"],
            "test_commands": ["pytest -q"],
            "engine": "claude_code",
            "claude_oauth_token": "tok",
        },
        "default",
        task_id="crun_s1_contract",
    )
    assert built["contract_version"] == body["contract_version"]
    assert built["task_id"] == body["task_id"]
    assert built["repo"] == body["repo"]
    assert built["engine"] == body["engine"]
    assert built["engine_env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"


def test_frozen_firecracker_path_fails_closed_named_reason():
    s = _settings(sandbox_provider="microvm", microvm_claims_frozen=True)
    # Working-software default: freeze routes new runs to local.
    provider = make_sandbox_provider(s)
    assert isinstance(provider, LocalSubprocessProvider)
    # Direct MicroVM path still fail-closes — no silent success.
    with pytest.raises(MicroVMClaimsFrozen) as ei:
        MicroVMProvider(s).run_code_task(
            {"owner": "o", "repo": "r", "task_id": "crun_x"}
        )
    assert ei.value.code == "microvm_claims_frozen"
    assert ei.value.status_code == 422
    assert "BOSS_MICROVM_CLAIMS_FROZEN" in ei.value.message


def test_host_422_claims_frozen_is_not_reclassified_as_503():
    import httpx

    request = httpx.Request("POST", "http://127.0.0.1:8078/v1/sandbox/tasks")
    response = httpx.Response(
        422,
        request=request,
        text=json.dumps(
            {
                "error": "microVM execution is not claimed for Community GA",
                "reason": "microvm_claims_frozen",
                "code": "microvm_claims_frozen",
            }
        ),
    )
    exc = httpx.HTTPStatusError("422", request=request, response=response)
    mapped = map_sandbox_http_error(exc, base="http://127.0.0.1:8078", action="reach")
    assert isinstance(mapped, MicroVMClaimsFrozen)
    assert mapped.status_code == 422
    assert mapped.code == "microvm_claims_frozen"


def test_host_422_firecracker_not_enabled_is_named():
    import httpx

    request = httpx.Request("POST", "http://127.0.0.1:8078/v1/sandbox/tasks")
    response = httpx.Response(
        422,
        request=request,
        text=json.dumps(
            {
                "error": FIRECRACKER_NOT_ENABLED_MESSAGE,
                "reason": "firecracker_not_enabled",
                "code": "firecracker_not_enabled",
            }
        ),
    )
    exc = httpx.HTTPStatusError("422", request=request, response=response)
    mapped = map_sandbox_http_error(exc, base="http://127.0.0.1:8078", action="reach")
    assert isinstance(mapped, FirecrackerNotEnabled)
    assert mapped.status_code == 422
    assert mapped.code == "firecracker_not_enabled"


def test_local_subprocess_still_runs_while_frozen(tmp_path):
    s = _settings(sandbox_provider="local", microvm_claims_frozen=True)
    provider = make_sandbox_provider(s)
    assert isinstance(provider, LocalSubprocessProvider)
    sandbox = provider.acquire(task_id="crun_local")
    sandbox.path = tmp_path
    result = sandbox.run("python3 -c 'print(42)'")
    assert result.exit_code == 0
    assert "42" in (result.stdout or "")
    assert microvm_claims_are_frozen(s) is True


def test_engine_selects_local_without_any_go_service():
    s = _settings(sandbox_provider="local", sandbox_agent_url="", microvm_claims_frozen=True)
    provider = make_sandbox_provider(s)
    assert provider.kind == "local"
    assert isinstance(provider.acquire(task_id="crun_offline"), object)


def test_interpret_health_does_not_invent_connected_when_frozen_scaffold():
    host = sandbox_console.interpret_sandbox_host_health(
        {
            "ok": False,
            "service": "up",
            "backend": "none",
            "firecracker_live": False,
            "execution_ready": False,
            "claims_frozen": True,
            "reason": "microvm_claims_frozen",
            "capacity": 0,
            "available": 0,
            "running": 0,
        },
        claims_frozen=True,
        url="http://127.0.0.1:8078",
    )
    assert host["connected"] is False
    assert host["firecracker_live"] is False
    assert host["execution_ready"] is False
    assert host["claims_frozen"] is True


def test_list_sandboxes_against_s1_scaffold_is_not_connected(monkeypatch):
    """HTTP 200 from the Go scaffold must not become host.connected=true."""

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "ok": False,
                "service": "up",
                "backend": "none",
                "firecracker_live": False,
                "execution_ready": False,
                "claims_frozen": True,
                "reason": "microvm_claims_frozen",
                "capacity": 0,
                "available": 0,
                "running": 0,
            }

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(sandbox_console.httpx, "Client", _Client)
    result = sandbox_console.list_sandboxes(
        _settings(microvm_claims_frozen=True, sandbox_agent_url="http://127.0.0.1:8078")
    )
    assert result["host"]["connected"] is False
    assert result["host"]["claims_frozen"] is True
    assert result["host"]["firecracker_live"] is False


def test_list_sandboxes_legacy_ok_true_still_reports_connectivity():
    """A real host that says ok:true may report connected even while frozen."""
    host = sandbox_console.interpret_sandbox_host_health(
        {"ok": True, "capacity": 8, "available": 8, "running": 0},
        claims_frozen=True,
        url="http://sandbox-host:8077",
    )
    assert host["connected"] is True
    assert host["claims_frozen"] is True
    assert host["firecracker_live"] is False  # not claimed unless the host says so


def test_go_scale_track_honesty_stays_unshipped():
    track = go_scale_track(settings=_settings(microvm_claims_frozen=True))
    assert track["community_ga_required"] is False
    assert track["go_runtime_shipped"] is False
    assert track["s1_go_sandbox_service"] is False
    assert track["s1_in_repo_scaffold"] is True
    assert track["s2_go_tenant_gateway"] is False
    assert track["s3_go_event_dispatcher"] is False
    assert track["microvm_claims_frozen"] is True


@pytest.mark.skipif(shutil.which("go") is None, reason="go toolchain not installed")
def test_go_sandbox_service_unit_tests():
    completed = subprocess.run(
        ["go", "test", "./..."],
        cwd=GO_SERVICE,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_run_code_task_does_not_invent_success_when_host_returns_frozen_422():
    p = MicroVMProvider(_settings(sandbox_provider="microvm", microvm_claims_frozen=False))

    class _FrozenHost:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            import httpx

            request = httpx.Request("POST", "http://127.0.0.1:8078/v1/sandbox/tasks")
            response = httpx.Response(
                422,
                request=request,
                text=json.dumps(
                    {
                        "error": "microVM execution is not claimed for Community GA",
                        "reason": "microvm_claims_frozen",
                        "code": "microvm_claims_frozen",
                    }
                ),
            )
            raise httpx.HTTPStatusError("422", request=request, response=response)

        def get(self, *a, **k):
            raise AssertionError("must not poll after fail-closed submit")

    with patch("httpx.Client", return_value=_FrozenHost()):
        with pytest.raises(MicroVMClaimsFrozen):
            p.run_code_task({"owner": "o", "repo": "r", "task_id": "crun_x"})
