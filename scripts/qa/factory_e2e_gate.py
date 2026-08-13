#!/usr/bin/env python3
"""Factory E2E gate — Community GA software-factory surface check.

Two modes:

  --dry-run (default when BOSS_API_KEY missing)
    Structural validation of factory / code-task endpoint contracts.
    No network. Exit 0 if structure is coherent.

  live (credentials present)
    1. health
    2. GET /v1/factory/wip
    3. GET /v1/code-tasks?project_id=…
    4. Optional POST /v1/code-tasks (only with --create-code-task and full env)
    5. GET /v1/software-factory/proposals/queue (soft if 404)

Exit codes:
  0  all required checks passed
  1  one or more required checks failed
  2  usage / configuration error

---------------------------------------------------------------------------
Required environment for FULL LIVE run (optional create path)
---------------------------------------------------------------------------

  BOSS_API_BASE or BOSS_API_URL   API base URL
  BOSS_API_KEY                    API key (never printed / never committed)
  PROJECT_ID or BOSS_PROJECT_ID   project scope (default boss-dogfood)

Optional for --create-code-task (queues a real governed run — spends budget):

  BOSS_FACTORY_OWNER or GITHUB_OWNER   GitHub owner for the code task
  BOSS_FACTORY_REPO or GITHUB_REPO     GitHub repo for the code task
  BOSS_FACTORY_BASE_BRANCH             base branch (default main)
  BOSS_FACTORY_ALLOW_CREATE=1          hard confirm create is intentional

Without create flags, live mode is read-only (list endpoints only).

Examples:
  python3 scripts/qa/factory_e2e_gate.py --dry-run
  BOSS_API_KEY=… make qa-factory-e2e
  BOSS_API_KEY=… BOSS_FACTORY_ALLOW_CREATE=1 \\
    BOSS_FACTORY_OWNER=acme BOSS_FACTORY_REPO=app \\
    python3 scripts/qa/factory_e2e_gate.py --create-code-task
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

GATE_NAME = "factory_e2e_gate"
GATE_VERSION = "1.0.0"
DEFAULT_API_URL = "https://boss-api.local.sean.network"
DEFAULT_PROJECT_ID = "boss-dogfood"
DEFAULT_TIMEOUT_SECONDS = 30.0
COMMAND_PREFIX = "[factory-e2e-gate]"

# Endpoint contracts validated in dry-run and probed live.
STRUCTURAL_ENDPOINTS: list[dict[str, Any]] = [
    {
        "name": "health",
        "method": "GET",
        "path": "/health",
        "auth": False,
        "required": True,
    },
    {
        "name": "factory.wip",
        "method": "GET",
        "path": "/v1/factory/wip?project_id={project_id}&wip_limit=5",
        "auth": True,
        "required": True,
    },
    {
        "name": "code_tasks.list",
        "method": "GET",
        "path": "/v1/code-tasks?project_id={project_id}&limit=20",
        "auth": True,
        "required": True,
    },
    {
        "name": "code_tasks.create",
        "method": "POST",
        "path": "/v1/code-tasks",
        "auth": True,
        "required": False,  # only required when --create-code-task
        "body_keys": [
            "project_id",
            "title",
            "instructions",
            "owner",
            "repo",
            "base_branch",
        ],
    },
    {
        "name": "factory.proposals_queue",
        "method": "GET",
        "path": "/v1/software-factory/proposals/queue?project_id={project_id}",
        "auth": True,
        "required": False,  # soft — older deploys may 404
    },
    {
        "name": "factory.merge_check",
        "method": "POST",
        "path": "/v1/factory/merge-check",
        "auth": True,
        "required": False,
        "body_keys": ["project_id"],
        "note": "structural only unless --live-merge-check",
    },
]


# ---------------------------------------------------------------------------
# Pure helpers
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


def item_count(payload: Any) -> int | None:
    if isinstance(payload, list):
        return len(payload)
    if not isinstance(payload, dict):
        return None
    for key in ("items", "runs", "code_tasks", "proposals", "wip", "tasks"):
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
    return None


def extract_run_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("run_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def code_task_create_body(
    project_id: str,
    *,
    owner: str,
    repo: str,
    base_branch: str = "main",
    run_tag: str | None = None,
) -> dict[str, Any]:
    tag = run_tag or uuid.uuid4().hex[:8]
    return {
        "project_id": project_id,
        "title": f"{COMMAND_PREFIX} structural probe {tag}",
        "instructions": (
            f"{COMMAND_PREFIX} dry structural create for project {project_id}. "
            "No production changes. Prefer a no-op README touch if work proceeds."
        ),
        "acceptance_criteria": ["Gate probe completes or is cancelled without side effects"],
        "test_commands": [],
        "owner": owner,
        "repo": repo,
        "base_branch": base_branch,
        "profile": "lean",
        "open_pr": False,
    }


def resolve_factory_repo() -> tuple[str | None, str | None, str]:
    owner = (
        os.environ.get("BOSS_FACTORY_OWNER", "").strip()
        or os.environ.get("GITHUB_OWNER", "").strip()
        or None
    )
    repo = (
        os.environ.get("BOSS_FACTORY_REPO", "").strip()
        or os.environ.get("GITHUB_REPO", "").strip()
        or None
    )
    base = (
        os.environ.get("BOSS_FACTORY_BASE_BRANCH", "").strip()
        or "main"
    )
    return owner, repo, base


def allow_create_from_env() -> bool:
    return os.environ.get("BOSS_FACTORY_ALLOW_CREATE", "").strip() in (
        "1",
        "true",
        "TRUE",
        "yes",
        "YES",
    )


def validate_dry_run_structure(
    *,
    api_base: str = DEFAULT_API_URL,
    project_id: str = DEFAULT_PROJECT_ID,
) -> list[CheckResult]:
    """Structure-only validation of factory endpoint contracts — no network."""
    checks: list[CheckResult] = []

    names = [e["name"] for e in STRUCTURAL_ENDPOINTS]
    required_present = {"health", "factory.wip", "code_tasks.list", "code_tasks.create"}
    missing = required_present - set(names)
    checks.append(
        CheckResult(
            name="structure.endpoint_catalog",
            ok=not missing,
            detail=f"catalog={names}" if not missing else f"missing {sorted(missing)}",
            evidence={"names": names},
        )
    )

    for endpoint in STRUCTURAL_ENDPOINTS:
        path = endpoint["path"].format(project_id=project_id)
        url = join_url(api_base, path)
        method = endpoint["method"]
        ok = method in ("GET", "POST", "PUT", "PATCH", "DELETE") and url.startswith(
            ("http://", "https://")
        )
        checks.append(
            CheckResult(
                name=f"structure.endpoint.{endpoint['name']}",
                ok=ok,
                required=bool(endpoint.get("required", True)),
                detail=f"{method} {url}",
                evidence={
                    "auth": endpoint.get("auth"),
                    "body_keys": endpoint.get("body_keys"),
                },
            )
        )

    # create body shape
    body = code_task_create_body(
        project_id, owner="example-org", repo="example-repo", run_tag="dry"
    )
    need = {"project_id", "title", "instructions", "owner", "repo", "base_branch"}
    missing_keys = need - set(body)
    checks.append(
        CheckResult(
            name="structure.code_task_body",
            ok=not missing_keys and COMMAND_PREFIX in body["title"],
            detail="ok" if not missing_keys else f"missing {sorted(missing_keys)}",
            evidence={"keys": sorted(body.keys())},
        )
    )

    # helpers
    checks.append(
        CheckResult(
            name="structure.helpers",
            ok=(
                is_2xx(201)
                and not is_2xx(500)
                and extract_run_id({"run_id": "r1", "status": "queued"}) == "r1"
            ),
            detail="is_2xx + extract_run_id",
        )
    )

    # document live env requirements in evidence for operators reading dry-run report
    checks.append(
        CheckResult(
            name="structure.live_env_docs",
            ok=True,
            required=False,
            detail=(
                "full live create needs BOSS_API_BASE, BOSS_API_KEY, PROJECT_ID, "
                "BOSS_FACTORY_OWNER, BOSS_FACTORY_REPO, BOSS_FACTORY_ALLOW_CREATE=1"
            ),
            evidence={
                "required_read_only": [
                    "BOSS_API_BASE|BOSS_API_URL",
                    "BOSS_API_KEY",
                    "PROJECT_ID|BOSS_PROJECT_ID",
                ],
                "required_create": [
                    "BOSS_FACTORY_OWNER|GITHUB_OWNER",
                    "BOSS_FACTORY_REPO|GITHUB_REPO",
                    "BOSS_FACTORY_ALLOW_CREATE=1",
                    "--create-code-task",
                ],
            },
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
    return {
        "gate": GATE_NAME,
        "version": GATE_VERSION,
        "mode": mode,
        "api_url": api_url,
        "project_id": project_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "summary": summarize_report(checks),
        "checks": [c.to_dict() for c in checks],
        "meta": meta or {},
        "env_for_full_live": {
            "required": [
                "BOSS_API_BASE or BOSS_API_URL",
                "BOSS_API_KEY",
                "PROJECT_ID or BOSS_PROJECT_ID",
            ],
            "optional_create": [
                "BOSS_FACTORY_OWNER or GITHUB_OWNER",
                "BOSS_FACTORY_REPO or GITHUB_REPO",
                "BOSS_FACTORY_BASE_BRANCH (default main)",
                "BOSS_FACTORY_ALLOW_CREATE=1",
                "CLI flag --create-code-task",
            ],
        },
    }


def gate_exit_code(report: dict[str, Any]) -> int:
    summary = report.get("summary") or {}
    return 0 if summary.get("passed") else 1


def default_output_path() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for base in (Path("artifacts") / "qa", Path("/tmp") / "boss-qa" / "artifacts", Path("/tmp")):
        try:
            base.mkdir(parents=True, exist_ok=True)
            return base / f"factory-e2e-gate-{stamp}.json"
        except OSError:
            continue
    return Path(f"/tmp/factory-e2e-gate-{stamp}.json")


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
    create_code_task: bool = False,
    owner: str | None = None,
    repo: str | None = None,
    base_branch: str = "main",
) -> list[CheckResult]:
    checks: list[CheckResult] = []

    def record(result: CheckResult) -> None:
        checks.append(result)

    # 1. health
    try:
        status, payload, duration_ms = client.request("GET", "/health", auth=False)
        ok = is_2xx(status)
        detail = f"status={status}"
        if isinstance(payload, dict):
            detail = f"status={status} health={payload.get('status')!r}"
            if payload.get("database_connected") is False:
                ok = False
                detail += " database_connected=false"
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

    # 2. factory WIP
    try:
        path = f"/v1/factory/wip?{urlencode({'project_id': project_id, 'wip_limit': 5})}"
        status, payload, duration_ms = client.request("GET", path, auth=True)
        ok = is_2xx(status)
        count = item_count(payload)
        record(
            CheckResult(
                name="factory.wip",
                ok=ok,
                detail=f"status={status} count={count}" if ok else f"factory/wip expected 2xx, got {status}",
                status_code=status,
                duration_ms=duration_ms,
                evidence={"count": count},
            )
        )
    except RuntimeError as error:
        record(CheckResult(name="factory.wip", ok=False, detail=str(error)))

    # 3. code-tasks list
    try:
        path = f"/v1/code-tasks?{urlencode({'project_id': project_id, 'limit': 20})}"
        status, payload, duration_ms = client.request("GET", path, auth=True)
        ok = is_2xx(status)
        count = item_count(payload)
        record(
            CheckResult(
                name="code_tasks.list",
                ok=ok,
                detail=f"status={status} count={count}" if ok else f"code-tasks list expected 2xx, got {status}",
                status_code=status,
                duration_ms=duration_ms,
                evidence={"count": count},
            )
        )
    except RuntimeError as error:
        record(CheckResult(name="code_tasks.list", ok=False, detail=str(error)))

    # 4. optional create
    if create_code_task:
        if not owner or not repo:
            record(
                CheckResult(
                    name="code_tasks.create",
                    ok=False,
                    required=True,
                    detail=(
                        "create requested but owner/repo missing "
                        "(set BOSS_FACTORY_OWNER + BOSS_FACTORY_REPO)"
                    ),
                )
            )
        elif not allow_create_from_env():
            record(
                CheckResult(
                    name="code_tasks.create",
                    ok=False,
                    required=True,
                    detail=(
                        "create requested but BOSS_FACTORY_ALLOW_CREATE is not set to 1 "
                        "(refusing to queue budgeted work without explicit allow)"
                    ),
                )
            )
        else:
            try:
                body = code_task_create_body(
                    project_id,
                    owner=owner,
                    repo=repo,
                    base_branch=base_branch,
                )
                status, payload, duration_ms = client.request(
                    "POST",
                    "/v1/code-tasks",
                    body=body,
                    auth=True,
                )
                ok = is_2xx(status)
                run_id = extract_run_id(payload) if ok else None
                record(
                    CheckResult(
                        name="code_tasks.create",
                        ok=ok,
                        required=True,
                        detail=(
                            f"status={status} run_id={run_id}"
                            if ok
                            else f"code-tasks create expected 2xx, got {status}"
                        ),
                        status_code=status,
                        duration_ms=duration_ms,
                        evidence={"run_id": run_id, "owner": owner, "repo": repo},
                    )
                )
            except RuntimeError as error:
                record(
                    CheckResult(
                        name="code_tasks.create",
                        ok=False,
                        required=True,
                        detail=str(error),
                    )
                )
    else:
        record(
            CheckResult(
                name="code_tasks.create",
                ok=True,
                required=False,
                detail="skipped (pass --create-code-task + BOSS_FACTORY_ALLOW_CREATE=1 for live create)",
            )
        )

    # 5. proposals queue (soft)
    try:
        path = (
            f"/v1/software-factory/proposals/queue?"
            f"{urlencode({'project_id': project_id})}"
        )
        status, payload, duration_ms = client.request("GET", path, auth=True)
        ok = is_2xx(status)
        # 404 is soft — surface may not be mounted on all editions
        if status == 404:
            record(
                CheckResult(
                    name="factory.proposals_queue",
                    ok=True,
                    required=False,
                    detail="404 — proposals queue not mounted (soft ok)",
                    status_code=status,
                    duration_ms=duration_ms,
                )
            )
        else:
            count = item_count(payload)
            record(
                CheckResult(
                    name="factory.proposals_queue",
                    ok=ok,
                    required=False,
                    detail=f"status={status} count={count}",
                    status_code=status,
                    duration_ms=duration_ms,
                    evidence={"count": count},
                )
            )
    except RuntimeError as error:
        record(
            CheckResult(
                name="factory.proposals_queue",
                ok=False,
                required=False,
                detail=str(error),
            )
        )

    return checks


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Factory E2E gate: factory WIP + code-tasks (+ optional create).",
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
        help="BOSS API key (env: BOSS_API_KEY).",
    )
    parser.add_argument(
        "--project-id",
        default=None,
        help="Project id (env: PROJECT_ID or BOSS_PROJECT_ID).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Structure check only — no network. Also used when API key is missing.",
    )
    parser.add_argument(
        "--create-code-task",
        action="store_true",
        help=(
            "Optionally POST /v1/code-tasks (requires BOSS_FACTORY_ALLOW_CREATE=1 "
            "and owner/repo env)."
        ),
    )
    parser.add_argument(
        "--owner",
        default=None,
        help="GitHub owner override for create (env: BOSS_FACTORY_OWNER).",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="GitHub repo override for create (env: BOSS_FACTORY_REPO).",
    )
    parser.add_argument(
        "--base-branch",
        default=None,
        help="Base branch for create (env: BOSS_FACTORY_BASE_BRANCH, default main).",
    )
    parser.add_argument(
        "--output",
        default=os.environ.get("BOSS_FACTORY_GATE_OUTPUT"),
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
    env_owner, env_repo, env_base = resolve_factory_repo()
    owner = (args.owner or env_owner or "").strip() or None
    repo = (args.repo or env_repo or "").strip() or None
    base_branch = (args.base_branch or env_base or "main").strip() or "main"

    # Auto dry-run when no key and not explicitly live-attempting
    force_dry = args.dry_run or not api_key

    started = datetime.now(timezone.utc)
    started_mono = time.monotonic()

    if force_dry:
        if args.create_code_task and not args.dry_run and not api_key:
            print(
                "error: --create-code-task requires BOSS_API_KEY. "
                "Use --dry-run for structure-only checks.",
                file=sys.stderr,
            )
            return 2
        checks = validate_dry_run_structure(api_base=api_base, project_id=project_id)
        mode = "dry-run"
        if not api_key and not args.dry_run:
            # Implicit dry-run: make it obvious
            print(
                f"[{GATE_NAME}] no BOSS_API_KEY — running structural dry-run "
                "(pass key for live read-only probes).",
                file=sys.stderr,
            )
    else:
        client = BossApiClient(
            api_base,
            api_key,
            timeout_seconds=args.timeout_seconds,
            secrets_to_redact=[api_key] if api_key else [],
        )
        try:
            checks = run_gate(
                client,
                project_id=project_id,
                create_code_task=bool(args.create_code_task),
                owner=owner,
                repo=repo,
                base_branch=base_branch,
            )
        except Exception as error:  # noqa: BLE001
            checks = [
                CheckResult(
                    name="gate.crash",
                    ok=False,
                    detail=redact_secrets(str(error), [api_key] if api_key else []),
                )
            ]
        mode = "live" + ("+create" if args.create_code_task else "+readonly")

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
        meta={
            "create_requested": bool(args.create_code_task),
            "owner_set": bool(owner),
            "repo_set": bool(repo),
            "allow_create_env": allow_create_from_env(),
        },
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
