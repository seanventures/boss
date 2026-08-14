#!/usr/bin/env python3
"""Unified BOSS E2E QA harness.

Phases
  0 preflight     health + auth fail-closed + edition
  1 features      GET/safe-POST sweep of operator settings and features
  2 control-plane command → tasks → budget → jobs (no factory spend)
  3 canaries      BugBox, HookWatch, ApproveIt catalog + optional launch

Modes for canaries (never spend unless explicitly allowed):
  catalog   GET /v1/qa/benchmark-apps; assert the three contracts (default)
  reuse     catalog + already_built reports if present
  fixture   POST simulated=true via route=direct (no LLM, no PR)
  launch    real governed builds — requires BOSS_QA_ALLOW_CANARIES=1

Environment
  BOSS_API_URL / BOSS_API_BASE   default https://boss-api.local.sean.network
  BOSS_API_KEY                   required for live (never printed)
  BOSS_PROJECT_ID                default boss-dogfood
  BOSS_QA_ALLOW_CANARIES         must be 1 to POST live canary builds

Exit
  0 all required checks passed
  1 one or more required checks failed
  2 usage / missing key
"""
from __future__ import annotations

import argparse
import json
import os
import sys
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
    summarize,
)

GATE_NAME = "boss_qa_harness"
GATE_VERSION = "1.0.0"


def _import_feature_sweep():
    from scripts.qa import feature_sweep

    return feature_sweep


def _import_canaries():
    from scripts.qa import canary_apps

    return canary_apps


def _dry_run_catalog(importer, label: str) -> Any:
    """Return a sibling module's dry-run catalog, or a skip record.

    Glue must work if this CLI lands before feature_sweep / canary_apps.
    """
    try:
        module = importer()
        return module.dry_run_catalog()
    except ImportError as exc:
        return {"skipped": True, "reason": f"{label} unavailable: {exc}"}


def phase_preflight(client: BossClient) -> list[CheckResult]:
    checks: list[CheckResult] = []
    status, payload, ms = client.request("GET", "/health", authenticate=False)
    ok = is_2xx(status) and isinstance(payload, dict) and payload.get("status") in {
        "ok",
        "healthy",
        "up",
    }
    checks.append(
        CheckResult(
            name="preflight.health",
            ok=ok,
            detail=f"status={status} body_status={payload.get('status') if isinstance(payload, dict) else None}",
            status_code=status,
            duration_ms=ms,
            phase="preflight",
        )
    )
    status, _, ms = client.request("GET", "/v1/projects", authenticate=False)
    checks.append(
        CheckResult(
            name="preflight.unauth_fail_closed",
            ok=status in (401, 403),
            detail=f"unauthenticated /v1/projects → {status}",
            status_code=status,
            duration_ms=ms,
            phase="preflight",
        )
    )
    status, payload, ms = client.request(
        "GET", "/v1/edition", query={"project_id": client.project_id}
    )
    edition = None
    if isinstance(payload, dict):
        edition = payload.get("edition_id") or payload.get("edition")
    checks.append(
        CheckResult(
            name="preflight.edition",
            ok=is_2xx(status) and bool(edition),
            detail=f"edition={edition}",
            status_code=status,
            duration_ms=ms,
            evidence={"edition": edition},
            phase="preflight",
        )
    )
    status, payload, ms = client.request(
        "GET", "/v1/projects", query={"project_id": client.project_id}
    )
    checks.append(
        CheckResult(
            name="preflight.auth_projects",
            ok=is_2xx(status),
            detail=f"projects status={status}",
            status_code=status,
            duration_ms=ms,
            phase="preflight",
        )
    )
    return checks


def phase_control_plane(client: BossClient) -> list[CheckResult]:
    checks: list[CheckResult] = []
    q = {"project_id": client.project_id}
    for name, path, extra in (
        ("control.tasks", "/v1/tasks", {}),
        ("control.jobs", "/v1/jobs", {}),
        ("control.events", "/v1/events", {}),
        ("control.decisions_inbox", "/v1/decisions/inbox", {}),
        ("control.deadletters", "/v1/deadletters", {}),
        ("control.factory_wip", "/v1/factory/wip", {}),
        ("control.budget_status", "/v1/budget/status", {"scope_type": "project", "scope_id": client.project_id}),
    ):
        query = {**q, **extra}
        status, payload, ms = client.request("GET", path, query=query)
        checks.append(
            CheckResult(
                name=name,
                ok=is_2xx(status),
                detail=f"{path} → {status}",
                status_code=status,
                duration_ms=ms,
                evidence={"keys": list(payload)[:12] if isinstance(payload, dict) else []},
                phase="control_plane",
            )
        )
    return checks


