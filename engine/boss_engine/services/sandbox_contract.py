"""#99 — the async Python⇄Go sandbox job contract (spec 10 §4), v1.1 (P8b S9, BOSSAPP-1070).

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

v1.1 — NO CREDENTIAL IN THE BODY (boss-agent-hosts-lld.md §4.2, S9). v1.0 carried the run's git
token and model credential (``git_token``, ``engine_env``) INTO the guest; P8-pre closed the worst
leaks and P8b removes the fields. A v1.1 body carries instead:

  * ``handles``   — ``{egr, mdl, mcpgw, run}``: opaque ``boss_*`` placeholders, worthless off the VM
                     (they are redeemed only through the VM's own vsock socket on its own host);
  * ``endpoints`` — the guest-loopback ports boss-vsockd bridges (71xx: model, git/api, mcp,
                     run plane, connect);
  * ``mcp``       — the MCP gateway URLs (``servers: {name: url}``), handle in ``handles.mcpgw``.

Both validators (Python here, Go ``contract.ValidateSubmit``) refuse a v1.1 body that carries a raw
credential field, or — on a ``strict_secrets`` host — any secret-shaped value (``env_refs``'s name
shape, and the well-known key prefixes). **v1.0 is accepted only for the frozen legacy lane**
(``BOSS_SANDBOX_CONTRACT_LEGACY=1``), with a deprecation log line, and never by a strict host.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

log = logging.getLogger(__name__)

CONTRACT_VERSION = "1.1"
LEGACY_CONTRACT_VERSION = "1.0"
LEGACY_ENV = "BOSS_SANDBOX_CONTRACT_LEGACY"

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


def legacy_lane_enabled() -> bool:
    """The frozen v1.0 lane, on only when the operator says so (BOSS_SANDBOX_CONTRACT_LEGACY=1)."""
    return (os.environ.get(LEGACY_ENV) or "").strip().lower() in ("1", "true", "yes")


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


# --- v1.1: handles, endpoints, mcp --------------------------------------------
#: The guest's handles, by kind, and the prefix each must carry (identity/runs.py, mcp_gateway,
#: streaming_model_gateway). A value under one of these names that does not carry its prefix is a
#: raw credential, and refused.
HANDLE_PREFIXES: dict[str, str] = {
    "egr": "boss_egr_", "mdl": "boss_mdl_", "mcpgw": "boss_mcpgw_", "run": "boss_run_",
}
#: Guest-loopback endpoints boss-vsockd bridges to boss-hostd (LLD §3.8; HOST_API.md §4.1).
#: Fixed by the host image, sent so a guest never guesses a port.
DEFAULT_ENDPOINTS: dict[str, str] = {
    "model": "http://127.0.0.1:7101",
    "git": "http://127.0.0.1:7102",
    "mcp": "http://127.0.0.1:7103",
    "run_plane": "http://127.0.0.1:7104",
    "connect": "http://127.0.0.1:7105",
}
_ENDPOINT_RE = re.compile(r"^http://127\.0\.0\.1:71\d\d$")

#: Fields a v1.1 body never carries: the v1.0 credential fields and every flat guest-spec name
#: a credential ever rode under. Present (even empty) → refused by name.
RAW_CREDENTIAL_FIELDS: frozenset[str] = frozenset({
    "git_token", "engine_env", "token", "claude_oauth_token", "anthropic_api_key",
    "openai_api_key", "azure_key", "github_token", "gh_token", "api_key", "password",
})
#: Well-known credential shapes, refused wherever they appear in a strict body (a key stuffed
#: into `instructions` or `resources` is still a key in the guest).
_SECRET_VALUE = re.compile(
    r"(sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{32,}|gh[pousr]_[A-Za-z0-9]{30,}"
    r"|github_pat_[A-Za-z0-9_]{40,}|glpat-[A-Za-z0-9_-]{20,}|xox[abprs]-[A-Za-z0-9-]{20,}"
    r"|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,})")

RAW_CREDENTIAL_FIELD = "raw_credential_field"
SECRET_SHAPED_VALUE = "secret_shaped_value"
CONTRACT_DEPRECATED = "contract_deprecated"
CONTRACT_LEGACY_REFUSED = "contract_legacy_refused"


class RawCredentialRefused(ValueError):
    """A v1.1 body about to carry a credential into the guest. Carries field NAMES only."""

    def __init__(self, reason: str, problems: list[str]):
        self.reason = reason
        self.problems = list(problems)
        super().__init__("; ".join(problems))


#: env_refs._SECRET_NAME (env_refs.py:253-257) minus the bare ``_KEY$`` — a contract body names
#: ids that way (dedupe_key, idempotency_key); API/ACCESS/PRIVATE keys still match. Go mirrors it.
_SECRET_NAME = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSPHRASE|_PAT$|API_KEY|ACCESS_KEY|PRIVATE_KEY"
                          r"|CREDENTIAL)")


def _name_carries_secret(name: str) -> bool:
    from .env_refs import _NOT_SECRET_NAME

    upper = str(name).upper()
    return bool(_SECRET_NAME.search(upper)) and not _NOT_SECRET_NAME.search(upper)


def secret_problems(body: Any, *, strict: bool = True) -> list[str]:
    """Every place a v1.1 body carries a credential (field names, never values). A raw credential
    field is refused always; a secret-shaped value (a secret-named key with a non-handle value,
    or a well-known key shape anywhere) only on a strict host. Go's ``SecretProblems`` has the
    same order and wording."""
    problems: list[str] = []
    if not isinstance(body, dict):
        return problems
    for name in sorted(RAW_CREDENTIAL_FIELDS & set(body)):
        problems.append(f"{RAW_CREDENTIAL_FIELD}: {name} is not a v1.1 field; the guest holds handles")
    handles = body.get("handles") if isinstance(body.get("handles"), dict) else {}
    for kind in sorted(HANDLE_PREFIXES):
        value, prefix = handles.get(kind), HANDLE_PREFIXES[kind]
        if value and not str(value).startswith(prefix):
            problems.append(f"{RAW_CREDENTIAL_FIELD}: handles.{kind} is not a {prefix} handle")
    if strict:
        problems += _walk(body, "")
    return problems


def _walk(node: Any, path: str) -> list[str]:
    out: list[str] = []
    if isinstance(node, dict):
        for key in sorted(node, key=str):
            value = node[key]
            here = f"{path}.{key}" if path else str(key)
            if here.startswith("handles.") and isinstance(value, str) and value.startswith("boss_"):
                continue
            if isinstance(value, str) and value and _name_carries_secret(str(key)) \
                    and not value.startswith("boss_"):
                out.append(f"{SECRET_SHAPED_VALUE}: {here} is named like a secret and holds a value")
                continue
            out += _walk(value, here)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            out += _walk(value, f"{path}[{i}]")
    elif isinstance(node, str) and _SECRET_VALUE.search(node):
        out.append(f"{SECRET_SHAPED_VALUE}: {path or 'body'} holds a credential-shaped value")
    return out


def ensure_no_raw_credential(body: dict[str, Any], *, strict: bool = True) -> None:
    """Raise RawCredentialRefused, by name, before a body that carries a credential is sent."""
    problems = secret_problems(body, strict=strict)
    if problems:
        reason = (RAW_CREDENTIAL_FIELD if problems[0].startswith(RAW_CREDENTIAL_FIELD + ":")
                  else SECRET_SHAPED_VALUE)
        raise RawCredentialRefused(reason, problems)


# --- v1.0 (legacy lane only) ---------------------------------------------------
_ENGINE_ENV_MAP = (
    ("CLAUDE_CODE_OAUTH_TOKEN", "claude_oauth_token"),
    # The console's Anthropic API key, when a claude_code guest runs on it (claude_credential).
    # NOT a name the guest reads (Go's engineEnvToSpec has no entry, the guest no field): it is
    # mapped so a claude_code run on the API key reaches validation and is REFUSED by name
    # (engine_env_unknown) instead of booting a guest with no credential (BOSSAPP-1061 defect 1).
    ("ANTHROPIC_API_KEY", "anthropic_api_key"),
    ("OPENAI_API_KEY", "openai_api_key"),
    ("OPENAI_BASE_URL", "openai_base_url"),
    ("OPENAI_MODEL", "model"),
    ("AZURE_OPENAI_ENDPOINT", "azure_endpoint"),
    ("AZURE_OPENAI_DEPLOYMENT", "azure_deployment"),
    ("AZURE_OPENAI_API_VERSION", "azure_api_version"),
    ("AZURE_OPENAI_KEY", "azure_key"),
)


#: The engine_env names a v1.0 guest reads — exactly Go's ``engineEnvToSpec`` keys
#: (services/sandbox-service/internal/contract/contract.go). Anything else is refused by name.
GUEST_ENGINE_ENV_NAMES = frozenset({
    "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
    "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT", "AZURE_OPENAI_API_VERSION",
    "AZURE_OPENAI_KEY",
})
_OPENAI_LANE = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "AZURE_OPENAI_ENDPOINT",
                "AZURE_OPENAI_DEPLOYMENT", "AZURE_OPENAI_API_VERSION", "AZURE_OPENAI_KEY")
#: Which engine reads which names (Go's ``engineEnvByEngine``). A guest is sent ONLY its own
#: engine's credential: no OpenAI or Azure key ever reaches a claude_code guest (P8-pre S5).
#: ``claude_code`` lists ANTHROPIC_API_KEY so the API-key case is carried to validation and
#: refused there by name, never dropped silently on the way.
ENGINE_ENV_BY_ENGINE: dict[str, tuple[str, ...]] = {
    "echo": (),
    "claude_code": ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"),
    "openai": _OPENAI_LANE,
    "native": _OPENAI_LANE,
}

ENGINE_ENV_UNKNOWN = "engine_env_unknown"
ENGINE_ENV_NOT_FOR_ENGINE = "engine_env_not_for_engine"


class EngineEnvRefused(ValueError):
    """A v1.0 submit body whose engine_env the guest cannot honour. Carries names only, never a
    value; ``reason`` is ENGINE_ENV_UNKNOWN or ENGINE_ENV_NOT_FOR_ENGINE."""

    def __init__(self, reason: str, problems: list[str]):
        self.reason = reason
        self.problems = list(problems)
        super().__init__("; ".join(problems))


def engine_env_for(engine: str, env: dict[str, Any]) -> dict[str, Any]:
    """Only the selected engine's names. An engine this table does not list is not narrowed."""
    allowed = ENGINE_ENV_BY_ENGINE.get(str(engine or "").strip().lower())
    if allowed is None:
        return dict(env)
    return {k: v for k, v in env.items() if k in allowed}


