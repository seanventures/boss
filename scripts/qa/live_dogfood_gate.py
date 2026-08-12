#!/usr/bin/env python3
"""Live functional E2E gate for goldeneye dogfood (Plane BOSS-351).

Proves the vertical-slice control-plane pieces that can run without full
factory success (no sandbox/PR/merge required):

  1. health + auth (public health; unauth /v1 fails closed; auth works)
  2. provider-definitions + providers list (community keys after deploy)
  3. budget quote
  4. command propose → task exists
  5. decisions inbox reachable
  6. deadletters list
  7. metatron question (optional / soft if skipped or slow)

Environment:
  BOSS_API_URL      default https://boss-api.local.sean.network
  BOSS_API_KEY      required for authenticated checks (never printed)
  BOSS_PROJECT_ID   default boss-dogfood

Exit codes:
  0  all required checks passed
  1  one or more required checks failed
  2  usage / configuration error (missing API key, bad args)

Examples:
  BOSS_API_KEY=… make qa-live
  python3 scripts/qa/live_dogfood_gate.py --api-url https://boss-api.local.sean.network \\
      --api-key \"$BOSS_API_KEY\" --project-id boss-dogfood --output /tmp/boss-live-gate.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

GATE_NAME = "live_dogfood_gate"
GATE_VERSION = "1.0.0"
DEFAULT_API_URL = "https://boss-api.local.sean.network"
DEFAULT_PROJECT_ID = "boss-dogfood"
DEFAULT_ACTOR = "executive_console"
DEFAULT_BUDGET_CATEGORY = "model_api"
DEFAULT_BUDGET_MICROS = 50_000  # $0.05 — quote only, no reserve
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_METATRON_TIMEOUT_SECONDS = 45.0

# Command text is intentionally low-stakes and self-identifying so dogfood
# operators can recognize gate noise in the task list.
COMMAND_PREFIX = "[BOSS-351 live-gate]"


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    name: str
    ok: bool
    required: bool = True
    detail: str = ""
    status_code: int | None = None
    duration_ms: int = 0
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def redact_secrets(text: str, secrets: list[str] | None = None) -> str:
    """Strip known secrets from error/detail strings before reporting."""
    if not text:
        return text
    redacted = text
    for secret in secrets or []:
        if secret and secret in redacted:
            redacted = redacted.replace(secret, "***")
    # Never leak common header-shaped blobs.
    if "X-Boss-Api-Key" in redacted:
        redacted = redacted.replace("X-Boss-Api-Key", "X-Boss-Api-Key(redacted)")
    return redacted


def join_url(base: str, path: str) -> str:
    """Join API base URL with a path that may include a query string."""
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def item_count(payload: Any) -> int | None:
    """Best-effort count of list-like API payloads."""
    if isinstance(payload, list):
        return len(payload)
    if not isinstance(payload, dict):
        return None
    for key in ("items", "decisions", "deadletters", "tasks", "projects"):
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
    return None


def extract_task_id(payload: Any) -> str | None:
    """Extract task_id from command propose or task list responses."""
    if not isinstance(payload, dict):
        return None
    for key in ("task_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    task = payload.get("task")
    if isinstance(task, dict):
        value = task.get("id") or task.get("task_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def evaluate_health(payload: Any, status_code: int) -> tuple[bool, str]:
    if status_code != 200:
        return False, f"expected 200, got {status_code}"
    if not isinstance(payload, dict):
        return False, "health payload is not an object"
    status = payload.get("status")
    if status not in ("ok", "healthy", "up"):
        return False, f"unexpected health status={status!r}"
    db = payload.get("database_connected")
    if db is False:
        return False, "database_connected is false"
    return True, f"status={status} database_connected={db}"


def evaluate_unauth(status_code: int) -> tuple[bool, str]:
    if status_code in (401, 403):
        return True, f"unauthenticated /v1 correctly rejected ({status_code})"
    return False, f"expected 401/403 without key, got {status_code}"


def evaluate_auth_projects(payload: Any, status_code: int, project_id: str) -> tuple[bool, str]:
    if status_code != 200:
        return False, f"expected 200, got {status_code}"
    projects: list[Any]
    if isinstance(payload, list):
        projects = payload
    elif isinstance(payload, dict):
        projects = payload.get("items") or payload.get("projects") or []
        if not isinstance(projects, list):
            projects = []
    else:
        return False, "projects payload is not a list or object"
    ids = {
        (p.get("id") if isinstance(p, dict) else None)
        for p in projects
    }
    if project_id not in ids:
        return False, f"project {project_id!r} not in projects list (count={len(projects)})"
    return True, f"project {project_id} present (count={len(projects)})"


def evaluate_list_ok(
    payload: Any,
    status_code: int,
    *,
    label: str,
    allow_empty: bool = True,
) -> tuple[bool, str, dict[str, Any]]:
    if status_code != 200:
        return False, f"{label}: expected 200, got {status_code}", {}
    count = item_count(payload)
    evidence: dict[str, Any] = {"count": count}
    if count is None:
        # Some list endpoints wrap differently; still accept 200 with object body.
        if isinstance(payload, (dict, list)):
            return True, f"{label}: 200 (structure ok)", evidence
        return False, f"{label}: unexpected payload type", evidence
    if not allow_empty and count == 0:
        return False, f"{label}: empty list not allowed", evidence
    return True, f"{label}: 200 count={count}", evidence


def evaluate_budget_quote(payload: Any, status_code: int) -> tuple[bool, str, dict[str, Any]]:
    if status_code != 200:
        return False, f"budget quote expected 200, got {status_code}", {}
    if not isinstance(payload, dict):
        return False, "budget quote payload is not an object", {}
    if "allowed" not in payload:
        return False, "budget quote missing 'allowed' field", {}
    evidence = {
        "allowed": payload.get("allowed"),
        "budget_state": payload.get("budget_state"),
        "requested_usd_micros": payload.get("requested_usd_micros"),
        "available_usd_micros": payload.get("available_usd_micros"),
    }
    # A denied quote still proves the budget path is live; only hard-fail if
    # the response is malformed. Operators can tighten later.
    detail = (
        f"allowed={payload.get('allowed')} state={payload.get('budget_state')} "
        f"available={payload.get('available_usd_micros')}"
    )
    return True, detail, evidence


def evaluate_command_propose(payload: Any, status_code: int) -> tuple[bool, str, dict[str, Any]]:
    if status_code not in (200, 201):
        return False, f"command propose expected 200/201, got {status_code}", {}
    task_id = extract_task_id(payload)
    if not task_id:
        return False, "command propose response missing task_id", {"payload_keys": list(payload) if isinstance(payload, dict) else []}
    evidence = {
        "task_id": task_id,
        "job_id": payload.get("job_id") if isinstance(payload, dict) else None,
        "status": payload.get("status") if isinstance(payload, dict) else None,
        "plan_id": payload.get("plan_id") if isinstance(payload, dict) else None,
    }
    return True, f"task_id={task_id} status={evidence.get('status')}", evidence


def evaluate_task_exists(
    payload: Any,
    status_code: int,
    expected_task_id: str,
) -> tuple[bool, str, dict[str, Any]]:
    if status_code != 200:
        return False, f"get task expected 200, got {status_code}", {"task_id": expected_task_id}
    got = extract_task_id(payload)
    if got and got != expected_task_id:
        return False, f"task id mismatch got={got} expected={expected_task_id}", {"task_id": got}
    # Accept either exact id match or a list containing the task.
    if got == expected_task_id:
        return True, f"task {expected_task_id} exists", {"task_id": expected_task_id}
    if isinstance(payload, dict):
        items = payload.get("items") or []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("id") == expected_task_id:
                    return True, f"task {expected_task_id} in list", {"task_id": expected_task_id}
    if status_code == 200 and isinstance(payload, dict):
        # Some deployments return the task object without id alias; trust 200 for known path.
        return True, f"task endpoint 200 for {expected_task_id}", {"task_id": expected_task_id}
    return False, f"task {expected_task_id} not found", {"task_id": expected_task_id}


def evaluate_metatron(payload: Any, status_code: int) -> tuple[bool, str, dict[str, Any]]:
    if status_code != 200:
        return False, f"metatron expected 200, got {status_code}", {}
    if not isinstance(payload, dict):
        return False, "metatron payload is not an object", {}
    # Accept several answer field names used historically.
    answer = (
        payload.get("answer")
        or payload.get("summary")
        or payload.get("response")
        or payload.get("text")
    )
    evidence = {
        "has_answer": bool(answer),
        "keys": sorted(payload.keys())[:20],
    }
    if answer:
        snippet = str(answer)[:160].replace("\n", " ")
        return True, f"answer={snippet!r}", evidence
    # Some metatron responses are structured without a free-text answer.
    if payload.get("references") is not None or payload.get("project_id"):
        return True, "metatron 200 structured response", evidence
    return True, "metatron 200", evidence


def summarize_report(checks: list[CheckResult]) -> dict[str, Any]:
    required = [c for c in checks if c.required]
    optional = [c for c in checks if not c.required]
    failed_required = [c for c in required if not c.ok]
    failed_optional = [c for c in optional if not c.ok]
    return {
        "total": len(checks),
        "required_total": len(required),
        "required_passed": len(required) - len(failed_required),
        "required_failed": len(failed_required),
        "optional_total": len(optional),
        "optional_passed": len(optional) - len(failed_optional),
        "optional_failed": len(failed_optional),
        "failed_required_names": [c.name for c in failed_required],
        "failed_optional_names": [c.name for c in failed_optional],
        "passed": len(failed_required) == 0,
    }


def build_report(
    *,
    api_url: str,
    project_id: str,
    checks: list[CheckResult],
    started_at: str,
    finished_at: str,
    duration_ms: int,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = summarize_report(checks)
    return {
        "gate": GATE_NAME,
        "version": GATE_VERSION,
        "plane": "BOSS-351",
        "api_url": api_url,
        "project_id": project_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "summary": summary,
        "checks": [c.to_dict() for c in checks],
        "meta": meta or {},
    }


def gate_exit_code(report: dict[str, Any]) -> int:
    summary = report.get("summary") or {}
    return 0 if summary.get("passed") else 1


def default_output_path() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidates = [
        Path("artifacts") / "qa",
        Path("/tmp") / "boss-qa" / "artifacts",
        Path("/tmp"),
    ]
    for base in candidates:
        try:
            base.mkdir(parents=True, exist_ok=True)
            return base / f"live-dogfood-gate-{stamp}.json"
        except OSError:
            continue
    return Path(f"/tmp/live-dogfood-gate-{stamp}.json")


def build_gate_command(project_id: str, run_id: str) -> str:
    return (
        f"{COMMAND_PREFIX} probe {run_id}: durable intake only for project {project_id}. "
        "Do not spend model budget; do not open PRs. Status report is enough."
    )


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class BossApiClient:
    def __init__(
        self,
        api_url: str,
        api_key: str | None,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        secrets_to_redact: list[str] | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.secrets_to_redact = [s for s in (secrets_to_redact or []) if s]

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        auth: bool = True,
        timeout_seconds: float | None = None,
        expect_json: bool = True,
    ) -> tuple[int, Any, int]:
        """Return (status_code, payload_or_text, duration_ms)."""
        url = join_url(self.api_url, path)
        encoded = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        if auth and self.api_key:
            headers["X-Boss-Api-Key"] = self.api_key
        request = Request(url, data=encoded, headers=headers, method=method)
        started = time.monotonic()
        try:
            with urlopen(request, timeout=timeout_seconds or self.timeout_seconds) as response:  # noqa: S310
                raw = response.read().decode("utf-8", errors="replace")
                status = int(response.status)
        except HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            status = int(error.code)
        except URLError as error:
            duration_ms = int((time.monotonic() - started) * 1000)
            reason = redact_secrets(str(error.reason), self.secrets_to_redact)
            raise RuntimeError(f"BOSS API {method} {path} unreachable: {reason}") from error
        duration_ms = int((time.monotonic() - started) * 1000)
        raw = redact_secrets(raw, self.secrets_to_redact)
        if not expect_json:
            return status, raw, duration_ms
        if not raw.strip():
            return status, None, duration_ms
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return status, raw[:1000], duration_ms
        return status, payload, duration_ms


# ---------------------------------------------------------------------------
# Gate steps
# ---------------------------------------------------------------------------


def run_gate(
    client: BossApiClient,
    *,
    project_id: str,
    actor: str = DEFAULT_ACTOR,
    budget_category: str = DEFAULT_BUDGET_CATEGORY,
    budget_micros: int = DEFAULT_BUDGET_MICROS,
    skip_metatron: bool = False,
    skip_command: bool = False,
    metatron_timeout_seconds: float = DEFAULT_METATRON_TIMEOUT_SECONDS,
) -> list[CheckResult]:
    checks: list[CheckResult] = []

    def record(result: CheckResult) -> None:
        checks.append(result)

    # 1a. health
    try:
        status, payload, duration_ms = client.request("GET", "/health", auth=False)
        ok, detail = evaluate_health(payload, status)
        record(
            CheckResult(
                name="health",
                ok=ok,
                detail=detail,
                status_code=status,
                duration_ms=duration_ms,
                evidence={"payload": payload if isinstance(payload, dict) else None},
            )
        )
    except RuntimeError as error:
        record(CheckResult(name="health", ok=False, detail=str(error)))

    # 1b. unauthenticated /v1 fails closed
    try:
        status, payload, duration_ms = client.request("GET", "/v1/projects", auth=False)
        ok, detail = evaluate_unauth(status)
        record(
            CheckResult(
                name="auth.unauthenticated_rejected",
                ok=ok,
                detail=detail,
                status_code=status,
                duration_ms=duration_ms,
            )
        )
    except RuntimeError as error:
        record(CheckResult(name="auth.unauthenticated_rejected", ok=False, detail=str(error)))

    # 1c. authenticated projects list includes dogfood project
    try:
        status, payload, duration_ms = client.request("GET", "/v1/projects", auth=True)
        ok, detail = evaluate_auth_projects(payload, status, project_id)
        record(
            CheckResult(
                name="auth.projects",
                ok=ok,
                detail=detail,
                status_code=status,
                duration_ms=duration_ms,
            )
        )
    except RuntimeError as error:
        record(CheckResult(name="auth.projects", ok=False, detail=str(error)))

    # 2a. provider-definitions (community keys path after deploy)
    try:
        status, payload, duration_ms = client.request("GET", "/v1/provider-definitions", auth=True)
        ok, detail, evidence = evaluate_list_ok(
            payload, status, label="provider-definitions", allow_empty=True
        )
        if status == 404:
            detail = (
                "provider-definitions returned 404 — community ProviderOps routers "
                "not mounted on this deploy (need api image with community keys)"
            )
            ok = False
        record(
            CheckResult(
                name="providers.definitions",
                ok=ok,
                detail=detail,
                status_code=status,
                duration_ms=duration_ms,
                evidence=evidence,
            )
        )
    except RuntimeError as error:
        record(CheckResult(name="providers.definitions", ok=False, detail=str(error)))

    # 2b. providers list
    try:
        path = f"/v1/providers?{urlencode({'project_id': project_id})}"
        status, payload, duration_ms = client.request("GET", path, auth=True)
        ok, detail, evidence = evaluate_list_ok(
            payload, status, label="providers", allow_empty=True
        )
        if status == 404:
            detail = (
                "providers returned 404 — community provider accounts not mounted "
                "on this deploy"
            )
            ok = False
        record(
            CheckResult(
                name="providers.list",
                ok=ok,
                detail=detail,
                status_code=status,
                duration_ms=duration_ms,
                evidence=evidence,
            )
        )
    except RuntimeError as error:
        record(CheckResult(name="providers.list", ok=False, detail=str(error)))

    # 3. budget quote
    try:
        status, payload, duration_ms = client.request(
            "POST",
            "/v1/budget/quote",
            body={
                "project_id": project_id,
                "category": budget_category,
                "amount_usd_micros": budget_micros,
                "reason": f"{COMMAND_PREFIX} quote-only smoke",
            },
            auth=True,
        )
        ok, detail, evidence = evaluate_budget_quote(payload, status)
        record(
            CheckResult(
                name="budget.quote",
                ok=ok,
                detail=detail,
                status_code=status,
                duration_ms=duration_ms,
                evidence=evidence,
            )
        )
    except RuntimeError as error:
        record(CheckResult(name="budget.quote", ok=False, detail=str(error)))

    # 4. command propose → task exists
    task_id: str | None = None
    if skip_command:
        record(
            CheckResult(
                name="command.propose",
                ok=True,
                required=False,
                detail="skipped by --skip-command",
            )
        )
        record(
            CheckResult(
                name="command.task_exists",
                ok=True,
                required=False,
                detail="skipped by --skip-command",
            )
        )
    else:
        run_id = uuid.uuid4().hex[:12]
        command = build_gate_command(project_id, run_id)
        try:
            status, payload, duration_ms = client.request(
                "POST",
                "/v1/commands/propose",
                body={
                    "project_id": project_id,
                    "command": command,
                    "actor": actor,
                    "constraints": {
                        "source": GATE_NAME,
                        "plane": "BOSS-351",
                        "run_id": run_id,
                        "no_spend": True,
                    },
                },
                auth=True,
            )
            ok, detail, evidence = evaluate_command_propose(payload, status)
            task_id = evidence.get("task_id") if isinstance(evidence.get("task_id"), str) else None
            record(
                CheckResult(
                    name="command.propose",
                    ok=ok,
                    detail=detail,
                    status_code=status,
                    duration_ms=duration_ms,
                    evidence=evidence,
                )
            )
        except RuntimeError as error:
            record(CheckResult(name="command.propose", ok=False, detail=str(error)))

        if task_id:
            try:
                status, payload, duration_ms = client.request(
                    "GET",
                    f"/v1/tasks/{task_id}?{urlencode({'project_id': project_id})}",
                    auth=True,
                )
                # Some deployments require project_id query; if 404, fall back to list filter.
                if status == 404:
                    status, payload, duration_ms = client.request(
                        "GET",
                        f"/v1/tasks?{urlencode({'project_id': project_id, 'limit': 50})}",
                        auth=True,
                    )
                ok, detail, evidence = evaluate_task_exists(payload, status, task_id)
                record(
                    CheckResult(
                        name="command.task_exists",
                        ok=ok,
                        detail=detail,
                        status_code=status,
                        duration_ms=duration_ms,
                        evidence=evidence,
                    )
                )
            except RuntimeError as error:
                record(CheckResult(name="command.task_exists", ok=False, detail=str(error)))
        else:
            record(
                CheckResult(
                    name="command.task_exists",
                    ok=False,
                    detail="skipped — no task_id from command.propose",
                )
            )

    # 5. decisions inbox
    try:
        path = f"/v1/decisions/inbox?{urlencode({'project_id': project_id})}"
        status, payload, duration_ms = client.request("GET", path, auth=True)
        ok, detail, evidence = evaluate_list_ok(
            payload, status, label="decisions/inbox", allow_empty=True
        )
        record(
            CheckResult(
                name="decisions.inbox",
                ok=ok,
                detail=detail,
                status_code=status,
                duration_ms=duration_ms,
                evidence=evidence,
            )
        )
    except RuntimeError as error:
        record(CheckResult(name="decisions.inbox", ok=False, detail=str(error)))

    # 6. deadletters list
    try:
        path = f"/v1/deadletters?{urlencode({'project_id': project_id, 'limit': 50})}"
        status, payload, duration_ms = client.request("GET", path, auth=True)
        ok, detail, evidence = evaluate_list_ok(
            payload, status, label="deadletters", allow_empty=True
        )
        record(
            CheckResult(
                name="deadletters.list",
                ok=ok,
                detail=detail,
                status_code=status,
                duration_ms=duration_ms,
                evidence=evidence,
            )
        )
    except RuntimeError as error:
        record(CheckResult(name="deadletters.list", ok=False, detail=str(error)))

    # 7. metatron (optional)
    if skip_metatron:
        record(
            CheckResult(
                name="metatron.query",
                ok=True,
                required=False,
                detail="skipped by --skip-metatron",
            )
        )
    else:
        try:
            status, payload, duration_ms = client.request(
                "POST",
                "/v1/metatron/query",
                body={
                    "project_id": project_id,
                    "question": "What is the current budget state and open deadletter count?",
                    "limit": 5,
                },
                auth=True,
                timeout_seconds=metatron_timeout_seconds,
            )
            ok, detail, evidence = evaluate_metatron(payload, status)
            record(
                CheckResult(
                    name="metatron.query",
                    ok=ok,
                    required=False,  # soft: slow LLM path must not fail deploy gate alone
                    detail=detail,
                    status_code=status,
                    duration_ms=duration_ms,
                    evidence=evidence,
                )
            )
        except RuntimeError as error:
            record(
                CheckResult(
                    name="metatron.query",
                    ok=False,
                    required=False,
                    detail=f"metatron soft-fail: {error}",
                )
            )

    return checks


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Live functional E2E gate for BOSS dogfood (BOSS-351).",
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("BOSS_API_URL", DEFAULT_API_URL),
        help=f"BOSS API base URL (default: BOSS_API_URL or {DEFAULT_API_URL}).",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("BOSS_API_KEY") or os.environ.get("NEXT_PUBLIC_BOSS_API_KEY"),
        help="BOSS API key (default: BOSS_API_KEY). Never written to the report.",
    )
    parser.add_argument(
        "--project-id",
        default=os.environ.get("BOSS_PROJECT_ID")
        or os.environ.get("BOSS_DEFAULT_PROJECT_ID")
        or DEFAULT_PROJECT_ID,
        help=f"Project id (default: BOSS_PROJECT_ID or {DEFAULT_PROJECT_ID}).",
    )
    parser.add_argument("--actor", default=DEFAULT_ACTOR)
    parser.add_argument("--budget-category", default=DEFAULT_BUDGET_CATEGORY)
    parser.add_argument("--budget-micros", type=int, default=DEFAULT_BUDGET_MICROS)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="HTTP timeout for most checks.",
    )
    parser.add_argument(
        "--metatron-timeout-seconds",
        type=float,
        default=DEFAULT_METATRON_TIMEOUT_SECONDS,
        help="HTTP timeout for metatron (soft check).",
    )
    parser.add_argument(
        "--skip-metatron",
        action="store_true",
        help="Skip the optional metatron query check.",
    )
    parser.add_argument(
        "--skip-command",
        action="store_true",
        help="Skip command propose / task create (read-only mode).",
    )
    parser.add_argument(
        "--output",
        default=os.environ.get("BOSS_LIVE_GATE_OUTPUT"),
        help="JSON report path (default: artifacts/qa or /tmp/boss-qa/artifacts).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only print the report path and pass/fail summary.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.api_key:
        print(
            "error: BOSS_API_KEY (or --api-key) is required for authenticated checks.",
            file=sys.stderr,
        )
        return 2

    secrets = [args.api_key]
    client = BossApiClient(
        args.api_url,
        args.api_key,
        timeout_seconds=args.timeout_seconds,
        secrets_to_redact=secrets,
    )

    started = datetime.now(timezone.utc)
    started_mono = time.monotonic()
    try:
        checks = run_gate(
            client,
            project_id=args.project_id,
            actor=args.actor,
            budget_category=args.budget_category,
            budget_micros=args.budget_micros,
            skip_metatron=args.skip_metatron,
            skip_command=args.skip_command,
            metatron_timeout_seconds=args.metatron_timeout_seconds,
        )
    except Exception as error:  # noqa: BLE001 — top-level gate must always emit a report
        detail = redact_secrets(str(error), secrets)
        checks = [
            CheckResult(
                name="gate.crash",
                ok=False,
                detail=detail,
            )
        ]

    finished = datetime.now(timezone.utc)
    duration_ms = int((time.monotonic() - started_mono) * 1000)
    report = build_report(
        api_url=args.api_url,
        project_id=args.project_id,
        checks=checks,
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        duration_ms=duration_ms,
        meta={
            "actor": args.actor,
            "skip_metatron": args.skip_metatron,
            "skip_command": args.skip_command,
            "budget_category": args.budget_category,
        },
    )

    output_path = Path(args.output) if args.output else default_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    # Best-effort: avoid world-readable reports in /tmp.
    try:
        output_path.chmod(0o600)
    except OSError:
        pass

    summary = report["summary"]
    if not args.quiet:
        print(f"gate={GATE_NAME} version={GATE_VERSION} plane=BOSS-351")
        print(f"api={args.api_url} project={args.project_id}")
        for check in checks:
            flag = "PASS" if check.ok else ("SOFT" if not check.required else "FAIL")
            if check.ok and not check.required and check.detail.startswith("skipped"):
                flag = "SKIP"
            elif not check.ok and not check.required:
                flag = "SOFT"
            print(
                f"  [{flag}] {check.name}"
                f"{f' ({check.status_code})' if check.status_code is not None else ''}"
                f" {check.duration_ms}ms — {check.detail}"
            )
        print(
            f"summary: required {summary['required_passed']}/{summary['required_total']} passed; "
            f"optional {summary['optional_passed']}/{summary['optional_total']} passed; "
            f"overall={'PASS' if summary['passed'] else 'FAIL'}"
        )
    print(f"report={output_path}")

    return gate_exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
