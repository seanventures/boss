"""BOSS-97 parent seam — async Python ⇄ Go job contract.

Shared lifecycle used by future Go services (S1 sandbox, S2 gateway, S3
dispatcher): **enqueue → status → heartbeat → cancel**. Payload details for
S1 stay in ``sandbox_contract``; this module owns the versioned envelope,
fail-closed timeouts, JSON Schema, and receipt/dedupe keys.

The engine MUST run with no Go service present. ``service_available`` is
false when the URL is unset; ``GoJobClient`` then raises ``GoServiceUnset``
instead of hanging. Timeouts are fail-closed (never treated as success).

Not a Go binary. S1/S2/S3 implementations live on child tickets 98/101/102.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .sandbox_contract import (
    CANCELLED,
    CONTRACT_VERSION,
    QUEUED,
    build_submit_request,
    is_terminal,
    parse_status as parse_sandbox_status,
    parse_submit_response,
    release_path,
    status_path,
    submit_path,
    validate_submit_request,
)

SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "contracts" / "go_job_v1.schema.json"
)

DEFAULT_TIMEOUTS_S: dict[str, float] = {
    "enqueue": 10.0,
    "status": 5.0,
    "cancel": 5.0,
    "heartbeat": 5.0,
}

AUTH_HEADER = "X-Sandbox-Token"


class GoJobError(RuntimeError):
    """Fail-closed contract error. Never a silent success."""

    def __init__(self, message: str, *, code: str, op: str = ""):
        super().__init__(message)
        self.message = message
        self.code = code
        self.op = op


class GoServiceUnset(GoJobError):
    def __init__(self, message: str = "Go service URL is unset; engine must fall back"):
        super().__init__(message, code="go_service_unset")


class GoJobTimeout(GoJobError):
    def __init__(self, op: str, timeout_s: float):
        super().__init__(
            f"{op} timed out after {timeout_s}s (fail-closed)",
            code="go_job_timeout",
            op=op,
        )


def service_available(settings) -> bool:
    """True iff a sandbox/Go service URL is configured. Empty → engine stays local."""
    return bool((getattr(settings, "sandbox_agent_url", "") or "").strip())


def routes(job_id: str) -> dict[str, str]:
    """S1 HTTP routes. Do not collide with the engine's durable ``/v1/jobs`` queue."""
    sid = str(job_id or "").strip()
    return {
        "enqueue": submit_path(),
        "status": status_path(sid),
        "cancel": cancel_path(sid),
        "heartbeat": heartbeat_path(sid),
        "release": release_path(sid),
    }


def cancel_path(job_id: str) -> str:
    return f"/v1/sandbox/tasks/{job_id}/cancel"


def heartbeat_path(job_id: str) -> str:
    return f"/v1/sandbox/tasks/{job_id}/heartbeat"


def enqueue_dedupe_key(
    tenant_id: Optional[str], task_id: str, idempotency_key: Optional[str] = None
) -> str:
    tenant = tenant_id or "default"
    if idempotency_key:
        return f"go-job:enqueue:{tenant}:{task_id}:{idempotency_key}"
    return f"go-job:enqueue:{tenant}:{task_id}"


def enqueue_receipt(
    *,
    tenant_id: Optional[str],
    task_id: str,
    job_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> dict[str, str]:
    """Receipt fields for an enqueue (external effect). Caller persists; we do not spend."""
    return {
        "effect_kind": "go_job.enqueue",
        "dedupe_key": enqueue_dedupe_key(tenant_id, job_id or task_id, idempotency_key),
    }


def build_enqueue_request(
    spec: dict[str, Any],
    tenant_id: Optional[str],
    *,
    task_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    timeout_s: Optional[int] = None,
) -> dict[str, Any]:
    """Versioned enqueue body: S1 submit shape + job-lifecycle fields."""
    body = build_submit_request(spec, tenant_id, task_id=task_id)
    tid = str(body["task_id"])
    body["op"] = "enqueue"
    if idempotency_key:
        body["idempotency_key"] = str(idempotency_key)
    body["dedupe_key"] = enqueue_dedupe_key(tenant_id, tid, idempotency_key)
    if timeout_s is not None:
        body["timeout_s"] = int(timeout_s)
    if spec.get("heartbeat_interval_s"):
        body["heartbeat_interval_s"] = int(spec["heartbeat_interval_s"])
    else:
        body.setdefault("heartbeat_interval_s", 15)
    return body


def validate_enqueue_request(body: dict[str, Any]) -> list[str]:
    problems = validate_submit_request(body)
    if body.get("op") != "enqueue":
        problems.append("op must be enqueue")
    if not isinstance(body.get("dedupe_key"), str) or not body.get("dedupe_key"):
        problems.append("dedupe_key is required")
    return problems


def build_cancel_request(job_id: str, *, reason: str = "") -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "op": "cancel",
        "job_id": str(job_id),
        "reason": str(reason or ""),
    }


