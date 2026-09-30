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

# Honest Community GA message when BOSS_MICROVM_CLAIMS_FROZEN=true.
# Kept as a module constant so API, provider, and tests share one string.
MICROVM_CLAIMS_FROZEN_MESSAGE = (
    "microVM execution is not claimed for Community GA "
    "(BOSS_MICROVM_CLAIMS_FROZEN=true). Host connectivity may still be reported; "
    "use BOSS_SANDBOX_PROVIDER=local for coding, or clear the freeze after a "
    "proven rootfs bake. See docs/product/SANDBOX_GA_STATUS.md."
)
FIRECRACKER_NOT_ENABLED_MESSAGE = (
    "Firecracker backend is not enabled; the in-repo S1 sandbox-service "
    "scaffold does not run live microVMs (firecracker_not_enabled)."
)


class SandboxPlaneError(RuntimeError):
    """Fail-closed sandbox plane error mapped to 4xx/503 by the API (never 500).

    Subclasses of RuntimeError so code_task_service marks the run failed visibly
    instead of crashing the worker.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 503,
        code: str = "sandbox_unavailable",
    ):
        super().__init__(message)
        self.message = message
        self.status_code = int(status_code)
        self.code = code


class MicroVMClaimsFrozen(SandboxPlaneError):
    """BOSS_MICROVM_CLAIMS_FROZEN blocks microVM execution claims."""

    def __init__(self, message: str = MICROVM_CLAIMS_FROZEN_MESSAGE):
        super().__init__(message, status_code=422, code="microvm_claims_frozen")


class FirecrackerNotEnabled(SandboxPlaneError):
    """In-repo S1 scaffold / host without a live Firecracker backend."""

    def __init__(self, message: str = FIRECRACKER_NOT_ENABLED_MESSAGE):
        super().__init__(message, status_code=422, code="firecracker_not_enabled")


class SandboxHostUnavailable(SandboxPlaneError):
    """Sandbox host unreachable or not configured — fail closed with 503."""

    def __init__(self, message: str):
        super().__init__(message, status_code=503, code="sandbox_host_unavailable")


class SandboxTaskMissing(SandboxPlaneError):
    """Host returned 404 for a sandbox/task id — gone or never created.

    Retrying lands in the DLQ. Fail closed with 422 so the job completes as a
    visible blocked failure instead of a transient host error.
    """

    def __init__(self, message: str):
        super().__init__(message, status_code=422, code="sandbox_task_missing")


def map_sandbox_http_error(
    exc: Exception,
    *,
    base: str,
    sandbox_task_id: Optional[str] = None,
    action: str = "contact",
) -> SandboxPlaneError:
    """Classify host HTTP failures: 404 / missing task → 422; connectivity → 503."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    body = ""
    if response is not None:
        try:
            body = (getattr(response, "text", None) or "")[:200]
        except Exception:  # noqa: BLE001
            body = ""
    blob = f"{exc} {body}".lower()
    missing = status == 404 or "sandbox not found" in blob or "task not found" in blob
    if missing:
        sid = sandbox_task_id or "unknown"
        return SandboxTaskMissing(
            f"sandbox host task missing (HTTP 404) for {sid} at {base}; "
            "task is gone or never created — fail closed, not retryable."
        )
    # Host-side fail-closed reasons must stay 422, never become a 503 retry.
    if status == 422 or "microvm_claims_frozen" in blob or "firecracker_not_enabled" in blob:
        if "microvm_claims_frozen" in blob or "not claimed for community ga" in blob:
            return MicroVMClaimsFrozen()
        if "firecracker_not_enabled" in blob or "does not run live microvm" in blob:
            return FirecrackerNotEnabled()
        return SandboxPlaneError(
            body or f"sandbox-service rejected {action} at {base}",
            status_code=422,
            code="sandbox_rejected",
        )
    return SandboxHostUnavailable(
        f"cannot {action} sandbox-service at {base}: {type(exc).__name__}: {exc}"
    )


def microvm_claims_are_frozen(settings) -> bool:
    return bool(getattr(settings, "microvm_claims_frozen", False))


def ensure_microvm_execution_allowed(settings) -> None:
    """Raise MicroVMClaimsFrozen when Community GA has frozen microVM claims."""
    if microvm_claims_are_frozen(settings):
        raise MicroVMClaimsFrozen()


def sandbox_host_base(settings) -> str:
    return (getattr(settings, "sandbox_agent_url", "") or "").strip().rstrip("/")


