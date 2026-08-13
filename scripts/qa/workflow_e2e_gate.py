#!/usr/bin/env python3
"""Workflow E2E gate — Community GA automated vertical-slice check.

Proves the CEO-command control-plane path end to end:

  1. health
  2. command propose  (creates durable task)
  3. list tasks
  4. budget status OR budget quote  (whichever succeeds first)
  5. jobs list

Exit codes:
  0  all critical steps returned 2xx
  1  one or more critical steps failed
  2  usage / configuration error

Environment (live mode):
  BOSS_API_BASE or BOSS_API_URL   API base (default https://boss-api.local.sean.network)
  BOSS_API_KEY                    required for authenticated steps (never printed)
  PROJECT_ID or BOSS_PROJECT_ID   project scope (default boss-dogfood)

Dry-run (no network):
  python3 scripts/qa/workflow_e2e_gate.py --dry-run
  # Validates step definitions, URL construction, payload shapes; exit 0 on structure OK.

Makefile:
  make qa-workflow-e2e
  make qa-workflow-e2e DRY_RUN=1
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

GATE_NAME = "workflow_e2e_gate"
GATE_VERSION = "1.0.0"
DEFAULT_API_URL = "https://boss-api.local.sean.network"
DEFAULT_PROJECT_ID = "boss-dogfood"
DEFAULT_ACTOR = "executive_console"
DEFAULT_TIMEOUT_SECONDS = 30.0
COMMAND_PREFIX = "[workflow-e2e-gate]"

# Ordered critical steps. Each entry is (name, method, path_template, body_builder|None).
# path_template may include {project_id}. body_builder(project_id, run_id) -> dict|None.
CRITICAL_STEP_NAMES = (
    "health",
    "command.propose",
    "tasks.list",
    "budget.status_or_quote",
    "jobs.list",
)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested / dry-run)
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
    if not text:
        return text
    redacted = text
    for secret in secrets or []:
        if secret and secret in redacted:
            redacted = redacted.replace(secret, "***")
    if "X-Boss-Api-Key" in redacted:
        redacted = redacted.replace("X-Boss-Api-Key", "X-Boss-Api-Key(redacted)")
    return redacted


def join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def resolve_api_base(cli_value: str | None = None) -> str:
    """Prefer BOSS_API_BASE, then BOSS_API_URL, then default."""
    return (
        (cli_value or "").strip()
        or os.environ.get("BOSS_API_BASE", "").strip()
        or os.environ.get("BOSS_API_URL", "").strip()
        or DEFAULT_API_URL
    )


def resolve_project_id(cli_value: str | None = None) -> str:
    return (
        (cli_value or "").strip()
        or os.environ.get("PROJECT_ID", "").strip()
        or os.environ.get("BOSS_PROJECT_ID", "").strip()
        or os.environ.get("BOSS_DEFAULT_PROJECT_ID", "").strip()
        or DEFAULT_PROJECT_ID
    )


def resolve_api_key(cli_value: str | None = None) -> str | None:
    key = (
        (cli_value or "").strip()
        or os.environ.get("BOSS_API_KEY", "").strip()
        or os.environ.get("NEXT_PUBLIC_BOSS_API_KEY", "").strip()
    )
    return key or None


def is_2xx(status_code: int | None) -> bool:
    return status_code is not None and 200 <= status_code < 300


def extract_task_id(payload: Any) -> str | None:
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


def item_count(payload: Any) -> int | None:
    if isinstance(payload, list):
        return len(payload)
    if not isinstance(payload, dict):
        return None
    for key in ("items", "tasks", "jobs", "decisions", "deadletters"):
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
    return None


def build_gate_command(project_id: str, run_id: str) -> str:
    return (
        f"{COMMAND_PREFIX} probe {run_id}: durable intake for project {project_id}. "
        "Do not spend model budget; do not open PRs. Status report is enough."
    )


def command_propose_body(project_id: str, run_id: str, actor: str = DEFAULT_ACTOR) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "command": build_gate_command(project_id, run_id),
        "actor": actor,
        "constraints": {
            "source": GATE_NAME,
            "run_id": run_id,
            "no_spend": True,
        },
    }


def budget_quote_body(project_id: str) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "category": "model_api",
        "amount_usd_micros": 50_000,
        "reason": f"{COMMAND_PREFIX} quote-only smoke",
    }


def expected_step_defs() -> list[dict[str, Any]]:
    """Canonical step definitions for dry-run structure validation."""
    return [
        {
            "name": "health",
            "method": "GET",
            "path": "/health",
            "auth": False,
            "required": True,
        },
        {
            "name": "command.propose",
            "method": "POST",
            "path": "/v1/commands/propose",
            "auth": True,
            "required": True,
            "body_keys": ["project_id", "command", "actor", "constraints"],
        },
        {
            "name": "tasks.list",
            "method": "GET",
            "path": "/v1/tasks?project_id={project_id}&limit=50",
            "auth": True,
            "required": True,
        },
        {
            "name": "budget.status_or_quote",
            "method": "GET|POST",
            "paths": [
                "/v1/budget/status?scope_type=project&scope_id={project_id}",
                "/v1/budget/quote",
            ],
            "auth": True,
            "required": True,
            "note": "status preferred; quote is fallback if status is non-2xx",
        },
        {
            "name": "jobs.list",
            "method": "GET",
            "path": "/v1/jobs?project_id={project_id}&limit=50",
            "auth": True,
            "required": True,
        },
    ]


def validate_dry_run_structure(
    *,
    api_base: str = DEFAULT_API_URL,
    project_id: str = DEFAULT_PROJECT_ID,
) -> list[CheckResult]:
    """Structure-only validation — no network. Exit 0 when all defs are coherent."""
    checks: list[CheckResult] = []
    steps = expected_step_defs()
    names = [s["name"] for s in steps]

    # 1. critical names present and ordered
    if names == list(CRITICAL_STEP_NAMES):
        checks.append(
            CheckResult(
                name="structure.step_order",
                ok=True,
                detail=f"critical steps: {', '.join(names)}",
            )
        )
    else:
        checks.append(
            CheckResult(
                name="structure.step_order",
                ok=False,
                detail=f"expected {CRITICAL_STEP_NAMES}, got {tuple(names)}",
            )
        )

    # 2. URL join for each path template
    run_id = "dryrun0001"
    sample_paths: list[str] = []
    for step in steps:
        paths = step.get("paths") or [step.get("path", "")]
        for path in paths:
            filled = path.format(project_id=project_id)
            url = join_url(api_base, filled)
            sample_paths.append(url)
            if not url.startswith(("http://", "https://")):
                checks.append(
                    CheckResult(
                        name=f"structure.url.{step['name']}",
                        ok=False,
                        detail=f"bad url: {url}",
                    )
                )
                break
        else:
            checks.append(
                CheckResult(
                    name=f"structure.url.{step['name']}",
                    ok=True,
                    detail=f"paths ok for {step['name']}",
                    evidence={"urls": [join_url(api_base, p.format(project_id=project_id)) for p in paths]},
                )
            )

    # 3. command body shape
    body = command_propose_body(project_id, run_id)
    required_keys = {"project_id", "command", "actor", "constraints"}
    missing = required_keys - set(body)
    if missing:
        checks.append(
            CheckResult(
                name="structure.command_body",
                ok=False,
                detail=f"missing keys: {sorted(missing)}",
            )
        )
    elif COMMAND_PREFIX not in body["command"]:
        checks.append(
            CheckResult(
                name="structure.command_body",
                ok=False,
                detail="command text missing gate prefix",
            )
        )
    else:
        checks.append(
            CheckResult(
                name="structure.command_body",
                ok=True,
                detail="command propose body has required keys + gate prefix",
                evidence={"keys": sorted(body.keys())},
            )
        )

    # 4. budget quote body shape
    qbody = budget_quote_body(project_id)
    q_required = {"project_id", "category", "amount_usd_micros", "reason"}
    q_missing = q_required - set(qbody)
    checks.append(
        CheckResult(
            name="structure.budget_quote_body",
            ok=not q_missing,
            detail="ok" if not q_missing else f"missing: {sorted(q_missing)}",
            evidence={"keys": sorted(qbody.keys())},
        )
    )

    # 5. self-check helpers
    checks.append(
        CheckResult(
            name="structure.helpers",
            ok=is_2xx(200) and not is_2xx(404) and extract_task_id({"task_id": "t1"}) == "t1",
            detail="is_2xx + extract_task_id helpers",
        )
    )

    return checks


def summarize_report(checks: list[CheckResult]) -> dict[str, Any]:
    required = [c for c in checks if c.required]
    failed_required = [c for c in required if not c.ok]
    optional = [c for c in checks if not c.required]
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
    mode: str,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = summarize_report(checks)
    return {
        "gate": GATE_NAME,
        "version": GATE_VERSION,
        "mode": mode,
        "api_url": api_url,
        "project_id": project_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "summary": summary,
        "checks": [c.to_dict() for c in checks],
        "critical_steps": list(CRITICAL_STEP_NAMES),
        "meta": meta or {},
    }


def gate_exit_code(report: dict[str, Any]) -> int:
    summary = report.get("summary") or {}
    return 0 if summary.get("passed") else 1


def default_output_path() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for base in (Path("artifacts") / "qa", Path("/tmp") / "boss-qa" / "artifacts", Path("/tmp")):
        try:
            base.mkdir(parents=True, exist_ok=True)
            return base / f"workflow-e2e-gate-{stamp}.json"
        except OSError:
            continue
    return Path(f"/tmp/workflow-e2e-gate-{stamp}.json")


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
    ) -> tuple[int, Any, int]:
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
        if not raw.strip():
            return status, None, duration_ms
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return status, raw[:1000], duration_ms
        return status, payload, duration_ms


# ---------------------------------------------------------------------------
# Live gate
# ---------------------------------------------------------------------------


def run_gate(
    client: BossApiClient,
    *,
    project_id: str,
    actor: str = DEFAULT_ACTOR,
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    run_id = uuid.uuid4().hex[:12]

    def record(result: CheckResult) -> None:
        checks.append(result)

    # 1. health
    try:
        status, payload, duration_ms = client.request("GET", "/health", auth=False)
        ok = is_2xx(status)
        detail = f"status={status}"
        if isinstance(payload, dict):
            detail = f"status={status} health={payload.get('status')!r} db={payload.get('database_connected')}"
            if payload.get("database_connected") is False:
                ok = False
                detail += " (database_connected=false)"
        record(
            CheckResult(
                name="health",
                ok=ok,
                detail=detail,
                status_code=status,
                duration_ms=duration_ms,
            )
        )
    except RuntimeError as error:
        record(CheckResult(name="health", ok=False, detail=str(error)))

    # 2. command propose
    task_id: str | None = None
    try:
        status, payload, duration_ms = client.request(
            "POST",
            "/v1/commands/propose",
            body=command_propose_body(project_id, run_id, actor=actor),
            auth=True,
        )
        ok = is_2xx(status)
        task_id = extract_task_id(payload) if ok else None
        if ok and not task_id:
            # Accept 2xx even without task_id if body is structured — still log warning.
            detail = f"status={status} (no task_id in response)"
        else:
            detail = f"status={status} task_id={task_id}"
        if not ok:
            detail = f"command propose expected 2xx, got {status}"
        record(
            CheckResult(
                name="command.propose",
                ok=ok,
                detail=detail,
                status_code=status,
                duration_ms=duration_ms,
                evidence={"task_id": task_id, "run_id": run_id},
            )
        )
    except RuntimeError as error:
        record(CheckResult(name="command.propose", ok=False, detail=str(error)))

    # 3. list tasks
    try:
        path = f"/v1/tasks?{urlencode({'project_id': project_id, 'limit': 50})}"
        status, payload, duration_ms = client.request("GET", path, auth=True)
        ok = is_2xx(status)
        count = item_count(payload)
        detail = f"status={status} count={count}"
        if ok and task_id and isinstance(payload, dict):
            items = payload.get("items") or payload.get("tasks") or []
            if isinstance(items, list):
                found = any(
                    isinstance(i, dict) and (i.get("id") == task_id or i.get("task_id") == task_id)
                    for i in items
                )
                detail += f" proposed_task_in_list={found}"
        record(
            CheckResult(
                name="tasks.list",
                ok=ok,
                detail=detail if ok else f"tasks list expected 2xx, got {status}",
                status_code=status,
                duration_ms=duration_ms,
                evidence={"count": count, "proposed_task_id": task_id},
            )
        )
    except RuntimeError as error:
        record(CheckResult(name="tasks.list", ok=False, detail=str(error)))

    # 4. budget status OR quote (either 2xx is success)
    budget_ok = False
    budget_detail = ""
    budget_status_code: int | None = None
    budget_duration = 0
    budget_evidence: dict[str, Any] = {}
    try:
        status_path = (
            f"/v1/budget/status?{urlencode({'scope_type': 'project', 'scope_id': project_id})}"
        )
        status, payload, duration_ms = client.request("GET", status_path, auth=True)
        budget_duration = duration_ms
        budget_status_code = status
        if is_2xx(status):
            budget_ok = True
            state = payload.get("budget_state") if isinstance(payload, dict) else None
            budget_detail = f"budget/status 2xx state={state!r}"
            budget_evidence = {
                "path": "status",
                "budget_state": state,
                "available_usd_micros": (
                    payload.get("available_usd_micros") if isinstance(payload, dict) else None
                ),
            }
        else:
            # Fallback: quote
            status2, payload2, duration2 = client.request(
                "POST",
                "/v1/budget/quote",
                body=budget_quote_body(project_id),
                auth=True,
            )
            budget_duration += duration2
            budget_status_code = status2
            if is_2xx(status2):
                budget_ok = True
                allowed = payload2.get("allowed") if isinstance(payload2, dict) else None
                budget_detail = f"budget/status={status}; quote 2xx allowed={allowed!r}"
                budget_evidence = {
                    "path": "quote",
                    "status_fallback_code": status,
                    "allowed": allowed,
                }
            else:
                budget_detail = (
                    f"budget/status={status} and budget/quote={status2} (need one 2xx)"
                )
                budget_evidence = {
                    "path": "both_failed",
                    "status_code": status,
                    "quote_code": status2,
                }
    except RuntimeError as error:
        budget_detail = str(error)
    record(
        CheckResult(
            name="budget.status_or_quote",
            ok=budget_ok,
            detail=budget_detail,
            status_code=budget_status_code,
            duration_ms=budget_duration,
            evidence=budget_evidence,
        )
    )

    # 5. jobs list
    try:
        path = f"/v1/jobs?{urlencode({'project_id': project_id, 'limit': 50})}"
        status, payload, duration_ms = client.request("GET", path, auth=True)
        ok = is_2xx(status)
        count = item_count(payload)
        record(
            CheckResult(
                name="jobs.list",
                ok=ok,
                detail=f"status={status} count={count}" if ok else f"jobs list expected 2xx, got {status}",
                status_code=status,
                duration_ms=duration_ms,
                evidence={"count": count},
            )
        )
    except RuntimeError as error:
        record(CheckResult(name="jobs.list", ok=False, detail=str(error)))

    return checks


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Workflow E2E gate: health → command → tasks → budget → jobs.",
    )
    parser.add_argument(
        "--api-base",
        "--api-url",
        dest="api_base",
        default=None,
        help="BOSS API base (env: BOSS_API_BASE or BOSS_API_URL).",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="BOSS API key (env: BOSS_API_KEY). Never written to the report.",
    )
    parser.add_argument(
        "--project-id",
        default=None,
        help="Project id (env: PROJECT_ID or BOSS_PROJECT_ID).",
    )
    parser.add_argument("--actor", default=DEFAULT_ACTOR)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Structure check only — no network calls.",
    )
    parser.add_argument(
        "--output",
        default=os.environ.get("BOSS_WORKFLOW_GATE_OUTPUT"),
        help="JSON report path.",
    )
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def _print_checks(checks: list[CheckResult], quiet: bool) -> None:
    if quiet:
        return
    for check in checks:
        flag = "PASS" if check.ok else "FAIL"
        req = "required" if check.required else "optional"
        code = f" [{check.status_code}]" if check.status_code is not None else ""
        print(f"  {flag}  {check.name} ({req}){code}: {check.detail}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    api_base = resolve_api_base(args.api_base)
    project_id = resolve_project_id(args.project_id)
    api_key = resolve_api_key(args.api_key)

    started = datetime.now(timezone.utc)
    started_mono = time.monotonic()

    if args.dry_run:
        checks = validate_dry_run_structure(api_base=api_base, project_id=project_id)
        mode = "dry-run"
    else:
        if not api_key:
            print(
                "error: BOSS_API_KEY (or --api-key) is required for live mode. "
                "Use --dry-run for structure-only checks.",
                file=sys.stderr,
            )
            return 2
        client = BossApiClient(
            api_base,
            api_key,
            timeout_seconds=args.timeout_seconds,
            secrets_to_redact=[api_key],
        )
        try:
            checks = run_gate(client, project_id=project_id, actor=args.actor)
        except Exception as error:  # noqa: BLE001
            checks = [
                CheckResult(
                    name="gate.crash",
                    ok=False,
                    detail=redact_secrets(str(error), [api_key]),
                )
            ]
        mode = "live"

    finished = datetime.now(timezone.utc)
    duration_ms = int((time.monotonic() - started_mono) * 1000)
    report = build_report(
        api_url=api_base,
        project_id=project_id,
        checks=checks,
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        duration_ms=duration_ms,
        mode=mode,
    )
    exit_code = gate_exit_code(report)

    out_path = Path(args.output) if args.output else default_output_path()
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        print(f"warning: could not write report: {error}", file=sys.stderr)
        out_path = Path("(unwritten)")

    summary = report["summary"]
    overall = "PASS" if summary["passed"] else "FAIL"
    print(
        f"[{GATE_NAME}] mode={mode} overall={overall} "
        f"required={summary['required_passed']}/{summary['required_total']} "
        f"duration_ms={duration_ms}"
    )
    _print_checks(checks, args.quiet)
    print(f"report: {out_path}")
    if not summary["passed"]:
        print(f"failed required: {summary['failed_required_names']}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
