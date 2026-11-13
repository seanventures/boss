"""#99 — the async Python⇄Go sandbox job contract (spec 10 §4).

The Python engine dispatches a whole coding task to an external **sandbox-service** (the Go
Firecracker service, S1) over HTTP and never blocks a worker on the execution: **submit → poll →
collect**. That contract was implicit (scattered through ``MicroVMProvider``); this makes it a single,
**versioned, documented, testable** surface the Go side conforms to. Full JSON reference:
``docs/infra/PYTHON_GO_SANDBOX_CONTRACT.md``.

Golden rule (spec 10 §4): the engine must run with **no** sandbox-service present — it falls back to the
in-process ``LocalSubprocessProvider``. So ``available(settings)`` gates the whole contract, and every
parse is defensive (a malformed guest response degrades to a failed run, never a crash).

Ownership: the **Python side is canonical**; the Go service conforms. Changes are **additive** and bump
:data:`CONTRACT_VERSION`.
"""
from __future__ import annotations

from typing import Any, Optional

CONTRACT_VERSION = "1.0"

# --- job status (the guest's poll response `status`) -------------------------
QUEUED, RUNNING, DONE, FAILED, TIMEOUT, CANCELLED = (
    "queued",
    "running",
    "done",
    "failed",
    "timeout",
    "cancelled",
)
_TERMINAL = {DONE, FAILED, TIMEOUT, CANCELLED}
_ALL_STATUSES = {QUEUED, RUNNING, DONE, FAILED, TIMEOUT, CANCELLED}


def is_terminal(status: Optional[str]) -> bool:
    """A poll can stop once the job reaches a terminal status."""
    return (status or "").lower() in _TERMINAL


def succeeded(status: Optional[str]) -> bool:
    return (status or "").lower() == DONE


def available(settings) -> bool:
    """Whether an external sandbox-service is configured. When False the engine MUST fall back to the
    local provider — the Go service is the scale substrate, never a hard dependency."""
    return bool((getattr(settings, "sandbox_agent_url", "") or "").strip())


# --- routes (the Go service implements these) --------------------------------
def submit_path() -> str:
    return "/v1/sandbox/tasks"


def status_path(sandbox_task_id: str) -> str:
    return f"/v1/sandbox/tasks/{sandbox_task_id}"


def release_path(sandbox_task_id: str) -> str:
    return f"/v1/sandbox/tasks/{sandbox_task_id}"     # DELETE on success; failed VMs are retained


def cancel_path(sandbox_task_id: str) -> str:
    """POST — operator/engine cancel. Additive (BOSS-97)."""
    return f"/v1/sandbox/tasks/{sandbox_task_id}/cancel"


def heartbeat_path(sandbox_task_id: str) -> str:
    """POST — fail-closed liveness while a job is running. Additive (BOSS-97)."""
    return f"/v1/sandbox/tasks/{sandbox_task_id}/heartbeat"


# --- submit request ----------------------------------------------------------
_ENGINE_ENV_MAP = (
    ("CLAUDE_CODE_OAUTH_TOKEN", "claude_oauth_token"),
    ("OPENAI_API_KEY", "openai_api_key"),
    ("OPENAI_BASE_URL", "openai_base_url"),
    ("OPENAI_MODEL", "model"),
    ("AZURE_OPENAI_ENDPOINT", "azure_endpoint"),
    ("AZURE_OPENAI_DEPLOYMENT", "azure_deployment"),
    ("AZURE_OPENAI_API_VERSION", "azure_api_version"),
    ("AZURE_OPENAI_KEY", "azure_key"),
)