def validate_cancel_request(body: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if body.get("contract_version") != CONTRACT_VERSION:
        problems.append(f"contract_version must be {CONTRACT_VERSION}")
    if body.get("op") != "cancel":
        problems.append("op must be cancel")
    if not body.get("job_id"):
        problems.append("job_id is required")
    return problems


def build_heartbeat_request(job_id: str) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "op": "heartbeat",
        "job_id": str(job_id),
    }


def validate_heartbeat_request(body: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if body.get("contract_version") != CONTRACT_VERSION:
        problems.append(f"contract_version must be {CONTRACT_VERSION}")
    if body.get("op") != "heartbeat":
        problems.append("op must be heartbeat")
    if not body.get("job_id"):
        problems.append("job_id is required")
    return problems


def parse_enqueue_response(resp: Any, *, spec: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    job_id = parse_submit_response(resp)
    d = resp if isinstance(resp, dict) else {}
    status = str(d.get("status") or QUEUED).lower()
    tenant_id = None
    task_id = job_id or ""
    idempotency_key = None
    if isinstance(spec, dict):
        tenant_id = spec.get("tenant_id")
        task_id = str(spec.get("task_id") or task_id)
        idempotency_key = spec.get("idempotency_key")
    return {
        "job_id": job_id,
        "status": status,
        "receipt": enqueue_receipt(
            tenant_id=tenant_id,
            task_id=task_id or (job_id or "unknown"),
            job_id=job_id,
            idempotency_key=idempotency_key,
        ),
    }


def parse_status(resp: Any) -> dict[str, Any]:
    parsed = parse_sandbox_status(resp)
    d = resp if isinstance(resp, dict) else {}
    status = str(d.get("status") or "").lower()
    if status == CANCELLED:
        parsed = {
            "status": CANCELLED,
            "phase": d.get("phase"),
            "terminal": True,
            "succeeded": False,
        }
    parsed["job_id"] = d.get("job_id") or d.get("sandbox_task_id")
    return parsed


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_against_schema(instance: Any, def_name: str) -> list[str]:
    """Stdlib JSON Schema check against ``$defs[def_name]``. No extra dependency."""
    schema = load_schema()
    defs = schema.get("$defs") or {}
    spec = defs.get(def_name)
    if spec is None:
        return [f"unknown schema def {def_name}"]
    return _validate(instance, spec, defs, "$")


def _resolve(spec: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    ref = spec.get("$ref")
    if not ref:
        return spec
    name = str(ref).rsplit("/", 1)[-1]
    return defs.get(name) or spec


def _validate(instance: Any, spec: dict[str, Any], defs: dict[str, Any], path: str) -> list[str]:
    spec = _resolve(spec, defs)
    problems: list[str] = []
    expected = spec.get("type")
    types = expected if isinstance(expected, list) else ([expected] if expected else [])

    def _type_ok(value: Any, name: str) -> bool:
        if name == "object":
            return isinstance(value, dict)
        if name == "array":
            return isinstance(value, list)
        if name == "string":
            return isinstance(value, str)
        if name == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if name == "boolean":
            return isinstance(value, bool)
        if name == "null":
            return value is None
        if name == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        return True

    if types and not any(_type_ok(instance, t) for t in types):
        return [f"{path}: expected {'|'.join(types)}"]

    if "const" in spec and instance != spec["const"]:
        problems.append(f"{path}: must be {spec['const']!r}")
    if "enum" in spec and instance not in spec["enum"]:
        problems.append(f"{path}: {instance!r} not in enum {spec['enum']}")

    if isinstance(instance, str) and "minLength" in spec and len(instance) < int(spec["minLength"]):
        problems.append(f"{path}: shorter than minLength {spec['minLength']}")

    if isinstance(instance, dict) and (not types or "object" in types):
        required = spec.get("required") or []
        for key in required:
            if key not in instance:
                problems.append(f"{path}.{key}: required")
        props = spec.get("properties") or {}
        additional = spec.get("additionalProperties", True)
        for key, value in instance.items():
            if key in props:
                problems.extend(_validate(value, props[key], defs, f"{path}.{key}"))
            elif additional is False:
                problems.append(f"{path}.{key}: unexpected")

    if isinstance(instance, int) and not isinstance(instance, bool) and "minimum" in spec:
        if instance < int(spec["minimum"]):
            problems.append(f"{path}: below minimum {spec['minimum']}")

    return problems


class GoJobClient:
    """HTTP client for the Go job contract. Fail-closed on unset URL and timeouts."""

    def __init__(
        self,
        settings,
        *,
        http_client=None,
        timeouts_s: Optional[dict[str, float]] = None,
    ):
        self.settings = settings
        self._http = http_client
        self.timeouts_s = dict(DEFAULT_TIMEOUTS_S)
        if timeouts_s:
            self.timeouts_s.update({k: float(v) for k, v in timeouts_s.items()})

    def _base(self) -> str:
        if not service_available(self.settings):
            raise GoServiceUnset()
        return str(self.settings.sandbox_agent_url).strip().rstrip("/")

    def _headers(self) -> dict[str, str]:
        token = getattr(self.settings, "sandbox_token", "") or ""
        return {AUTH_HEADER: token} if token else {}

    def _timeout(self, op: str) -> float:
        return float(self.timeouts_s.get(op) or DEFAULT_TIMEOUTS_S[op])

    def _call(self, method: str, op: str, url: str, *, json_body: Optional[dict] = None) -> Any:
        import httpx

        timeout = self._timeout(op)
        client = self._http
        owns = client is None
        if owns:
            client = httpx.Client(timeout=timeout)
        try:
            try:
                if method == "GET":
                    response = client.get(url, headers=self._headers(), timeout=timeout)
                else:
                    response = client.post(
                        url, json=json_body, headers=self._headers(), timeout=timeout
                    )
                response.raise_for_status()
                return response.json()
            except httpx.TimeoutException as exc:
                raise GoJobTimeout(op, timeout) from exc
            except httpx.HTTPError as exc:
                raise GoJobError(
                    f"{op} failed: {type(exc).__name__}: {exc}",
                    code="go_job_http_error",
                    op=op,
                ) from exc
        finally:
            if owns:
                close = getattr(client, "close", None)
                if callable(close):
                    close()

    def enqueue(self, spec: dict[str, Any], *, tenant_id: Optional[str] = None) -> dict[str, Any]:
        base = self._base()
        body = build_enqueue_request(
            spec,
            tenant_id or spec.get("tenant_id"),
            task_id=spec.get("task_id"),
            idempotency_key=spec.get("idempotency_key"),
            timeout_s=spec.get("timeout_s"),
        )
        problems = validate_enqueue_request(body)
        if problems:
            raise GoJobError(
                "; ".join(problems), code="go_job_contract_error", op="enqueue"
            )
        raw = self._call("POST", "enqueue", f"{base}{submit_path()}", json_body=body)
        parsed = parse_enqueue_response(raw, spec=body)
        if not parsed.get("job_id"):
            raise GoJobError(
                "enqueue response missing job_id (fail-closed)",
                code="go_job_contract_error",
                op="enqueue",
            )
        return parsed

    def status(self, job_id: str) -> dict[str, Any]:
        base = self._base()
        raw = self._call("GET", "status", f"{base}{status_path(job_id)}")
        parsed = parse_status(raw)
        parsed["job_id"] = parsed.get("job_id") or job_id
        return parsed

    def cancel(self, job_id: str, *, reason: str = "") -> dict[str, Any]:
        base = self._base()
        body = build_cancel_request(job_id, reason=reason)
        raw = self._call(
            "POST", "cancel", f"{base}{cancel_path(job_id)}", json_body=body
        )
        parsed = parse_status(raw)
        parsed["job_id"] = parsed.get("job_id") or job_id
        if parsed.get("status") != CANCELLED and is_terminal(parsed.get("status")):
            return parsed
        if parsed.get("status") != CANCELLED:
            # Host acknowledged cancel without a body — still fail-visible as cancelled.
            parsed["status"] = CANCELLED
            parsed["terminal"] = True
            parsed["succeeded"] = False
        return parsed

    def heartbeat(self, job_id: str) -> dict[str, Any]:
        base = self._base()
        body = build_heartbeat_request(job_id)
        raw = self._call(
            "POST", "heartbeat", f"{base}{heartbeat_path(job_id)}", json_body=body
        )
        parsed = parse_status(raw)
        parsed["job_id"] = parsed.get("job_id") or job_id
        d = raw if isinstance(raw, dict) else {}
        parsed["heartbeat_ok"] = bool(d.get("heartbeat_ok", True))
        return parsed