def engine_env_problems(engine: Any, env: Any) -> list[str]:
    """Named refusals for a v1.0 engine_env (Go's ``EngineEnvProblems``, same order and wording)."""
    if not isinstance(env, dict):
        return []
    name_of_engine = str(engine or "").strip().lower()
    allowed = ENGINE_ENV_BY_ENGINE.get(name_of_engine)
    problems = []
    for name in sorted(env):
        if name not in GUEST_ENGINE_ENV_NAMES:
            problems.append(f"{ENGINE_ENV_UNKNOWN}: engine_env {name} is not a name the guest reads")
        elif allowed is not None and name not in allowed:
            problems.append(f"{ENGINE_ENV_NOT_FOR_ENGINE}: engine_env {name} is not read by "
                            f"engine {name_of_engine}")
    return problems


# --- submit request ----------------------------------------------------------
def _common(spec: dict[str, Any], tenant_id: Optional[str], tid: str, engine: str) -> dict[str, Any]:
    return {
        "task_id": tid,
        "tenant_id": tenant_id or "default",
        "repo": {
            "owner": spec.get("owner"), "name": spec.get("repo"),
            "base_branch": spec.get("base_branch") or "main",
            "work_branch": spec.get("work_branch") or f"boss/code/{tid}",
        },
        "instructions": spec.get("instructions", ""),
        "acceptance_criteria": spec.get("acceptance_criteria", []),
        "test_commands": spec.get("test_commands", []),
        "engine": engine,
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


def build_submit_request(spec: dict[str, Any], tenant_id: Optional[str], *,
                         task_id: Optional[str] = None, handles: Optional[dict[str, str]] = None,
                         endpoints: Optional[dict[str, str]] = None,
                         mcp: Optional[dict[str, Any]] = None, legacy: bool = False
                         ) -> dict[str, Any]:
    """The versioned submit body: BOSS's flat guest_spec → the sandbox-service submit shape.

    v1.1 (default): no credential. ``handles`` are the guest's placeholders (``spec["handles"]``
    when not given), ``endpoints`` the loopback ports (the defaults), ``mcp`` the gateway URLs.
    ``legacy=True`` builds a v1.0 body for the frozen lane — model creds in ``engine_env``, the
    git token in ``git_token`` — and logs that it is deprecated."""
    engine = str(spec.get("engine") or "openai").lower()
    tid = task_id or spec.get("task_id") or "crun_unknown"
    body = _common(spec, tenant_id, tid, engine)
    if legacy:
        log.warning("%s: sandbox contract v%s is deprecated (P8b S9); task %s is built on the "
                    "frozen legacy lane, which carries credentials into the guest",
                    CONTRACT_DEPRECATED, LEGACY_CONTRACT_VERSION, tid)
        env = engine_env_for(engine, {k: spec[f] for k, f in _ENGINE_ENV_MAP if spec.get(f)})
        return {"contract_version": LEGACY_CONTRACT_VERSION, **body,
                "git_token": spec.get("token") or "", "engine_env": env}
    given = dict(handles if handles is not None else (spec.get("handles") or {}))
    return {"contract_version": CONTRACT_VERSION, **body,
            "handles": {k: str(given[k]) for k in HANDLE_PREFIXES if given.get(k)},
            "endpoints": {**DEFAULT_ENDPOINTS, **(endpoints or {})},
            "mcp": {"servers": dict((mcp or spec.get("mcp") or {}).get("servers") or {})}}


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


def validate_submit_request(body: dict[str, Any], *, strict_secrets: bool = True,
                            allow_legacy: Optional[bool] = None) -> list[str]:
    """Return a list of contract violations in a submit body (empty = valid). For tests + a Go
    conformance check (Go's ``ValidateSubmitWith`` has the same reasons and wording).

    A v1.1 body is refused when it carries a raw credential field, and — when ``strict_secrets``
    (the host's mode; the default) — any secret-shaped value. A v1.0 body is accepted only when
    ``allow_legacy`` (default: the legacy lane's switch) and never on a strict host."""
    problems: list[str] = []
    version = body.get("contract_version")
    if version == LEGACY_CONTRACT_VERSION:
        legacy = legacy_lane_enabled() if allow_legacy is None else allow_legacy
        if not legacy:
            problems.append(f"{CONTRACT_LEGACY_REFUSED}: contract_version {LEGACY_CONTRACT_VERSION} "
                            f"is the frozen legacy lane; send {CONTRACT_VERSION}")
        elif strict_secrets:
            problems.append(f"{CONTRACT_LEGACY_REFUSED}: contract_version {LEGACY_CONTRACT_VERSION} "
                            "carries credentials and is refused by a strict_secrets host")
        else:
            log.warning("%s: a contract v%s submit was accepted on the legacy lane",
                        CONTRACT_DEPRECATED, LEGACY_CONTRACT_VERSION)
            if not isinstance(body.get("engine_env"), dict):
                problems.append("engine_env must be an object")
            problems += engine_env_problems(body.get("engine"), body.get("engine_env"))
    elif version != CONTRACT_VERSION:
        problems.append(f"contract_version must be {CONTRACT_VERSION}")
    else:
        problems += secret_problems(body, strict=strict_secrets)
        if not isinstance(body.get("handles"), dict):
            problems.append("handles must be an object")
        endpoints = body.get("endpoints")
        if not isinstance(endpoints, dict):
            problems.append("endpoints must be an object")
        else:
            for name in sorted(endpoints):
                if not _ENDPOINT_RE.match(str(endpoints[name])):
                    problems.append(f"endpoints.{name} must be a guest-loopback 71xx URL")
        if body.get("mcp") is not None and not isinstance(body.get("mcp"), dict):
            problems.append("mcp must be an object")
    if not body.get("task_id"):
        problems.append("task_id is required")
    repo = body.get("repo") or {}
    if not repo.get("owner") or not repo.get("name"):
        problems.append("repo.owner and repo.name are required")
    return problems


def ensure_engine_env_valid(body: dict[str, Any]) -> None:
    """Raise EngineEnvRefused, by name, before a v1.0 body the guest cannot honour is sent."""
    problems = engine_env_problems(body.get("engine"), body.get("engine_env"))
    if problems:
        reason = (ENGINE_ENV_UNKNOWN if problems[0].startswith(ENGINE_ENV_UNKNOWN + ":")
                  else ENGINE_ENV_NOT_FOR_ENGINE)
        raise EngineEnvRefused(reason, problems)
