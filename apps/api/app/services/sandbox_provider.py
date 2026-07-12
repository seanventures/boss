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
from .core import make_id
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
        # Set to the sandbox-service task id once submitted, so the control plane can
        # persist it (to inspect a retained VM later) and release it on success.
        self.last_sandbox_task_id: Optional[str] = None

    def _headers(self) -> dict:
        return {"X-Sandbox-Token": self.settings.sandbox_token} if self.settings.sandbox_token else {}

    def release(self, sandbox_task_id: Optional[str]) -> None:
        """Destroy a VM (best-effort). Called on SUCCESS to free capacity; failed VMs
        are intentionally NOT released so the service retains the rootfs for inspection."""
        if not sandbox_task_id:
            return
        base = (self.settings.sandbox_agent_url or "").rstrip("/")
        try:
            import httpx
            with httpx.Client(timeout=15.0) as client:
                client.delete(f"{base}/v1/sandbox/tasks/{sandbox_task_id}", headers=self._headers())
        except Exception:  # noqa: BLE001
            pass

    def acquire(self, *, task_id: str, tenant_id: Optional[str] = None) -> Sandbox:
        raise NotImplementedError(
            "MicroVMProvider dispatches whole tasks via run_code_task(); it has no "
            "granular acquire(). This path is selected automatically by code_task_service."
        )

    def _submit_body(self, spec: dict, tenant_id: Optional[str]) -> dict:
        """Map BOSS's internal flat guest_spec -> the sandbox-service §2 submit body.
        Model creds ride in engine_env (per task, never baked into the guest image)."""
        engine = (spec.get("engine") or "openai").lower()
        env: dict = {}
        for key, field in (
            ("CLAUDE_CODE_OAUTH_TOKEN", "claude_oauth_token"),
            ("OPENAI_API_KEY", "openai_api_key"),
            ("OPENAI_BASE_URL", "openai_base_url"),
            ("OPENAI_MODEL", "model"),
            ("AZURE_OPENAI_ENDPOINT", "azure_endpoint"),
            ("AZURE_OPENAI_DEPLOYMENT", "azure_deployment"),
            ("AZURE_OPENAI_API_VERSION", "azure_api_version"),
            ("AZURE_OPENAI_KEY", "azure_key"),
        ):
            val = spec.get(field)
            if val:
                env[key] = val
        task_id = spec.get("task_id") or make_id("crun")
        return {
            "task_id": task_id,
            "tenant_id": tenant_id or "default",
            "repo": {
                "owner": spec.get("owner"), "name": spec.get("repo"),
                "base_branch": spec.get("base_branch") or "main",
                "work_branch": spec.get("work_branch") or f"boss/code/{task_id}",
            },
            "git_token": spec.get("token") or "",
            "instructions": spec.get("instructions", ""),
            "acceptance_criteria": spec.get("acceptance_criteria", []),
            "test_commands": spec.get("test_commands", []),
            "engine": engine,
            "engine_env": env,
            "run_mutation": bool(spec.get("run_mutation", False)),
            "resources": spec.get("resources") or {},
        }

    def run_code_task(self, spec: dict, *, tenant_id: Optional[str] = None):
        """Dispatch the task to the sandbox-service over the async §2 contract
        (POST submit -> GET poll -> DELETE release) and map the guest's returned
        result back into a CodeAgentResult so the rest of code_task_service is
        unchanged. The whole clone->agent->test->diff pipeline runs guest-side;
        only governance metadata + the diff cross back."""
        import time

        import httpx

        from ..schemas import FileChange
        from .code_agent import CodeAgentResult
        from .sandbox import CommandResult, DiffSummary

        base = (self.settings.sandbox_agent_url or "").rstrip("/")
        headers = {}
        if self.settings.sandbox_token:
            headers["X-Sandbox-Token"] = self.settings.sandbox_token
        body = self._submit_body(spec, tenant_id)
        timeout_s = int(self.settings.sandbox_agent_timeout_seconds or 1500)
        poll = max(3, int(getattr(self.settings, "sandbox_poll_interval_seconds", 5)))

        data = None
        last_status = None
        with httpx.Client(timeout=60.0) as client:
            try:
                r = client.post(f"{base}/v1/sandbox/tasks", json=body, headers=headers)
                r.raise_for_status()
            except httpx.HTTPError as exc:
                # Fail fast + loud instead of hanging: the control plane can't reach the
                # sandbox-service (bad URL, network, or auth). Surfaces as a failed run.
                raise RuntimeError(
                    f"cannot reach sandbox-service at {base}: {type(exc).__name__}: {exc}"
                ) from exc
            sid = r.json().get("sandbox_task_id")
            if not sid:
                raise RuntimeError("sandbox-service did not return a sandbox_task_id")
            self.last_sandbox_task_id = sid  # expose so the control plane can retain/inspect
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                time.sleep(poll)
                pr = client.get(f"{base}/v1/sandbox/tasks/{sid}", headers=headers)
                pr.raise_for_status()
                pdata = pr.json()
                last_status = pdata.get("status")
                if last_status in ("done", "failed", "timeout"):
                    data = pdata
                    break
            # NOTE: we do NOT release the VM here. The control plane releases it on
            # SUCCESS (to free capacity) and RETAINS it on any failure so the service
            # keeps the rootfs (~2h) for inspection. See code_task_service.

        if data is None:
            raise RuntimeError(
                f"sandbox task {sid} did not finish within {timeout_s}s (last status={last_status})"
            )
        if data.get("status") != "done":
            raise RuntimeError(data.get("error") or f"sandbox task {data.get('status')}")

        # The guest's /work/result.json is nested under `result` in the §2 envelope.
        result = data.get("result") or {}
        if result.get("status") not in (None, "ok"):
            raise RuntimeError(result.get("error") or "guest agent reported failure")

        diff = [FileChange(path=f["path"], content=f["content"]) for f in result.get("diff", [])]
        ds = result.get("diff_summary") or {}
        diff_summary = DiffSummary(
            files_changed=ds.get("files_changed", 0), additions=ds.get("additions", 0),
            deletions=ds.get("deletions", 0), paths=ds.get("paths", []),
        )
        tests = [
            CommandResult(command=t.get("command", ""), argv=[], exit_code=t.get("exit_code"),
                          duration_ms=t.get("duration_ms", 0), timed_out=t.get("timed_out", False),
                          # The guest's captured test output IS the repair-loop evidence —
                          # concrete failures fed back to the implementer on retry.
                          stdout=(t.get("output") or "")[-4000:], stderr="")
            for t in result.get("tests", [])
        ]
        return CodeAgentResult(
            summary=result.get("summary", ""), diff=diff, diff_summary=diff_summary,
            tests=tests, tests_passed=bool(result.get("tests_passed")),
            cost_usd_micros=int(result.get("cost_usd_micros", 0)), usage=result.get("usage") or {},
            transcript=result.get("summary", ""), turns=int(result.get("turns", 0)),
            engine=result.get("engine", "openai"),
        )


def make_sandbox_provider(settings: Settings) -> SandboxProvider:
    kind = (getattr(settings, "sandbox_provider", "local") or "local").lower()
    if kind in ("local", "subprocess"):
        return LocalSubprocessProvider(settings)
    if kind in ("microvm", "firecracker", "gvisor"):
        return MicroVMProvider(settings)
    raise ValueError(f"Unknown BOSS_SANDBOX_PROVIDER: {settings.sandbox_provider!r}")