def build_submit_request(spec: dict[str, Any], tenant_id: Optional[str], *,
                         task_id: Optional[str] = None) -> dict[str, Any]:
    """The versioned submit body: BOSS's flat guest_spec → the sandbox-service submit shape. Model
    creds ride in ``engine_env`` per task (never baked into the guest image)."""
    engine = str(spec.get("engine") or "openai").lower()
    env = {k: spec[f] for k, f in _ENGINE_ENV_MAP if spec.get(f)}
    tid = task_id or spec.get("task_id") or "crun_unknown"
    return {
        "contract_version": CONTRACT_VERSION,
        "task_id": tid,
        "tenant_id": tenant_id or "default",
        "repo": {
            "owner": spec.get("owner"), "name": spec.get("repo"),
            "base_branch": spec.get("base_branch") or "main",
            "work_branch": spec.get("work_branch") or f"boss/code/{tid}",
        },
        "git_token": spec.get("token") or "",
        "instructions": spec.get("instructions", ""),
        "acceptance_criteria": spec.get("acceptance_criteria", []),
        "test_commands": spec.get("test_commands", []),
        "engine": engine,
        "engine_env": env,
        "run_mutation": bool(spec.get("run_mutation", False)),
        # Diagnostic/offline flag the Go side already types (SubmitRequest.test_mode):
        # test_mode guests init a throwaway repo instead of cloning, so the
        # contract is drivable end-to-end with no network and no credentials.
        "test_mode": bool(spec.get("test_mode", False)),
        # Requested guest agent turn cap. The sandbox-service forwards it (top-level or via
        # resources.max_turns) to `claude --max-turns`; absent -> the guest defaults to ~40.
        "max_turns": spec.get("max_turns"),
        # Wall-clock budget for the guest run. The sandbox-service scales the VM TTL to this
        # (minus headroom); absent -> the guest's ~900s default, which a high max_turns overruns.
        "timeout": spec.get("timeout"),
        "resources": {**(spec.get("resources") or {}),
                      **({"max_turns": spec["max_turns"]} if spec.get("max_turns") else {})},
    }


# --- response parsing (defensive) --------------------------------------------
def parse_submit_response(resp: Any) -> Optional[str]:
    """Extract the sandbox_task_id from a submit response, or None if the guest didn't return one."""
    if isinstance(resp, dict):
        sid = resp.get("sandbox_task_id") or resp.get("id")
        return str(sid) if sid else None
    return None


def parse_status(resp: Any) -> dict[str, Any]:
    """Normalize a poll response to {status, phase, terminal, succeeded}. An unrecognized/empty status
    is treated as RUNNING (keep polling) rather than a false terminal."""
    d = resp if isinstance(resp, dict) else {}
    status = str(d.get("status") or "").lower()
    if status not in _ALL_STATUSES:
        status = RUNNING
    return {"status": status, "phase": d.get("phase"),
            "terminal": is_terminal(status), "succeeded": succeeded(status)}


def parse_result(resp: Any) -> dict[str, Any]:
    """Normalize the guest's result (present on a DONE/FAILED status) into the fields the pipeline maps
    back into a CodeAgentResult: the diff, whether the guest's own tests passed, a summary, and cost."""
    d = resp if isinstance(resp, dict) else {}
    result = d.get("result") if isinstance(d.get("result"), dict) else d
    diff_raw = result.get("diff") or result.get("files") or []
    diff = [{"path": str(f.get("path")), "content": str(f.get("content", ""))}
            for f in diff_raw if isinstance(f, dict) and f.get("path")]
    return {
        "diff": diff,
        "tests_passed": bool(result.get("tests_passed")),
        "summary": str(result.get("summary", "")),
        "cost_usd_micros": int(result.get("cost_usd_micros") or 0),
        "error": result.get("error"),
    }


def validate_submit_request(body: dict[str, Any]) -> list[str]:
    """Return a list of contract violations in a submit body (empty = valid). For tests + a Go
    conformance check."""
    problems: list[str] = []
    if body.get("contract_version") != CONTRACT_VERSION:
        problems.append(f"contract_version must be {CONTRACT_VERSION}")
    if not body.get("task_id"):
        problems.append("task_id is required")
    repo = body.get("repo") or {}
    if not repo.get("owner") or not repo.get("name"):
        problems.append("repo.owner and repo.name are required")
    if not isinstance(body.get("engine_env"), dict):
        problems.append("engine_env must be an object")
    return problems
