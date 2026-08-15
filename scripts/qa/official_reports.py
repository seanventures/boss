#!/usr/bin/env python3
"""Official QA /reports for BugBox, HookWatch, ApproveIt.

Evidence-only. Never invents UI/UX/capability scores. Live official POSTs
require BOSS_QA_ALLOW_OFFICIAL=1 AND an already-on factory/budget path.
Default mode is catalog/reuse: GET existing reports and print honesty gaps.

Environment
  BOSS_API_URL / BOSS_API_BASE   default https://boss-api.local.sean.network
  BOSS_API_KEY                   required for live (never printed)
  BOSS_PROJECT_ID                default boss-dogfood
  BOSS_QA_ALLOW_OFFICIAL         must be 1 to POST official (spendy) runs

Exit
  0 honesty checks passed (or official runs posted when allowed)
  1 required honesty check failed
  2 usage / missing key
"""
from __future__ import annotations

import argparse
import json
import os
import sys
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
    summarize,
)

CANARY_IDS = ("bugbox", "hookwatch", "approveit")
ALLOW_OFFICIAL_FLAG = "BOSS_QA_ALLOW_OFFICIAL"
GATE_NAME = "boss_official_reports"
GATE_VERSION = "1.0.0"


def _result(name: str, ok: bool, **kwargs) -> CheckResult:
    return CheckResult(name=name, ok=ok, phase="official", **kwargs)


def _honesty_from_report(report: dict[str, Any]) -> dict[str, Any]:
    blob = report.get("honesty")
    if isinstance(blob, dict) and "honest" in blob:
        return blob
    scorecard = report.get("scorecard") or {}
    dimensions = scorecard.get("dimensions") or []
    missing = [
        {"id": item.get("id"), "reason": item.get("reason")}
        for item in dimensions
        if isinstance(item, dict) and item.get("status") == "not_scored"
    ]
    invented = [
        item.get("id")
        for item in dimensions
        if isinstance(item, dict)
        and item.get("status") == "scored"
        and item.get("score") is None
    ]
    return {
        "honest": not invented,
        "missing_evidence": missing,
        "invented_scores": invented,
    }


def inspect_reports(client: BossClient, *, timeout: float = 30.0) -> list[CheckResult]:
    """GET catalog + existing /reports. Fail if any report invents a score."""
    checks: list[CheckResult] = []
    status, body, ms = client.request(
        "GET",
        "/v1/qa/benchmark-apps",
        query={"project_id": client.project_id},
        timeout=timeout,
    )
    catalog_ok = is_2xx(status) and isinstance(body, dict)
    checks.append(
        _result(
            "official.catalog",
            catalog_ok,
            detail=f"GET /v1/qa/benchmark-apps → {status}",
            status_code=status,
            duration_ms=ms,
        )
    )
    if not catalog_ok:
        return checks
    items = body.get("items") or body.get("benchmark_apps") or body.get("apps") or []
    by_id = {
        str(app.get("id") or "").strip().lower(): app
        for app in items
        if isinstance(app, dict)
    }
    for canary_id in CANARY_IDS:
        app = by_id.get(canary_id) or {}
        built = app.get("built") if isinstance(app.get("built"), dict) else None
        run_id = str((built or {}).get("run_id") or "").strip()
        if not run_id:
            checks.append(
                _result(
                    f"official.{canary_id}.no_report",
                    True,
                    required=False,
                    detail="no built.run_id — skip, not an invented score",
                )
            )
            continue
        rstatus, report, rms = client.request(
            "GET",
            f"/v1/qa/benchmark-apps/runs/{run_id}/report",
            timeout=timeout,
        )
        report_ok = is_2xx(rstatus) and isinstance(report, dict)
        if not report_ok:
            checks.append(
                _result(
                    f"official.{canary_id}.report_missing",
                    True,
                    required=False,
                    detail=f"run {run_id} report → {rstatus} (stale; not invented)",
                    status_code=rstatus,
                    duration_ms=rms,
                )
            )
            continue
        honesty = _honesty_from_report(report)
        invented = honesty.get("invented_scores") or []
        missing = honesty.get("missing_evidence") or []
        checks.append(
            _result(
                f"official.{canary_id}.honesty",
                bool(honesty.get("honest")) and not invented,
                detail=(
                    f"run {run_id} honest={honesty.get('honest')} "
                    f"missing={[m.get('id') if isinstance(m, dict) else m for m in missing]}"
                ),
                status_code=rstatus,
                duration_ms=rms,
                evidence={
                    "run_id": run_id,
                    "invented_scores": invented,
                    "missing_evidence": missing,
                    "passed": report.get("passed"),
                },
            )
        )
    return checks


def request_official_runs(client: BossClient, *, timeout: float = 30.0) -> list[CheckResult]:
    """POST official runs only when the spend flag is already on."""
    if not env_flag(ALLOW_OFFICIAL_FLAG):
        return [
            _result(
                "official.launch.blocked",
                True,
                required=False,
                detail=f"{ALLOW_OFFICIAL_FLAG} is off — not posting spendy official runs",
            )
        ]
    checks: list[CheckResult] = []
    for canary_id in CANARY_IDS:
        status, body, ms = client.request(
            "POST",
            f"/v1/qa/benchmark-apps/{canary_id}/runs",
            body={"official": True, "project_id": client.project_id},
            timeout=timeout,
        )
        checks.append(
            _result(
                f"official.{canary_id}.post",
                is_2xx(status),
                required=False,
                detail=f"POST official {canary_id} → {status}",
                status_code=status,
                duration_ms=ms,
                evidence={"run_id": (body or {}).get("id") if isinstance(body, dict) else None},
            )
        )
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("inspect", "official"), default="inspect")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    api_url = os.environ.get("BOSS_API_URL") or os.environ.get("BOSS_API_BASE") or DEFAULT_API_URL
    api_key = os.environ.get("BOSS_API_KEY") or ""
    project_id = os.environ.get("BOSS_PROJECT_ID") or DEFAULT_PROJECT_ID
    if not api_key:
        print("BOSS_API_KEY is required", file=sys.stderr)
        return 2
    client = BossClient(api_url, api_key=api_key, project_id=project_id)
    checks = inspect_reports(client)
    if args.mode == "official":
        checks.extend(request_official_runs(client))
    summary = summarize(checks)
    payload = {
        "gate": GATE_NAME,
        "version": GATE_VERSION,
        "ok": summary["ok"],
        "summary": summary,
        "checks": [c.to_dict() for c in checks],
    }
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(f"{GATE_NAME} {GATE_VERSION}: {'ok' if summary['ok'] else 'FAIL'}")
        for item in checks:
            mark = "ok" if item.ok else "FAIL"
            print(f"  [{mark}] {item.name}: {item.detail}")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