def run_harness(
    *,
    api_url: str,
    api_key: str | None,
    project_id: str,
    actor: str,
    phases: set[str],
    canary_mode: str,
    output: Path | None,
    dry_run: bool,
    timeout: float,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "gate": GATE_NAME,
        "version": GATE_VERSION,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "api_url": api_url,
        "project_id": project_id,
        "dry_run": dry_run,
        "canary_mode": canary_mode,
        "phases": sorted(phases),
        "checks": [],
        "summary": {},
    }
    if dry_run:
        # Sibling modules may land after this CLI. Prefer real catalogs;
        # if they are missing, record a skip reason and still exit 0.
        report["catalog"] = {
            "features": _dry_run_catalog(_import_feature_sweep, "feature_sweep"),
            "canaries": _dry_run_catalog(_import_canaries, "canary_apps"),
        }
        report["summary"] = {"ok": True, "dry_run": True}
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write(report, output)
        return report

    if not api_key:
        raise SystemExit(2)

    client = BossClient(api_url, api_key, project_id=project_id, actor=actor, timeout=timeout)
    checks: list[CheckResult] = []
    if "preflight" in phases:
        checks.extend(phase_preflight(client))
    if "features" in phases:
        checks.extend(_import_feature_sweep().run_feature_sweep(client))
    if "control" in phases:
        checks.extend(phase_control_plane(client))
    if "canaries" in phases:
        checks.extend(
            _import_canaries().run_canaries(client, mode=canary_mode, timeout=timeout)
        )
    report["checks"] = [c.to_dict() for c in checks]
    report["summary"] = summarize(checks)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    _write(report, output)
    return report


def _write(report: dict[str, Any], output: Path | None) -> None:
    if output is None:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, indent=2)
    secrets = [os.environ.get("BOSS_API_KEY") or ""]
    output.write_text(redact_secrets(text, secrets), encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BOSS unified E2E QA harness")
    parser.add_argument(
        "--api-url",
        default=os.environ.get("BOSS_API_BASE")
        or os.environ.get("BOSS_API_URL")
        or DEFAULT_API_URL,
    )
    parser.add_argument("--api-key", default=os.environ.get("BOSS_API_KEY"))
    parser.add_argument(
        "--project-id",
        default=os.environ.get("BOSS_PROJECT_ID") or DEFAULT_PROJECT_ID,
    )
    parser.add_argument("--actor", default=os.environ.get("BOSS_ACTOR") or "executive_console")
    parser.add_argument(
        "--phases",
        default="preflight,features,control,canaries",
        help="Comma list: preflight,features,control,canaries",
    )
    parser.add_argument(
        "--canary-mode",
        choices=("catalog", "reuse", "fixture", "launch"),
        default="catalog",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.canary_mode == "launch" and not env_flag("BOSS_QA_ALLOW_CANARIES"):
        print(
            "canary-mode=launch requires BOSS_QA_ALLOW_CANARIES=1 "
            "(real LLM/sandbox spend).",
            file=sys.stderr,
        )
        return 2
    phases = {p.strip() for p in args.phases.split(",") if p.strip()}
    try:
        report = run_harness(
            api_url=args.api_url,
            api_key=args.api_key,
            project_id=args.project_id,
            actor=args.actor,
            phases=phases,
            canary_mode=args.canary_mode,
            output=args.output,
            dry_run=args.dry_run,
            timeout=args.timeout,
        )
    except SystemExit as exc:
        return int(exc.code or 2)
    except Exception as exc:  # noqa: BLE001
        print(redact_secrets(str(exc), [args.api_key] if args.api_key else None), file=sys.stderr)
        return 1
    summary = report.get("summary") or {}
    print(
        json.dumps(
            {
                "gate": GATE_NAME,
                "ok": summary.get("ok"),
                "required_passed": summary.get("required_passed"),
                "required": summary.get("required"),
                "failed": summary.get("failed"),
                "canary_mode": args.canary_mode,
                "dry_run": args.dry_run,
            },
            indent=2,
        )
    )
    if args.dry_run:
        return 0
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
