"""Community GA: microVM claim freeze + fail-closed host errors (S2).

When BOSS_MICROVM_CLAIMS_FROZEN is true, execution claims must raise a clear
422-class error rather than pretending Firecracker coding works. Missing host
must surface as SandboxHostUnavailable (503), never an opaque crash.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from boss_engine.services.sandbox_provider import (
    MICROVM_CLAIMS_FROZEN_MESSAGE,
    LocalSubprocessProvider,
    MicroVMClaimsFrozen,
    MicroVMProvider,
    SandboxHostUnavailable,
    ensure_microvm_execution_allowed,
    make_sandbox_provider,
    microvm_claims_are_frozen,
)
from boss_engine.services import sandbox_console


def _settings(**kwargs):
    base = dict(
        sandbox_provider="microvm",
        sandbox_agent_url="http://sandbox-host:8077",
        sandbox_token="tok",
        sandbox_agent_timeout_seconds=30,
        sandbox_poll_interval_seconds=1,
        microvm_claims_frozen=False,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_microvm_claims_frozen_flag():
    assert microvm_claims_are_frozen(_settings(microvm_claims_frozen=True)) is True
    assert microvm_claims_are_frozen(_settings(microvm_claims_frozen=False)) is False
    assert microvm_claims_are_frozen(SimpleNamespace()) is False


def test_ensure_microvm_execution_allowed_raises_when_frozen():
    with pytest.raises(MicroVMClaimsFrozen) as ei:
        ensure_microvm_execution_allowed(_settings(microvm_claims_frozen=True))
    assert ei.value.status_code == 422
    assert ei.value.code == "microvm_claims_frozen"
    assert "not claimed for Community GA" in ei.value.message
    assert "BOSS_MICROVM_CLAIMS_FROZEN" in ei.value.message
    # Shared constant so API and provider messages stay aligned.
    assert MICROVM_CLAIMS_FROZEN_MESSAGE in ei.value.message


def test_run_code_task_refuses_when_claims_frozen():
    p = MicroVMProvider(_settings(microvm_claims_frozen=True))
    with pytest.raises(MicroVMClaimsFrozen):
        p.run_code_task({"owner": "o", "repo": "r", "task_id": "crun_x"})


def test_serve_preview_refuses_when_claims_frozen():
    p = MicroVMProvider(_settings(microvm_claims_frozen=True))
    with pytest.raises(MicroVMClaimsFrozen):
        p.serve_preview("sbx_1", ttl_seconds=60)


def test_run_code_task_no_host_url_is_503_class():
    p = MicroVMProvider(_settings(sandbox_agent_url="", microvm_claims_frozen=False))
    with pytest.raises(SandboxHostUnavailable) as ei:
        p.run_code_task({"owner": "o", "repo": "r", "task_id": "crun_x"})
    assert ei.value.status_code == 503
    assert ei.value.code == "sandbox_host_unavailable"


def test_run_code_task_connection_error_is_host_unavailable():
    """Happy-path wiring is present but host is down → SandboxHostUnavailable (not bare 500)."""
    p = MicroVMProvider(_settings(microvm_claims_frozen=False))

    class _Boom:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            import httpx

            raise httpx.ConnectError("connection refused", request=MagicMock())

    with patch("httpx.Client", return_value=_Boom()):
        with pytest.raises(SandboxHostUnavailable) as ei:
            p.run_code_task({"owner": "o", "repo": "r", "task_id": "crun_x"})
    assert ei.value.status_code == 503
    assert "cannot reach sandbox-service" in ei.value.message


def test_local_provider_happy_path_unaffected_by_freeze():
    """Local substrate remains the GA coding path while microVM claims are frozen."""
    s = _settings(sandbox_provider="local", microvm_claims_frozen=True)
    provider = make_sandbox_provider(s)
    assert isinstance(provider, LocalSubprocessProvider)
    sandbox = provider.acquire(task_id="crun_local")
    assert sandbox is not None
    # Freeze is microVM-specific: local acquire still works; MicroVMProvider still blocked.
    assert microvm_claims_are_frozen(s) is True
    with pytest.raises(MicroVMClaimsFrozen):
        MicroVMProvider(s).run_code_task({"owner": "o", "repo": "r", "task_id": "x"})


def test_list_sandboxes_reports_claims_frozen_without_raising():
    s = _settings(microvm_claims_frozen=True, sandbox_agent_url="")
    result = sandbox_console.list_sandboxes(s)
    assert result["host"]["connected"] is False
    assert result["host"]["claims_frozen"] is True
    assert "not claimed" in (result["host"].get("claims_message") or "")


def test_list_sandboxes_no_host_is_not_exception():
    """Fail closed as structured payload (API returns 200 with connected=false)."""
    s = _settings(sandbox_agent_url="", microvm_claims_frozen=False)
    result = sandbox_console.list_sandboxes(s)
    assert result["host"]["connected"] is False
    assert "unavailable" in result


def test_get_tree_missing_host_raises_503_class():
    s = _settings(sandbox_agent_url="")
    with pytest.raises(SandboxHostUnavailable) as ei:
        sandbox_console.get_tree(s, "sbx_missing")
    assert ei.value.status_code == 503


def test_exec_command_frozen_raises_422_class():
    s = _settings(microvm_claims_frozen=True)
    with pytest.raises(MicroVMClaimsFrozen) as ei:
        sandbox_console.exec_command(MagicMock(), s, "sbx_1", "ls")
    assert ei.value.status_code == 422
