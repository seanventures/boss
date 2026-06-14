"""SandboxProvider — the seam that decides *where* a coding task's sandbox runs.

The `CodeAgent` and the durable `code.task` pipeline don't care whether the
sandbox is a local subprocess, a single-tenant microVM on BOSS infra, or a
runner inside the customer's VPC. They ask a provider for a sandbox and use the
same `Sandbox` surface. Swapping execution substrate (architecture §5.3/§5.4) is
a provider swap, not a rewrite.

Providers:
  - LocalSubprocessProvider  → today: a per-task EphemeralSandbox (temp clone +
    allowlisted subprocess). No isolation; for BOSS-infra Phase 0/1.
  - MicroVMProvider          → Phase 2: a single-tenant, ephemeral Firecracker/
    gVisor microVM per task, with warm-pool / snapshot-resume / per-repo cache.
    Stubbed here with the lifecycle surface documented; not yet implemented.
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from ..config import Settings
from ..schemas import FileChange
from .sandbox import DiffSummary, EphemeralSandbox


@runtime_checkable
class Sandbox(Protocol):
    """The surface the code pipeline uses — implemented by EphemeralSandbox and,
    later, by a microVM-backed sandbox."""

    path: Optional[object]

    def provision(
        self,
        owner: str,
        repo: str,
        *,
        base_branch: str = "main",
        work_branch: Optional[str] = None,
        token: Optional[str] = None,
    ) -> object: ...

    def run(self, command: str, *, timeout: Optional[int] = None): ...

    def diff(self) -> list[FileChange]: ...

    def diff_summary(self) -> DiffSummary: ...

    def teardown(self) -> None: ...


class SandboxProvider(Protocol):
    def acquire(self, *, task_id: str, tenant_id: Optional[str] = None) -> Sandbox: ...


class LocalSubprocessProvider:
    """Per-task EphemeralSandbox (temp clone + allowlisted subprocess). The
    `tenant_id` is accepted for interface parity but unused — there is no
    cross-task reuse or pooling at this tier."""

    kind = "local"

    def __init__(self, settings: Settings):
        self.settings = settings

    def acquire(self, *, task_id: str, tenant_id: Optional[str] = None) -> Sandbox:
        return EphemeralSandbox(self.settings, task_id=task_id)


class MicroVMProvider:
    """Single-tenant, ephemeral sandbox running on a Sandbox Host (a container
    stand-in today; a Kata/Firecracker microVM later — architecture §5.3/§5.4).

    Unlike the local provider, the WHOLE pipeline (clone -> agent -> test -> diff)
    runs inside the sandbox; the control plane dispatches a spec and collects
    metadata + the diff. So this provider exposes ``run_code_task`` instead of the
    granular ``acquire/provision/run/diff`` surface — ``code_task_service``
    branches on which one a provider offers.
    """

    kind = "microvm"

    def __init__(self, settings: Settings):
        self.settings = settings

    def acquire(self, *, task_id: str, tenant_id: Optional[str] = None) -> Sandbox:
        raise NotImplementedError(
            "MicroVMProvider dispatches whole tasks via run_code_task(); it has no "
            "granular acquire(). This path is selected automatically by code_task_service."
        )

    def run_code_task(self, spec: dict, *, tenant_id: Optional[str] = None):
        """Dispatch the task spec to the Sandbox Agent; map the JSON result back
        into a CodeAgentResult so the rest of code_task_service is unchanged."""
        import httpx

        from ..schemas import FileChange
        from .code_agent import CodeAgentResult
        from .sandbox import CommandResult, DiffSummary

        url = (self.settings.sandbox_agent_url or "").rstrip("/") + "/v1/sandbox/run-task"
        with httpx.Client(timeout=self.settings.sandbox_agent_timeout_seconds) as client:
            resp = client.post(url, json={"tenant_id": tenant_id or "default", "spec": spec})
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "ok":
            raise RuntimeError(data.get("error") or "sandbox run failed")

        diff = [FileChange(path=f["path"], content=f["content"]) for f in data.get("diff", [])]
        ds = data.get("diff_summary") or {}
        diff_summary = DiffSummary(
            files_changed=ds.get("files_changed", 0), additions=ds.get("additions", 0),
            deletions=ds.get("deletions", 0), paths=ds.get("paths", []),
        )
        tests = [
            CommandResult(command=t.get("command", ""), argv=[], exit_code=t.get("exit_code"),
                          duration_ms=t.get("duration_ms", 0), timed_out=t.get("timed_out", False),
                          stdout="", stderr="")
            for t in data.get("tests", [])
        ]
        return CodeAgentResult(
            summary=data.get("summary", ""), diff=diff, diff_summary=diff_summary,
            tests=tests, tests_passed=bool(data.get("tests_passed")),
            cost_usd_micros=int(data.get("cost_usd_micros", 0)), usage=data.get("usage") or {},
            transcript=data.get("summary", ""), turns=int(data.get("turns", 0)),
            engine=data.get("engine", "openai"),
        )


def make_sandbox_provider(settings: Settings) -> SandboxProvider:
    kind = (getattr(settings, "sandbox_provider", "local") or "local").lower()
    if kind in ("local", "subprocess"):
        return LocalSubprocessProvider(settings)
    if kind in ("microvm", "firecracker", "gvisor"):
        return MicroVMProvider(settings)
    raise ValueError(f"Unknown BOSS_SANDBOX_PROVIDER: {settings.sandbox_provider!r}")
