#!/usr/bin/env python3
"""Sealed E2E live attempt (Plane BOSS-351 slice 9).

Defaults to dry-run. Never prints secrets. Never merges. Not GA.

  python3 scripts/qa/sealed_e2e_live.py
  python3 scripts/qa/sealed_e2e_live.py --dry-run

Dry-run is structural only (no network). It documents the live-create
gate and the sealed POST body. Exit 0 if the contract is coherent.

  BOSS_API_KEY=… BOSS_FACTORY_ALLOW_CREATE=1 \\
    BOSS_FACTORY_OWNER=acme BOSS_FACTORY_REPO=app \\
    python3 scripts/qa/sealed_e2e_live.py --live

--live POSTs exactly one narrow governed code.task with open_pr=True
and title sealed-e2e-canary, then looks for a Receipt + hold Decision.
It refuses to run unless BOSS_FACTORY_ALLOW_CREATE is truthy and a
GitHub target is configured. It never approves or merges.

If allow-create is off, the script fail-closes and lists missing flag
names only. See docs/operations/SEALED_E2E_BLOCKER.md for the 2026-08-15
goldeneye /srv/apps/boss-app/.env reading (tag b6d8ccc).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

GATE_NAME = "sealed_e2e_live"
GATE_VERSION = "1.0.0"
PLANE = "BOSS-351"
DEFAULT_API_URL = "https://boss-api.local.sean.network"
DEFAULT_PROJECT_ID = "boss-dogfood"
DEFAULT_TIMEOUT_SECONDS = 30.0
CANARY_TITLE = "sealed-e2e-canary"
SEALED_EFFECT_KIND = "github.pull_request.create"
SEALED_DECISION_REASON = "merge_decision_required"
SEALED_DEFAULT_ACTION = "hold"

REQUIRED_ALLOW_FLAGS = ("BOSS_FACTORY_ALLOW_CREATE",)
SIMILAR_ALLOW_FLAGS = ("BOSS_QA_ALLOW_CANARIES",)
API_KEY_FLAGS = ("BOSS_API_KEY",)
GITHUB_OWNER_FLAGS = (
    "BOSS_FACTORY_OWNER",
    "GITHUB_OWNER",
    "BOSS_GITHUB_OWNER",
    "BOSS_TARGET_GITHUB_OWNER",
)
GITHUB_REPO_FLAGS = (
    "BOSS_FACTORY_REPO",
    "GITHUB_REPO",
    "BOSS_GITHUB_REPO",
    "BOSS_TARGET_GITHUB_REPO",
)
GITHUB_BASE_FLAGS = ("BOSS_FACTORY_BASE_BRANCH",)
TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass
class CheckResult:
    name: str
    ok: bool
    required: bool = True
    skipped: bool = False
    detail: str = ""
    status_code: int | None = None
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


def is_2xx(status: int | None) -> bool:
    return status is not None and 200 <= status < 300


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in TRUTHY


def first_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def allow_create_from_env() -> bool:
    return any(env_flag(name) for name in REQUIRED_ALLOW_FLAGS)


def resolve_github_target() -> tuple[str | None, str | None, str]:
    owner = first_env(*GITHUB_OWNER_FLAGS)
    repo = first_env(*GITHUB_REPO_FLAGS)
    base = first_env(*GITHUB_BASE_FLAGS) or "main"
    return owner, repo, base


def missing_live_flags(*, require_api_key: bool = True) -> list[str]:
    """Flag names that block a live sealed create. Names only."""
    missing: list[str] = []
    if not allow_create_from_env():
        missing.extend(REQUIRED_ALLOW_FLAGS)
    if require_api_key and not first_env(*API_KEY_FLAGS):
        missing.append("BOSS_API_KEY")
    owner, repo, _base = resolve_github_target()
    if not owner:
        missing.append("BOSS_FACTORY_OWNER|BOSS_GITHUB_OWNER|BOSS_TARGET_GITHUB_OWNER")
    if not repo:
        missing.append("BOSS_FACTORY_REPO|BOSS_GITHUB_REPO|BOSS_TARGET_GITHUB_REPO")
    return missing


def similar_absent_flags() -> list[str]:
    return [name for name in SIMILAR_ALLOW_FLAGS if not env_flag(name)]


def canary_create_body(
    project_id: str,
    *,
    owner: str,
    repo: str,
    base_branch: str = "main",
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "title": CANARY_TITLE,
        "instructions": (
            "Narrow sealed E2E canary for BOSS-351 slice 9. "
            "Open a draft PR only. Do not merge. Prefer a one-line README note "
            "that this is a sealed-e2e-canary. Hold for a human Decision."
        ),
        "acceptance_criteria": [
            "A Receipt exists for the PR effect",
            "A hold-default Decision exists",
            "No merge is attempted",
        ],
        "test_commands": [],
        "owner": owner,
        "repo": repo,
        "base_branch": base_branch,
        "profile": "lean",
        "open_pr": True,
        "simulated": False,
    }


def extract_run_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("run_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def receipt_matches_canary(receipt: dict[str, Any], *, run_id: str | None) -> bool:
    kind = str(receipt.get("effect_kind") or "")
    if kind != SEALED_EFFECT_KIND:
        return False
    if run_id:
        blob = json.dumps(receipt, default=str)
        if run_id not in blob:
            return False
    return True


def decision_is_hold(decision: dict[str, Any]) -> bool:
    default_action = str(decision.get("default_action") or "").strip() or SEALED_DEFAULT_ACTION
    status = decision.get("status")
    reason = decision.get("reason") or (decision.get("recommendation") or {}).get("reason_code")
    if default_action != SEALED_DEFAULT_ACTION:
        return False
    if status not in {None, "pending", "blocked"}:
        return False
    if reason and reason != SEALED_DECISION_REASON:
        return False
    return True


def summarize(checks: list[CheckResult]) -> dict[str, Any]:
    required = [c for c in checks if c.required]
    failed = [c.name for c in required if not c.ok and not c.skipped]
    return {
        "total": len(checks),
        "required": len(required),
        "required_passed": sum(1 for c in required if c.ok or c.skipped),
        "failed": failed,
        "ok": not failed,
        "proven_live": False,
    }


def dry_run_checks() -> list[CheckResult]:
    checks: list[CheckResult] = []
    body = canary_create_body("boss-dogfood", owner="acme", repo="app")
    checks.append(
        CheckResult(
            name="structure.create_body",
            ok=(
                body["open_pr"] is True
                and body["simulated"] is False
                and body["title"] == CANARY_TITLE
                and "merge" in body["instructions"].lower()
            ),
            detail="open_pr=True, simulated=False, title=sealed-e2e-canary",
            evidence={"body_keys": sorted(body)},
        )
    )
    missing = missing_live_flags(require_api_key=True)
    checks.append(
        CheckResult(
            name="structure.live_gate_documented",
            ok=True,
            required=False,
            detail=(
                "live create requires BOSS_FACTORY_ALLOW_CREATE plus a GitHub "
                "target and BOSS_API_KEY; defaults stay dry-run"
            ),
            evidence={
                "required_allow_flags": list(REQUIRED_ALLOW_FLAGS),
                "similar_allow_flags": list(SIMILAR_ALLOW_FLAGS),
                "github_owner_flags": list(GITHUB_OWNER_FLAGS),
                "github_repo_flags": list(GITHUB_REPO_FLAGS),
                "current_env_missing": missing,
                "similar_absent": similar_absent_flags(),
            },
        )
    )
    checks.append(
        CheckResult(
            name="structure.no_merge",
            ok=True,
            detail="script never calls approve, merge, or sandbox merge APIs",
        )
    )
    checks.append(
        CheckResult(
            name="structure.not_ga",
            ok=True,
            detail="Not GA. BOSS-351 stays open until a live receipt+hold is proven.",
        )
    )
    return checks


class BossClient:
    def __init__(self, api_url: str, api_key: str | None, *, timeout: float) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        authenticate: bool = True,
    ) -> tuple[int, Any]:
        url = join_url(self.api_url, path)
        if query:
            filtered = {k: v for k, v in query.items() if v is not None}
            if filtered:
                url = f"{url}?{urlencode(filtered)}"
        encoded = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        if authenticate and self.api_key:
            headers["X-Boss-Api-Key"] = self.api_key
        req = Request(url, data=encoded, headers=headers, method=method.upper())
        secrets = [self.api_key] if self.api_key else []
        try:
            with urlopen(req, timeout=self.timeout) as response:  # noqa: S310
                raw = response.read().decode("utf-8", errors="replace")
                status = int(response.status)
        except HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            status = int(error.code)
        except URLError as error:
            raise RuntimeError(
                redact_secrets(f"BOSS API {method} {path} unreachable: {error.reason}", secrets)
            ) from error
        raw = redact_secrets(raw, secrets)
        if not raw.strip():
            return status, {}
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError:
            return status, {"raw": raw[:400]}


def list_items(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def live_attempt(
    client: BossClient,
    *,
    project_id: str,
    owner: str,
    repo: str,
    base_branch: str,
    wait_seconds: float,
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    body = canary_create_body(
        project_id, owner=owner, repo=repo, base_branch=base_branch
    )
    try:
        status, payload = client.request("POST", "/v1/code-tasks", body=body)
    except RuntimeError as error:
        return [
            CheckResult(
                name="live.create",
                ok=False,
                detail=str(error),
            )
        ]
    run_id = extract_run_id(payload) if is_2xx(status) else None
    checks.append(
        CheckResult(
            name="live.create",
            ok=bool(is_2xx(status) and run_id),
            detail=(
                f"status={status} run_id={run_id}"
                if run_id
                else f"code-tasks create expected 2xx+run_id, got {status}"
            ),
            status_code=status,
            evidence={"run_id": run_id, "owner": owner, "repo": repo, "open_pr": True},
        )
    )
    if not run_id:
        return checks

    deadline = time.monotonic() + max(0.0, wait_seconds)
    receipt: dict[str, Any] | None = None
    decision: dict[str, Any] | None = None
    while True:
        try:
            _st, run_payload = client.request("GET", f"/v1/code-tasks/{run_id}")
        except RuntimeError:
            run_payload = {}
        try:
            _st, receipts_payload = client.request(
                "GET",
                "/v1/receipts",
                query={"project_id": project_id, "limit": 50},
            )
        except RuntimeError:
            receipts_payload = {}
        try:
            _st, inbox_payload = client.request(
                "GET",
                "/v1/decisions/inbox",
                query={"project_id": project_id},
            )
        except RuntimeError:
            inbox_payload = {}

        for row in list_items(receipts_payload, "items", "receipts"):
            if receipt_matches_canary(row, run_id=run_id):
                receipt = row
                break
        for row in list_items(inbox_payload, "items", "decisions", "inbox"):
            blob = json.dumps(row, default=str)
            if run_id in blob and decision_is_hold(row):
                decision = row
                break
            if receipt and str(receipt.get("id") or "") and str(receipt.get("id")) in blob:
                if decision_is_hold(row):
                    decision = row
                    break
        if receipt and decision:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(min(5.0, max(0.5, deadline - time.monotonic())))

    run_status = run_payload.get("status") if isinstance(run_payload, dict) else None
    checks.append(
        CheckResult(
            name="live.receipt",
            ok=receipt is not None,
            detail=(
                f"receipt_id={receipt.get('id')} effect_kind={receipt.get('effect_kind')}"
                if receipt
                else f"no {SEALED_EFFECT_KIND} receipt yet run_status={run_status}"
            ),
            evidence={
                "receipt_id": receipt.get("id") if receipt else None,
                "run_status": run_status,
            },
        )
    )
    checks.append(
        CheckResult(
            name="live.hold_decision",
            ok=decision is not None,
            detail=(
                f"decision_id={decision.get('id')} default_action={decision.get('default_action')}"
                if decision
                else "no hold decision linked to the canary yet"
            ),
            evidence={
                "decision_id": decision.get("id") if decision else None,
                "status": decision.get("status") if decision else None,
            },
        )
    )
    checks.append(
        CheckResult(
            name="live.no_merge",
            ok=True,
            detail="no approve/merge/execute calls were issued",
        )
    )
    return checks


def build_report(
    *,
    mode: str,
    api_url: str,
    project_id: str,
    checks: list[CheckResult],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = summarize(checks)
    proven = bool(
        mode == "live"
        and any(c.name == "live.receipt" and c.ok for c in checks)
        and any(c.name == "live.hold_decision" and c.ok for c in checks)
    )
    summary["proven_live"] = proven
    return {
        "gate": GATE_NAME,
        "version": GATE_VERSION,
        "plane": PLANE,
        "mode": mode,
        "api_url": api_url,
        "project_id": project_id,
        "ga": False,
        "merge_attempted": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "checks": [c.to_dict() for c in checks],
        "missing_live_flags": missing_live_flags(require_api_key=mode == "live"),
        "similar_absent_flags": similar_absent_flags(),
        "blocker_doc": "docs/operations/SEALED_E2E_BLOCKER.md",
        **(extra or {}),
    }


def default_output_path() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for base in (Path("artifacts") / "qa", Path("/tmp") / "boss-qa" / "artifacts", Path("/tmp")):
        try:
            base.mkdir(parents=True, exist_ok=True)
            return base / f"sealed-e2e-live-{stamp}.json"
        except OSError:
            continue
    return Path(f"/tmp/sealed-e2e-live-{stamp}.json")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sealed E2E live attempt. Defaults to dry-run. Never merges.",
    )
    parser.add_argument("--api-base", "--api-url", dest="api_base", default=None)
    parser.add_argument("--api-key", default=None, help="env: BOSS_API_KEY (never printed)")
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=30.0,
        help="How long --live will poll for receipt+decision (default 30).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Structure only (default). No network.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="POST one sealed-e2e-canary if allow-create + GitHub target are set.",
    )
    parser.add_argument("--output", default=None)
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.live and args.dry_run:
        print("error: --live and --dry-run are mutually exclusive", file=sys.stderr)
        return 2

    api_url = (
        args.api_base
        or os.environ.get("BOSS_API_BASE")
        or os.environ.get("BOSS_API_URL")
        or DEFAULT_API_URL
    )
    project_id = (
        args.project_id
        or os.environ.get("PROJECT_ID")
        or os.environ.get("BOSS_PROJECT_ID")
        or DEFAULT_PROJECT_ID
    )
    api_key = args.api_key or os.environ.get("BOSS_API_KEY") or None
    live = bool(args.live)
    if not live:
        checks = dry_run_checks()
        report = build_report(
            mode="dry-run",
            api_url=api_url,
            project_id=project_id,
            checks=checks,
        )
        out = Path(args.output) if args.output else default_output_path()
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if not args.quiet:
            print(
                f"gate={GATE_NAME} mode=dry-run plane={PLANE} "
                f"ok={report['summary']['ok']} proven_live=false not_ga"
            )
            print(f"report={out}")
        return 0 if report["summary"]["ok"] else 1

    missing = missing_live_flags(require_api_key=True)
    if missing:
        checks = [
            CheckResult(
                name="live.allow_create",
                ok=False,
                detail="missing flags: " + ", ".join(missing),
                evidence={
                    "missing_flags": missing,
                    "similar_absent_flags": similar_absent_flags(),
                    "posts": 0,
                },
            )
        ]
        report = build_report(
            mode="live-blocked",
            api_url=api_url,
            project_id=project_id,
            checks=checks,
        )
        out = Path(args.output) if args.output else default_output_path()
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if not args.quiet:
            print(
                f"gate={GATE_NAME} mode=live-blocked plane={PLANE} "
                f"missing={','.join(missing)} posts=0 not_ga",
                file=sys.stderr,
            )
            print(f"report={out}", file=sys.stderr)
        return 2

    owner, repo, base_branch = resolve_github_target()
    assert owner and repo  # missing_live_flags already gated
    client = BossClient(api_url, api_key, timeout=args.timeout_seconds)
    checks = live_attempt(
        client,
        project_id=project_id,
        owner=owner,
        repo=repo,
        base_branch=base_branch,
        wait_seconds=args.wait_seconds,
    )
    report = build_report(
        mode="live",
        api_url=api_url,
        project_id=project_id,
        checks=checks,
    )
    out = Path(args.output) if args.output else default_output_path()
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if not args.quiet:
        print(
            f"gate={GATE_NAME} mode=live plane={PLANE} "
            f"ok={report['summary']['ok']} proven_live={report['summary']['proven_live']} "
            f"not_ga merge_attempted=false"
        )
        print(f"report={out}")
    if report["summary"]["proven_live"]:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
