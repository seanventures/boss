#!/usr/bin/env python3
"""Community system gate — the whole no-spend QA stack as one command.

Compose (never spends; never prints BOSS_API_KEY):

  1. scripts/qa/harness.py --dry-run
     then, if BOSS_API_KEY is set: harness catalog (canary-mode=catalog)
  2. scripts/qa/live_dogfood_gate.py when key set
  3. scripts/qa/workflow_e2e_gate.py (dry-run without key, live with key)
  4. scripts/qa/factory_e2e_gate.py --dry-run
     (factory/wip + code-tasks, quality-loop exports, scorecard v1.1 ui/ux;
     never a budgeted code.task POST)
     live factory follows when key set; --create-fixture only if
     GET /v1/factory/wip advertises fixture_supported
  5. Optional preview fixture POST /v1/previews/fixture + GET .../web/
     when QA_PREVIEW_FIXTURE=1 (and a key is present)

Children run as subprocesses. Secrets are redacted from stdout/stderr/JSON.
Writes the combined report to --output.

Exit
  0 all required child steps passed (or dry-run structure ok)
  1 one or more required steps failed
  2 usage error
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.qa.lib import (  # noqa: E402
    DEFAULT_API_URL,
    DEFAULT_PROJECT_ID,
    BossClient,
    CheckResult,
    env_flag,
    is_2xx,
    redact_secrets,
)

GATE_NAME = "system_gate"
GATE_VERSION = "1.0.0"
DEFAULT_CHILD_TIMEOUT_SECONDS = 180.0
DEFAULT_ACTOR = "executive_console"

SCRIPT_HARNESS = ROOT / "scripts" / "qa" / "harness.py"
SCRIPT_LIVE = ROOT / "scripts" / "qa" / "live_dogfood_gate.py"
SCRIPT_WORKFLOW = ROOT / "scripts" / "qa" / "workflow_e2e_gate.py"
SCRIPT_FACTORY = ROOT / "scripts" / "qa" / "factory_e2e_gate.py"

CHILD_SCRIPTS: dict[str, Path] = {
    "harness.py": SCRIPT_HARNESS,
    "live_dogfood_gate.py": SCRIPT_LIVE,
    "workflow_e2e_gate.py": SCRIPT_WORKFLOW,
    "factory_e2e_gate.py": SCRIPT_FACTORY,
}

# Always-on structure children, in compose order (no key / --dry-run).
DRY_RUN_STEP_IDS = (
    "harness.dry_run",
    "workflow.dry_run",
    "factory.dry_run",
)

LIVE_STEP_IDS = (
    "harness.dry_run",
    "harness.catalog",
    "live_dogfood",
    "workflow.live",
    "factory.dry_run",
    "factory.live",
)

ENV_FOR_FULL_LIVE = {
    "required": [
        "BOSS_API_BASE or BOSS_API_URL",
        "BOSS_API_KEY",
        "PROJECT_ID or BOSS_PROJECT_ID",
    ],
    "optional": [
        "QA_PREVIEW_FIXTURE=1  (POST+GET /v1/previews/{id}/web/)",
        "BOSS_SYSTEM_GATE_OUTPUT  (combined JSON report)",
        "CREATE_FIXTURE is decided by fixture_supported (never spends)",
    ],
    "never": [
        "BOSS_QA_ALLOW_CANARIES / canary-mode=launch",
        "BOSS_FACTORY_ALLOW_CREATE / --create-code-task",
    ],
}


@dataclass
class StepResult:
    id: str
    ok: bool
    required: bool = True
    skipped: bool = False
    detail: str = ""
    exit_code: int | None = None
    duration_ms: int = 0
    script: str | None = None
    argv: list[str] = field(default_factory=list)
    output: str | None = None
    stdout: str = ""
    stderr: str = ""
    summary: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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


def secrets_from_env(extra: str | None = None) -> list[str]:
    found = [
        extra,
        os.environ.get("BOSS_API_KEY"),
        os.environ.get("NEXT_PUBLIC_BOSS_API_KEY"),
    ]
    return [s for s in found if s]


def redact_obj(value: Any, secrets: list[str]) -> Any:
    """Recursively redact secrets from a JSON-able object."""
    if isinstance(value, str):
        return redact_secrets(value, secrets)
    if isinstance(value, dict):
        return {str(k): redact_obj(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_obj(v, secrets) for v in value]
    return value


def default_output_path() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for base in (Path("artifacts") / "qa", Path("/tmp") / "boss-qa" / "artifacts", Path("/tmp")):
        try:
            base.mkdir(parents=True, exist_ok=True)
            return base / f"system-gate-{stamp}.json"
        except OSError:
            continue
    return Path(f"/tmp/system-gate-{stamp}.json")


def child_output_path(parent: Path, step_id: str) -> Path:
    return parent.with_name(f"{parent.stem}.{step_id}{parent.suffix}")


def wants_preview_fixture() -> bool:
    return env_flag("QA_PREVIEW_FIXTURE")


def compose_steps(
    *,
    has_api_key: bool,
    dry_run: bool,
    fixture_supported: bool | None,
    preview_fixture: bool,
) -> list[dict[str, Any]]:
    """Return the ordered step plan. Pure — no I/O, no network."""
    live = bool(has_api_key) and not dry_run
    steps: list[dict[str, Any]] = [
        {
            "id": "harness.dry_run",
            "script": "harness.py",
            "argv": ["--dry-run"],
            "required": True,
            "kind": "subprocess",
            "when": "always",
        }
    ]
    if live:
        steps.append(
            {
                "id": "harness.catalog",
                "script": "harness.py",
                "argv": ["--canary-mode", "catalog"],
                "required": True,
                "kind": "subprocess",
                "when": "api_key",
            }
        )
        steps.append(
            {
                "id": "live_dogfood",
                "script": "live_dogfood_gate.py",
                "argv": [],
                "required": True,
                "kind": "subprocess",
                "when": "api_key",
            }
        )
        steps.append(
            {
                "id": "workflow.live",
                "script": "workflow_e2e_gate.py",
                "argv": [],
                "required": True,
                "kind": "subprocess",
                "when": "api_key",
            }
        )
    else:
        steps.append(
            {
                "id": "workflow.dry_run",
                "script": "workflow_e2e_gate.py",
                "argv": ["--dry-run"],
                "required": True,
                "kind": "subprocess",
                "when": "always",
            }
        )
    steps.append(
        {
            "id": "factory.dry_run",
            "script": "factory_e2e_gate.py",
            "argv": ["--dry-run"],
            "required": True,
            "kind": "subprocess",
            "when": "always",
        }
    )
    if live:
        use_fixture = fixture_supported is True
        factory_argv = ["--create-fixture"] if use_fixture else []
        steps.append(
            {
                "id": "factory.live",
                "script": "factory_e2e_gate.py",
                "argv": factory_argv,
                "required": True,
                "kind": "subprocess",
                "when": "api_key",
                "create_fixture": use_fixture,
            }
        )
    if preview_fixture:
        skip_preview = dry_run or not has_api_key
        reason = None
        if dry_run:
            reason = "dry-run does not POST preview fixture"
        elif not has_api_key:
            reason = "QA_PREVIEW_FIXTURE=1 requires BOSS_API_KEY"
        steps.append(
            {
                "id": "preview.fixture",
                "script": None,
                "argv": [],
                "required": False,
                "kind": "http",
                "when": "QA_PREVIEW_FIXTURE=1",
                "skipped": skip_preview,
                "skip_reason": reason,
            }
        )
    return steps


def step_is_no_spend(step: dict[str, Any]) -> bool:
    argv = [str(a) for a in (step.get("argv") or [])]
    if "--create-code-task" in argv:
        return False
    if "launch" in argv:
        return False
    return True


def validate_dry_run_structure(
    *,
    api_base: str = DEFAULT_API_URL,
    project_id: str = DEFAULT_PROJECT_ID,
) -> list[CheckResult]:
    """Structure-only validation of the composed stack — no network."""
    checks: list[CheckResult] = []

    missing = [name for name, path in CHILD_SCRIPTS.items() if not path.is_file()]
    checks.append(
        CheckResult(
            name="structure.scripts_exist",
            ok=not missing,
            detail="all child scripts present" if not missing else f"missing {missing}",
            evidence={"scripts": sorted(CHILD_SCRIPTS)},
        )
    )

    dry_steps = compose_steps(
        has_api_key=False,
        dry_run=True,
        fixture_supported=None,
        preview_fixture=False,
    )
    dry_ids = [s["id"] for s in dry_steps]
    checks.append(
        CheckResult(
            name="structure.dry_run_order",
            ok=dry_ids == list(DRY_RUN_STEP_IDS),
            detail=f"dry_run_ids={dry_ids}",
            evidence={"ids": dry_ids, "expected": list(DRY_RUN_STEP_IDS)},
        )
    )

    live_steps = compose_steps(
        has_api_key=True,
        dry_run=False,
        fixture_supported=False,
        preview_fixture=False,
    )
    live_ids = [s["id"] for s in live_steps]
    checks.append(
        CheckResult(
            name="structure.live_order",
            ok=live_ids == list(LIVE_STEP_IDS),
            detail=f"live_ids={live_ids}",
            evidence={"ids": live_ids, "expected": list(LIVE_STEP_IDS)},
        )
    )

    with_fixture = compose_steps(
        has_api_key=True,
        dry_run=False,
        fixture_supported=True,
        preview_fixture=False,
    )
    without_fixture = compose_steps(
        has_api_key=True,
        dry_run=False,
        fixture_supported=False,
        preview_fixture=False,
    )
    with_argv = next(s["argv"] for s in with_fixture if s["id"] == "factory.live")
    without_argv = next(s["argv"] for s in without_fixture if s["id"] == "factory.live")
    fixture_ok = "--create-fixture" in with_argv and "--create-fixture" not in without_argv
    checks.append(
        CheckResult(
            name="structure.factory_fixture_gated",
            ok=fixture_ok,
            detail=(
                "CREATE_FIXTURE only when fixture_supported"
                if fixture_ok
                else f"with={with_argv} without={without_argv}"
            ),
            evidence={"with_fixture": with_argv, "without_fixture": without_argv},
        )
    )

    spendy = [s["id"] for s in live_steps + with_fixture + dry_steps if not step_is_no_spend(s)]
    checks.append(
        CheckResult(
            name="structure.no_spend_flags",
            ok=not spendy,
            detail="no launch / --create-code-task" if not spendy else f"spendy {spendy}",
            evidence={"spendy": spendy},
        )
    )

    preview_steps = compose_steps(
        has_api_key=True,
        dry_run=False,
        fixture_supported=False,
        preview_fixture=True,
    )
    preview_ids = [s["id"] for s in preview_steps]
    no_preview_ids = [s["id"] for s in live_steps]
    checks.append(
        CheckResult(
            name="structure.preview_optional",
            ok=("preview.fixture" in preview_ids and "preview.fixture" not in no_preview_ids),
            detail="preview.fixture only when QA_PREVIEW_FIXTURE=1",
            evidence={"with_flag": preview_ids, "without_flag": no_preview_ids},
        )
    )

    url_ok = api_base.startswith(("http://", "https://")) and bool(project_id)
    checks.append(
        CheckResult(
            name="structure.targets",
            ok=url_ok,
            detail=f"api={api_base} project={project_id}",
        )
    )

    docs_blob = json.dumps(ENV_FOR_FULL_LIVE)
    checks.append(
        CheckResult(
            name="structure.live_env_docs",
            ok="BOSS_API_KEY" in docs_blob,
            required=False,
            detail="env_for_full_live documents BOSS_API_KEY",
        )
    )
    return checks


def summarize_steps(steps: list[StepResult]) -> dict[str, Any]:
    required = [s for s in steps if s.required]
    optional = [s for s in steps if not s.required]
    failed_required = [s for s in required if not s.ok and not s.skipped]
    failed_optional = [s for s in optional if not s.ok and not s.skipped]
    skipped = [s for s in steps if s.skipped]
    return {
        "total": len(steps),
        "required_total": len(required),
        "required_passed": len(required) - len(failed_required),
        "required_failed": len(failed_required),
        "optional_total": len(optional),
        "optional_passed": len(optional) - len(failed_optional) - sum(1 for s in optional if s.skipped),
        "optional_failed": len(failed_optional),
        "skipped_total": len(skipped),
        "skipped_names": [s.id for s in skipped],
        "failed_required_names": [s.id for s in failed_required],
        "failed_optional_names": [s.id for s in failed_optional],
        "passed": not failed_required,
        "ok": not failed_required,
    }


def gate_exit_code(report: dict[str, Any]) -> int:
    summary = report.get("summary") or {}
    return 0 if summary.get("passed") else 1


def child_summary(report: Any) -> dict[str, Any]:
    if not isinstance(report, dict):
        return {}
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    ok = summary.get("ok")
    if ok is None:
        ok = summary.get("passed")
    return {
        "gate": report.get("gate"),
        "mode": report.get("mode"),
        "ok": ok,
        "failed": summary.get("failed") or summary.get("failed_required_names") or [],
    }


def build_child_argv(
    step: dict[str, Any],
    *,
    api_url: str,
    project_id: str,
    output: Path,
) -> list[str]:
    script = step.get("script")
    argv: list[str] = []
    if script in {"harness.py", "live_dogfood_gate.py"}:
        argv.extend(["--api-url", api_url])
    else:
        argv.extend(["--api-base", api_url])
    argv.extend(["--project-id", project_id])
    argv.extend(str(a) for a in (step.get("argv") or []))
    if script != "harness.py":
        argv.append("--quiet")
    argv.extend(["--output", str(output)])
    return argv


def run_subprocess_step(
    step: dict[str, Any],
    *,
    api_url: str,
    project_id: str,
    output: Path,
    timeout: float,
    secrets: list[str],
) -> StepResult:
    script_name = step["script"]
    script_path = CHILD_SCRIPTS[script_name]
    argv = build_child_argv(step, api_url=api_url, project_id=project_id, output=output)
    cmd = [sys.executable, str(script_path), *argv]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(ROOT),
            env=os.environ.copy(),
            check=False,
        )
        exit_code = int(proc.returncode)
        stdout = redact_secrets(proc.stdout or "", secrets)
        stderr = redact_secrets(proc.stderr or "", secrets)
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        stdout = redact_secrets((exc.stdout or "") if isinstance(exc.stdout, str) else "", secrets)
        stderr = redact_secrets(
            ((exc.stderr or "") if isinstance(exc.stderr, str) else "") + f"\ntimeout after {timeout}s",
            secrets,
        )
        return StepResult(
            id=step["id"],
            ok=False,
            required=bool(step.get("required", True)),
            detail=f"timeout after {timeout}s",
            exit_code=None,
            duration_ms=duration_ms,
            script=script_name,
            argv=argv,
            output=str(output),
            stdout=stdout[-4000:],
            stderr=stderr[-4000:],
        )
    duration_ms = int((time.monotonic() - started) * 1000)
    child_report: Any = None
    if output.is_file():
        try:
            child_report = redact_obj(json.loads(output.read_text(encoding="utf-8")), secrets)
            output.write_text(json.dumps(child_report, indent=2) + "\n", encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            child_report = None
    summary = child_summary(child_report)
    ok = exit_code == 0
    detail = f"exit={exit_code}"
    if summary.get("gate"):
        detail = f"exit={exit_code} gate={summary.get('gate')} ok={summary.get('ok')}"
    return StepResult(
        id=step["id"],
        ok=ok,
        required=bool(step.get("required", True)),
        detail=detail,
        exit_code=exit_code,
        duration_ms=duration_ms,
        script=script_name,
        argv=argv,
        output=str(output),
        stdout=stdout[-4000:],
        stderr=stderr[-4000:],
        summary=summary,
    )


def probe_fixture_supported(
    *,
    api_url: str,
    api_key: str,
    project_id: str,
    timeout: float,
) -> tuple[bool, str]:
    """Fail-closed: only True when the live WIP payload advertises the flag."""
    client = BossClient(api_url, api_key, project_id=project_id, timeout=timeout)
    try:
        status, payload, _ms = client.request(
            "GET",
            "/v1/factory/wip",
            query={"project_id": project_id, "wip_limit": 5},
        )
    except RuntimeError as exc:
        return False, redact_secrets(f"probe failed: {exc}", [api_key])
    advertised = isinstance(payload, dict) and payload.get("fixture_supported") is True
    return advertised, f"wip status={status} fixture_supported={advertised}"


def run_preview_fixture(
    *,
    api_url: str,
    api_key: str,
    project_id: str,
    actor: str,
    timeout: float,
    secrets: list[str],
) -> StepResult:
    client = BossClient(api_url, api_key, project_id=project_id, actor=actor, timeout=timeout)
    started = time.monotonic()
    evidence: dict[str, Any] = {}
    try:
        status, payload, post_ms = client.request(
            "POST",
            "/v1/previews/fixture",
            body={"project_id": project_id, "actor": actor, "ttl_minutes": 15},
        )
    except RuntimeError as exc:
        return StepResult(
            id="preview.fixture",
            ok=False,
            required=False,
            detail=redact_secrets(str(exc), secrets),
            duration_ms=int((time.monotonic() - started) * 1000),
            script=None,
        )
    preview_id = None
    if isinstance(payload, dict):
        preview_id = payload.get("id") or payload.get("preview_id")
        evidence["post_status"] = payload.get("status")
        evidence["mode"] = payload.get("mode")
        evidence["fixture"] = payload.get("fixture")
        evidence["simulated"] = payload.get("simulated")
    evidence["preview_id"] = preview_id
    evidence["post_http"] = status
    evidence["post_ms"] = post_ms
    if not is_2xx(status) or not preview_id:
        return StepResult(
            id="preview.fixture",
            ok=False,
            required=False,
            detail=f"POST /v1/previews/fixture → {status}",
            duration_ms=int((time.monotonic() - started) * 1000),
            evidence=evidence,
        )
    try:
        view_status, view_payload, view_ms = client.request(
            "GET",
            f"/v1/previews/{preview_id}/web/",
        )
    except RuntimeError as exc:
        return StepResult(
            id="preview.fixture",
            ok=False,
            required=False,
            detail=redact_secrets(f"GET /web/ failed: {exc}", secrets),
            duration_ms=int((time.monotonic() - started) * 1000),
            evidence=evidence,
        )
    html = ""
    if isinstance(view_payload, dict):
        html = str(view_payload.get("raw") or "")
    elif isinstance(view_payload, str):
        html = view_payload
    evidence["view_http"] = view_status
    evidence["view_ms"] = view_ms
    evidence["html_prefix"] = html[:200]
    ok = is_2xx(view_status)
    return StepResult(
        id="preview.fixture",
        ok=ok,
        required=False,
        detail=f"POST {status} preview_id={preview_id}; GET /web/ → {view_status}",
        duration_ms=int((time.monotonic() - started) * 1000),
        evidence=evidence,
    )


def build_report(
    *,
    api_url: str,
    project_id: str,
    steps: list[StepResult],
    planned: list[dict[str, Any]],
    started_at: str,
    finished_at: str,
    duration_ms: int,
    mode: str,
    structure_checks: list[CheckResult] | None = None,
    fixture_supported: bool | None = None,
    fixture_probe: str | None = None,
    preview_fixture: bool = False,
    has_api_key: bool = False,
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
        "has_api_key": bool(has_api_key),
        "preview_fixture_requested": bool(preview_fixture),
        "fixture_supported": fixture_supported,
        "fixture_probe": fixture_probe,
        "planned_steps": planned,
        "steps": [s.to_dict() for s in steps],
        "structure": [c.to_dict() for c in (structure_checks or [])],
        "summary": summarize_steps(steps),
        "env_for_full_live": ENV_FOR_FULL_LIVE,
        "meta": meta or {},
    }


def write_report(report: dict[str, Any], output: Path, secrets: list[str]) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = redact_obj(report, secrets)
    text = redact_secrets(json.dumps(payload, indent=2) + "\n", secrets)
    output.write_text(text, encoding="utf-8")
    try:
        output.chmod(0o600)
    except OSError:
        pass
    return output


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Community system gate: whole no-spend QA stack.",
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
        help="BOSS API key (env: BOSS_API_KEY). Never printed or written.",
    )
    parser.add_argument(
        "--project-id",
        default=None,
        help="Project id (env: PROJECT_ID or BOSS_PROJECT_ID).",
    )
    parser.add_argument("--actor", default=os.environ.get("BOSS_ACTOR") or DEFAULT_ACTOR)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_CHILD_TIMEOUT_SECONDS,
        help="Per-child subprocess / HTTP timeout.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Structure + child --dry-run only. No live HTTP.",
    )
    parser.add_argument(
        "--preview-fixture",
        action="store_true",
        help="Also POST+GET the no-spend preview fixture (or env QA_PREVIEW_FIXTURE=1).",
    )
    parser.add_argument(
        "--output",
        default=os.environ.get("BOSS_SYSTEM_GATE_OUTPUT"),
        help="Combined JSON report path.",
    )
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def _print_steps(steps: list[StepResult], quiet: bool) -> None:
    if quiet:
        return
    for step in steps:
        if step.skipped:
            flag = "SKIP"
        elif step.ok:
            flag = "PASS"
        else:
            flag = "FAIL"
        req = "required" if step.required else "optional"
        code = f" [{step.exit_code}]" if step.exit_code is not None else ""
        print(f"  {flag}  {step.id} ({req}){code}: {step.detail}")


def run_system_gate(
    *,
    api_url: str,
    api_key: str | None,
    project_id: str,
    actor: str,
    dry_run: bool,
    preview_fixture: bool,
    timeout: float,
    output: Path,
    quiet: bool,
) -> dict[str, Any]:
    secrets = secrets_from_env(api_key)
    started = datetime.now(timezone.utc)
    started_mono = time.monotonic()

    implicit_dry = dry_run or not api_key
    mode = "dry-run" if implicit_dry else "live"

    structure_checks = validate_dry_run_structure(api_base=api_url, project_id=project_id)
    structure_failed = [c for c in structure_checks if c.required and not c.ok]

    fixture_supported: bool | None = None
    fixture_probe: str | None = None
    if not implicit_dry and api_key:
        fixture_supported, fixture_probe = probe_fixture_supported(
            api_url=api_url,
            api_key=api_key,
            project_id=project_id,
            timeout=timeout,
        )

    planned = compose_steps(
        has_api_key=bool(api_key),
        dry_run=implicit_dry,
        fixture_supported=fixture_supported,
        preview_fixture=preview_fixture,
    )

    steps: list[StepResult] = []
    if structure_failed:
        steps.append(
            StepResult(
                id="structure",
                ok=False,
                required=True,
                detail="structure checks failed: " + ", ".join(c.name for c in structure_failed),
                evidence={"failed": [c.name for c in structure_failed]},
            )
        )
    else:
        for plan in planned:
            if plan.get("kind") == "http":
                if plan.get("skipped"):
                    steps.append(
                        StepResult(
                            id=plan["id"],
                            ok=True,
                            required=False,
                            skipped=True,
                            detail=f"SKIP: {plan.get('skip_reason')}",
                        )
                    )
                    continue
                if not api_key:
                    steps.append(
                        StepResult(
                            id=plan["id"],
                            ok=True,
                            required=False,
                            skipped=True,
                            detail="SKIP: no BOSS_API_KEY",
                        )
                    )
                    continue
                steps.append(
                    run_preview_fixture(
                        api_url=api_url,
                        api_key=api_key,
                        project_id=project_id,
                        actor=actor,
                        timeout=timeout,
                        secrets=secrets,
                    )
                )
                continue
            child_out = child_output_path(output, plan["id"])
            steps.append(
                run_subprocess_step(
                    plan,
                    api_url=api_url,
                    project_id=project_id,
                    output=child_out,
                    timeout=timeout,
                    secrets=secrets,
                )
            )

    finished = datetime.now(timezone.utc)
    duration_ms = int((time.monotonic() - started_mono) * 1000)
    report = build_report(
        api_url=api_url,
        project_id=project_id,
        steps=steps,
        planned=planned,
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        duration_ms=duration_ms,
        mode=mode,
        structure_checks=structure_checks,
        fixture_supported=fixture_supported,
        fixture_probe=fixture_probe,
        preview_fixture=preview_fixture,
        has_api_key=bool(api_key),
        meta={"actor": actor, "implicit_dry": implicit_dry},
    )
    write_report(report, output, secrets)

    summary = report["summary"]
    overall = "PASS" if summary["passed"] else "FAIL"
    print(
        f"[{GATE_NAME}] mode={mode} overall={overall} "
        f"required={summary['required_passed']}/{summary['required_total']} "
        f"duration_ms={duration_ms}"
    )
    _print_steps(steps, quiet)
    print(f"report: {output}")
    if not summary["passed"]:
        print(f"failed required: {summary['failed_required_names']}", file=sys.stderr)
    return report


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    api_url = resolve_api_base(args.api_base)
    project_id = resolve_project_id(args.project_id)
    api_key = resolve_api_key(args.api_key)
    preview = bool(args.preview_fixture) or wants_preview_fixture()
    output = Path(args.output) if args.output else default_output_path()

    if not args.dry_run and not api_key:
        print(
            f"[{GATE_NAME}] no BOSS_API_KEY — running structural dry-run "
            "(pass key for live no-spend children).",
            file=sys.stderr,
        )

    try:
        report = run_system_gate(
            api_url=api_url,
            api_key=api_key,
            project_id=project_id,
            actor=args.actor,
            dry_run=bool(args.dry_run),
            preview_fixture=preview,
            timeout=float(args.timeout_seconds),
            output=output,
            quiet=bool(args.quiet),
        )
    except Exception as error:  # noqa: BLE001 — always emit a report
        secrets = secrets_from_env(api_key)
        detail = redact_secrets(str(error), secrets)
        print(f"error: {detail}", file=sys.stderr)
        crash = {
            "gate": GATE_NAME,
            "version": GATE_VERSION,
            "mode": "error",
            "summary": {"passed": False, "ok": False, "failed_required_names": ["gate.crash"]},
            "error": detail,
        }
        try:
            write_report(crash, output, secrets)
        except OSError:
            pass
        return 1
    return gate_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