# Freeze-safe echo diagnostic. engine=echo + test_mode only — never claude_code.
ECHO_ENGINE = "echo"
ECHO_EXERCISE_TASK_ID = "exercise_echo_api"


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

    def _require_base(self) -> str:
        base = sandbox_host_base(self.settings)
        if not base:
            raise SandboxHostUnavailable(
                "sandbox host not configured (BOSS_SANDBOX_AGENT_URL is empty)"
            )
        return base

    def release(self, sandbox_task_id: Optional[str]) -> None:
        """Destroy a VM (best-effort). Called on SUCCESS to free capacity; failed VMs
        are intentionally NOT released so the service retains the rootfs for inspection."""
        if not sandbox_task_id:
            return
        try:
            base = self._require_base()
        except SandboxHostUnavailable:
            return
        try:
            import httpx
            with httpx.Client(timeout=15.0) as client:
                client.delete(f"{base}/v1/sandbox/tasks/{sandbox_task_id}", headers=self._headers())
        except Exception:  # noqa: BLE001
            pass

    def hold(self, sandbox_task_id: Optional[str], *, ttl_seconds: int, reason: str) -> bool:
        """Bound retention explicitly so a successful build can back a rootfs preview."""
        if not sandbox_task_id:
            return False
        try:
            base = self._require_base()
        except SandboxHostUnavailable:
            return False
        try:
            import httpx
            with httpx.Client(timeout=15.0) as client:
                response = client.post(
                    f"{base}/v1/sandbox/tasks/{sandbox_task_id}/hold",
                    json={"ttl_seconds": int(ttl_seconds), "reason": reason},
                    headers=self._headers(),
                )
                response.raise_for_status()
            return True
        except Exception:  # older sandbox services fall back to normal TTL/fallback preview
            return False

    def serve_preview(
        self, sandbox_task_id: str, *, ttl_seconds: int, command: str = ""
    ) -> dict:
        """Ask the sandbox service to boot the app in the retained build rootfs."""
        ensure_microvm_execution_allowed(self.settings)
        base = self._require_base()
        import httpx
        try:
            with httpx.Client(timeout=45.0) as client:
                response = client.post(
                    f"{base}/v1/sandbox/tasks/{sandbox_task_id}/preview",
                    json={"ttl_seconds": int(ttl_seconds), "command": command},
                    headers=self._headers(),
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as exc:
            raise map_sandbox_http_error(
                exc, base=base, sandbox_task_id=sandbox_task_id, action="preview"
            ) from exc

    def acquire(self, *, task_id: str, tenant_id: Optional[str] = None) -> Sandbox:
        raise NotImplementedError(
            "MicroVMProvider dispatches whole tasks via run_code_task(); it has no "
            "granular acquire(). This path is selected automatically by code_task_service."
        )

    def _submit_body(self, spec: dict, tenant_id: Optional[str]) -> dict:
        """Map BOSS's internal flat guest_spec -> the versioned sandbox-service submit body (#99
        contract). Model creds ride in engine_env (per task, never baked into the guest image)."""
        from .sandbox_contract import build_submit_request
        task_id = spec.get("task_id") or make_id("crun")
        return build_submit_request(spec, tenant_id, task_id=task_id)

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

        # Community GA honesty: never claim microVM execution while frozen.
        ensure_microvm_execution_allowed(self.settings)
        base = self._require_base()
        headers = {}
        if self.settings.sandbox_token:
            headers["X-Sandbox-Token"] = self.settings.sandbox_token
        body = self._submit_body(spec, tenant_id)
        timeout_s = int(self.settings.sandbox_agent_timeout_seconds or 1500)
        poll = max(3, int(getattr(self.settings, "sandbox_poll_interval_seconds", 5)))

        data = None
        last_status = None
        sid = None
        with httpx.Client(timeout=60.0) as client:
            try:
                r = client.post(f"{base}/v1/sandbox/tasks", json=body, headers=headers)
                r.raise_for_status()
            except httpx.HTTPError as exc:
                # 404 = host has no such task/route (fail-closed 422). Connectivity → 503.
                raise map_sandbox_http_error(
                    exc, base=base, action="reach"
                ) from exc
            sid = r.json().get("sandbox_task_id")
            if not sid:
                raise SandboxHostUnavailable(
                    "sandbox-service did not return a sandbox_task_id"
                )
            self.last_sandbox_task_id = sid  # expose so the control plane can retain/inspect
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                time.sleep(poll)
                try:
                    pr = client.get(f"{base}/v1/sandbox/tasks/{sid}", headers=headers)
                    pr.raise_for_status()
                except httpx.HTTPError as exc:
                    raise map_sandbox_http_error(
                        exc, base=base, sandbox_task_id=sid, action="poll"
                    ) from exc
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


def build_echo_submit_body(*, task_id: str = ECHO_EXERCISE_TASK_ID) -> dict:
    """Versioned submit envelope for the freeze-safe echo diagnostic.

    Hard-coded ``engine=echo`` + ``test_mode``. Never carries claude_code,
    model credentials, or a coding claim.
    """
    from .sandbox_contract import CONTRACT_VERSION

    return {
        "contract_version": CONTRACT_VERSION,
        "task_id": task_id,
        "test_mode": True,
        "engine": ECHO_ENGINE,
        "instructions": "Deterministic sandbox echo exercise — no LLM.",
        "acceptance_criteria": ["SANDBOX_OK.txt exists"],
        "test_commands": [],
        "repo": {},
        "engine_env": {},
        "run_mutation": False,
        "resources": {"vcpu": 1, "mem_mb": 512, "timeout_s": 180},
    }


def run_echo_exercise(
    settings,
    *,
    http_client=None,
    sleep=None,
    timeout_s: Optional[int] = None,
) -> dict:
    """Submit/poll a throwaway echo VM. Safe while claims are frozen.

    Does **not** call :func:`ensure_microvm_execution_allowed` — echo does not
    claim coding / claude_code / microVM GA. Missing or unreachable host
    raises :class:`SandboxHostUnavailable` (API maps to HTTP 503).
    """
    import time

    import httpx

    from .sandbox_contract import is_terminal, parse_submit_response

    base = sandbox_host_base(settings)
    if not base:
        raise SandboxHostUnavailable(
            "sandbox host not configured (BOSS_SANDBOX_AGENT_URL is empty)"
        )

    sleep_fn = time.sleep if sleep is None else sleep
    timeout = 180 if timeout_s is None else int(timeout_s)
    poll = max(0, int(getattr(settings, "sandbox_poll_interval_seconds", 3) or 0))
    body = build_echo_submit_body()
    if body.get("engine") != ECHO_ENGINE or not body.get("test_mode"):
        raise SandboxPlaneError(
            "echo exercise refused a non-echo submit body",
            status_code=422,
            code="echo_engine_required",
        )
    headers: dict[str, str] = {}
    token = getattr(settings, "sandbox_token", "") or ""
    if token:
        headers["X-Sandbox-Token"] = token

    out: dict = {
        "url": base,
        "claims_frozen": microvm_claims_are_frozen(settings),
        "engine": ECHO_ENGINE,
        "test_mode": True,
        "coding_claimed": False,
        "claude_code_claimed": False,
    }

    owns_client = http_client is None
    client = http_client if http_client is not None else httpx.Client(timeout=20.0)
    try:
        try:
            submitted = client.post(f"{base}/v1/sandbox/tasks", json=body, headers=headers)
            out["submit_http"] = getattr(submitted, "status_code", None)
            submitted.raise_for_status()
        except httpx.HTTPError as exc:
            raise map_sandbox_http_error(exc, base=base, action="submit echo") from exc
        payload = submitted.json() if hasattr(submitted, "json") else {}
        sid = parse_submit_response(payload)
        if not sid:
            raise SandboxHostUnavailable(
                "sandbox-service did not return a sandbox_task_id"
            )
        out["sandbox_task_id"] = sid
        last: Optional[dict] = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if poll:
                sleep_fn(poll)
            try:
                polled = client.get(f"{base}/v1/sandbox/tasks/{sid}", headers=headers)
                polled.raise_for_status()
            except httpx.HTTPError as exc:
                raise map_sandbox_http_error(
                    exc, base=base, sandbox_task_id=sid, action="poll echo"
                ) from exc
            last = polled.json() if hasattr(polled, "json") else {}
            if not isinstance(last, dict):
                last = {}
            if is_terminal(last.get("status")):
                break
        result = (last or {}).get("result") or {}
        if not isinstance(result, dict):
            result = {}
        out["final_status"] = (last or {}).get("status")
        out["summary"] = result.get("summary")
        out["diff_paths"] = [
            item.get("path")
            for item in (result.get("diff") or [])
            if isinstance(item, dict)
        ]
        out["reached"] = out["final_status"] == "done" and bool(out["diff_paths"])
        return out
    finally:
        if owns_client:
            close = getattr(client, "close", None)
            if callable(close):
                close()


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



def make_sandbox_provider(settings: Settings) -> SandboxProvider:
    kind = (getattr(settings, "sandbox_provider", "local") or "local").lower()
    if kind in ("local", "subprocess"):
        return LocalSubprocessProvider(settings)
    if kind in ("microvm", "firecracker", "gvisor"):
        # Community GA bar is working software, not Firecracker marketing.
        # While microVM coding claims are frozen, new code.task runs on the
        # local subprocess substrate (the path that already shipped BugBox).
        # MicroVMProvider.run_code_task still 422s if called directly.
        if microvm_claims_are_frozen(settings):
            return LocalSubprocessProvider(settings)
        return MicroVMProvider(settings)
    raise ValueError(f"Unknown BOSS_SANDBOX_PROVIDER: {settings.sandbox_provider!r}")
