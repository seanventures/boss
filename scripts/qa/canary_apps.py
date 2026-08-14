"""Phase 3 of the BOSS QA harness: BugBox, HookWatch, ApproveIt.

Talks only to the BOSS control-plane HTTP API via BossClient. Never prints
API keys. Never calls models, GitHub, or sandboxes directly.

Modes
  catalog   GET the pinned catalog (default, no spend)
  reuse     catalog + already-built reports / public preview probe
  fixture   POST simulated=true via route=direct (no LLM, no PR)
  launch    real governed builds — requires BOSS_QA_ALLOW_CANARIES=1
"""
from __future__ import annotations

import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from scripts.qa.lib import BossClient, CheckResult, env_flag, is_2xx

CANARY_IDS = ("bugbox", "hookwatch", "approveit")
CANARY_MODES = ("catalog", "reuse", "fixture", "launch")
PHASE = "canaries"
HEALTHCHECK_CMD = "npm run healthcheck"
VERIFY_CMD = "npm run verify"
TERMINAL_RUN_STATES = frozenset({"completed", "failed", "cancelled"})
LAUNCH_POLL_INTERVAL_SECONDS = 5.0
LAUNCH_POLL_TIMEOUT_DEFAULT = 1800.0
ALLOW_CANARIES_FLAG = "BOSS_QA_ALLOW_CANARIES"


def dry_run_catalog() -> dict[str, Any]:
    """Structural catalog the harness prints in --dry-run. No network."""
    return {
        "ids": list(CANARY_IDS),
        "count": len(CANARY_IDS),
        "modes": list(CANARY_MODES),
    }


def run_canaries(
    client: BossClient,
    *,
    mode: str,
    timeout: float = 30.0,
    poll_timeout: float | None = None,
) -> list[CheckResult]:
    """Run one canary mode against a live or fake BossClient."""
    resolved = (mode or "catalog").strip().lower() or "catalog"
    if resolved not in CANARY_MODES:
        return [
            _result(
                "canary.mode",
                False,
                detail=f"unknown mode={mode!r}; expected one of {list(CANARY_MODES)}",
            )
        ]
    if resolved == "launch" and not env_flag(ALLOW_CANARIES_FLAG):
        raise RuntimeError(
            f"launch mode requires {ALLOW_CANARIES_FLAG}=1 "
            "(real LLM/sandbox spend)."
        )
    effective_poll = (
        poll_timeout
        if poll_timeout is not None
        else (LAUNCH_POLL_TIMEOUT_DEFAULT if resolved == "launch" else timeout)
    )

    apps, catalog_checks = _load_catalog(client)
    if resolved == "catalog":
        return catalog_checks
    if resolved == "reuse":
        return catalog_checks + _run_reuse(client, apps, timeout=timeout)
    if resolved == "fixture":
        return _run_fixture(client, timeout=timeout)
    return _run_launch(client, timeout=timeout, poll_timeout=effective_poll)


def _result(
    name: str,
    ok: bool,
    *,
    required: bool = True,
    detail: str = "",
    status_code: int | None = None,
    duration_ms: int = 0,
    evidence: dict[str, Any] | None = None,
) -> CheckResult:
    return CheckResult(
        name=name,
        ok=ok,
        required=required,
        detail=detail,
        status_code=status_code,
        duration_ms=duration_ms,
        evidence=evidence or {},
        phase=PHASE,
    )


def _apps_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("benchmark_apps", "items", "apps"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _app_contract(app: dict[str, Any]) -> tuple[bool, str]:
    contract = app.get("completion_contract")
    if not contract:
        return False, "missing completion_contract"
    commands = app.get("verification_commands")
    if not isinstance(commands, list):
        return False, "verification_commands is not a list"
    if HEALTHCHECK_CMD not in commands:
        return False, f"{HEALTHCHECK_CMD!r} missing from verification_commands"
    if VERIFY_CMD in commands:
        return False, f"{VERIFY_CMD!r} must not appear in verification_commands"
    return True, "contract ok"


def _load_catalog(client: BossClient) -> tuple[list[dict[str, Any]], list[CheckResult]]:
    checks: list[CheckResult] = []
    status, payload, ms = client.request(
        "GET",
        "/v1/qa/benchmark-apps",
        query={"project_id": client.project_id},
    )
    if not is_2xx(status):
        checks.append(
            _result(
                "canary.catalog",
                False,
                detail=f"GET /v1/qa/benchmark-apps → {status}",
                status_code=status,
                duration_ms=ms,
            )
        )
        for canary_id in CANARY_IDS:
            checks.append(
                _result(
                    f"canary.{canary_id}.catalog",
                    False,
                    detail="catalog request failed",
                    status_code=status,
                    duration_ms=ms,
                )
            )
        return [], checks

    apps = _apps_from_payload(payload)
    by_id = {
        str(app.get("id") or "").strip().lower(): app
        for app in apps
        if str(app.get("id") or "").strip()
    }
    found = tuple(by_id)
    expected = set(CANARY_IDS)
    actual = set(by_id)
    checks.append(
        _result(
            "canary.catalog.ids",
            actual == expected,
            detail=f"ids={list(found)} expected={list(CANARY_IDS)}",
            status_code=status,
            duration_ms=ms,
            evidence={"ids": list(found), "expected": list(CANARY_IDS)},
        )
    )
    for canary_id in CANARY_IDS:
        app = by_id.get(canary_id)
        if app is None:
            checks.append(
                _result(
                    f"canary.{canary_id}.catalog",
                    False,
                    detail=f"{canary_id} missing from catalog",
                    status_code=status,
                    duration_ms=ms,
                )
            )
            continue
        ok, reason = _app_contract(app)
        checks.append(
            _result(
                f"canary.{canary_id}.catalog",
                ok,
                detail=reason,
                status_code=status,
                duration_ms=ms,
                evidence={
                    "id": canary_id,
                    "has_completion_contract": bool(app.get("completion_contract")),
                    "verification_commands": list(app.get("verification_commands") or []),
                    "built": bool(app.get("built")),
                },
            )
        )
    return apps, checks


def _run_reuse(
    client: BossClient, apps: list[dict[str, Any]], *, timeout: float
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    by_id = {
        str(app.get("id") or "").strip().lower(): app
        for app in apps
        if str(app.get("id") or "").strip()
    }
    for canary_id in CANARY_IDS:
        app = by_id.get(canary_id) or {}
        built = app.get("built") if isinstance(app.get("built"), dict) else None
        run_id = str((built or {}).get("run_id") or "").strip()
        if not run_id:
            checks.append(
                _result(
                    f"canary.{canary_id}.not_built",
                    True,
                    required=False,
                    detail="no built.run_id — skip, not a false red",
                    evidence={"built": False},
                )
            )
            continue
        status, report, ms = client.request(
            "GET",
            f"/v1/qa/benchmark-apps/runs/{run_id}/report",
            timeout=timeout,
        )
        report_ok = is_2xx(status) and isinstance(report, dict)
        passed = bool(report.get("passed")) if report_ok else None
        if not report_ok:
            # Catalog advertised a built run that the report endpoint cannot
            # load (deleted run, not a QA benchmark). Honesty, not a false red.
            checks.append(
                _result(
                    f"canary.{canary_id}.built_stale",
                    False,
                    required=False,
                    detail=(
                        f"catalog built.run_id={run_id} report missing → {status} "
                        "(stale pointer; not a launch failure)"
                    ),
                    status_code=status,
                    duration_ms=ms,
                    evidence={"run_id": run_id, "stale": True},
                )
            )
            continue
        checks.append(
            _result(
                f"canary.{canary_id}.report",
                report_ok,
                required=False,
                detail=f"built run {run_id} report → {status} passed={passed}",
                status_code=status,
                duration_ms=ms,
                evidence={
                    "run_id": run_id,
                    "passed": passed,
                    "run_status": report.get("run_status") if report_ok else None,
                },
            )
        )
        preview_url = str((built or {}).get("preview_url") or "").strip()
        if preview_url:
            checks.append(_probe_preview(canary_id, preview_url, timeout=timeout))
    return checks


def _probe_preview(canary_id: str, preview_url: str, *, timeout: float) -> CheckResult:
    """Public preview GET. 409 expired is honesty, never a required fail."""
    status, ms = _fetch_public(preview_url, timeout=timeout)
    expired = status == 409
    return _result(
        f"canary.{canary_id}.preview",
        True,
        required=False,
        detail=f"preview status={status}" + (" expired" if expired else ""),
        status_code=status,
        duration_ms=ms,
        evidence={
            "preview_host": urlparse(preview_url).netloc,
            "expired": expired,
        },
    )


def _fetch_public(url: str, *, timeout: float) -> tuple[int | None, int]:
    """GET a public URL. Never attaches API keys."""
    started = time.monotonic()
    req = Request(url, method="GET", headers={"Accept": "*/*"})
    try:
        with urlopen(req, timeout=timeout) as response:  # noqa: S310 — caller URL
            status = int(response.status)
    except HTTPError as error:
        status = int(error.code)
    except URLError:
        status = None
    return status, int((time.monotonic() - started) * 1000)


def _run_fixture(client: BossClient, *, timeout: float) -> list[CheckResult]:
    checks: list[CheckResult] = []
    for canary_id in CANARY_IDS:
        body = {
            "project_id": client.project_id,
            "actor": client.actor,
            "route": "direct",
            "simulated": True,
        }
        status, payload, ms = client.request(
            "POST",
            f"/v1/qa/benchmark-apps/{canary_id}/runs",
            body=body,
            timeout=timeout,
        )
        if status == 422:
            checks.append(
                _result(
                    f"canary.{canary_id}.fixture.unsupported",
                    True,
                    required=False,
                    detail="API returned 422 for simulated=true (old image) — skip",
                    status_code=status,
                    duration_ms=ms,
                    evidence={"simulated": True, "skipped": True, "reason": "http_422"},
                )
            )
            continue
        payload = payload if isinstance(payload, dict) else {}
        simulated_ack = bool(payload.get("simulated")) or payload.get("status") == "simulated"
        if is_2xx(status) and not simulated_ack:
            checks.append(
                _result(
                    f"canary.{canary_id}.fixture.ignored",
                    True,
                    required=False,
                    detail=(
                        "API ignored simulated=true and did not acknowledge a fixture; "
                        "refusing to treat a real run as a no-spend pass"
                    ),
                    status_code=status,
                    duration_ms=ms,
                    evidence={
                        "simulated": False,
                        "skipped": True,
                        "post_status": payload.get("status"),
                    },
                )
            )
            continue
        if not is_2xx(status):
            checks.append(
                _result(
                    f"canary.{canary_id}.fixture",
                    False,
                    detail=f"POST simulated fixture → {status}",
                    status_code=status,
                    duration_ms=ms,
                    evidence={"simulated": True},
                )
            )
            continue
        run_id = str(payload.get("run_id") or "").strip()
        if not run_id:
            checks.append(
                _result(
                    f"canary.{canary_id}.fixture",
                    False,
                    detail="simulated POST succeeded but run_id is missing",
                    status_code=status,
                    duration_ms=ms,
                    evidence={"simulated": True},
                )
            )
            continue
        checks.append(_evaluate_fixture_report(client, canary_id, run_id, timeout=timeout))
    return checks


def _evaluate_fixture_report(
    client: BossClient, canary_id: str, run_id: str, *, timeout: float
) -> CheckResult:
    status, report, ms = client.request(
        "GET",
        f"/v1/qa/benchmark-apps/runs/{run_id}/report",
        timeout=timeout,
    )
    if not is_2xx(status) or not isinstance(report, dict):
        return _result(
            f"canary.{canary_id}.fixture",
            False,
            detail=f"fixture report {run_id} → {status}",
            status_code=status,
            duration_ms=ms,
            evidence={"simulated": True, "run_id": run_id},
        )
    if report.get("passed") is True:
        return _result(
            f"canary.{canary_id}.fixture",
            False,
            detail="honesty fail: simulated fixture report claimed passed=true",
            status_code=status,
            duration_ms=ms,
            evidence={
                "simulated": True,
                "passed": True,
                "run_id": run_id,
                "run_status": report.get("run_status"),
            },
        )
    return _result(
        f"canary.{canary_id}.fixture",
        True,
        detail="simulated fixture recorded; not a certified pass",
        status_code=status,
        duration_ms=ms,
        evidence={
            "simulated": True,
            "passed": False,
            "run_id": run_id,
            "run_status": report.get("run_status"),
        },
    )


def _run_launch(
    client: BossClient, *, timeout: float, poll_timeout: float
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    for canary_id in CANARY_IDS:
        body = {
            "project_id": client.project_id,
            "actor": client.actor,
        }
        status, payload, ms = client.request(
            "POST",
            f"/v1/qa/benchmark-apps/{canary_id}/runs",
            body=body,
            timeout=timeout,
        )
        payload = payload if isinstance(payload, dict) else {}
        if not is_2xx(status):
            checks.append(
                _result(
                    f"canary.{canary_id}.launch",
                    False,
                    detail=f"POST launch → {status}",
                    status_code=status,
                    duration_ms=ms,
                )
            )
            continue
        already_built = payload.get("status") == "already_built"
        run_id = str(payload.get("run_id") or "").strip() or None
        if already_built:
            checks.append(
                _reuse_already_built(client, canary_id, run_id, timeout=timeout, post_ms=ms)
            )
            continue
        report_status, report, poll_ms = _poll_report(
            client,
            canary_id,
            run_id,
            timeout=timeout,
            poll_timeout=poll_timeout,
        )
        report_ok = is_2xx(report_status) and isinstance(report, dict)
        passed = bool(report.get("passed")) if report_ok else False
        terminal = report_ok and report.get("run_status") in TERMINAL_RUN_STATES
        checks.append(
            _result(
                f"canary.{canary_id}.launch",
                bool(report_ok and passed),
                detail=(
                    f"launch run={report.get('run_id') if report_ok else run_id} "
                    f"run_status={report.get('run_status') if report_ok else None} "
                    f"passed={passed} terminal={terminal}"
                ),
                status_code=report_status,
                duration_ms=ms + poll_ms,
                evidence={
                    "run_id": (report.get("run_id") if report_ok else run_id),
                    "passed": passed,
                    "run_status": report.get("run_status") if report_ok else None,
                    "already_built": False,
                    "post_status": payload.get("status"),
                },
            )
        )
    return checks


def _reuse_already_built(
    client: BossClient,
    canary_id: str,
    run_id: str | None,
    *,
    timeout: float,
    post_ms: int,
) -> CheckResult:
    if not run_id:
        return _result(
            f"canary.{canary_id}.launch",
            False,
            detail="already_built response missing run_id",
            duration_ms=post_ms,
            evidence={"already_built": True},
        )
    status, report, ms = client.request(
        "GET",
        f"/v1/qa/benchmark-apps/runs/{run_id}/report",
        timeout=timeout,
    )
    report_ok = is_2xx(status) and isinstance(report, dict)
    return _result(
        f"canary.{canary_id}.launch",
        report_ok,
        detail=(
            f"already_built run {run_id} report → {status} "
            f"passed={report.get('passed') if report_ok else None}"
        ),
        status_code=status,
        duration_ms=post_ms + ms,
        evidence={
            "run_id": run_id,
            "already_built": True,
            "passed": report.get("passed") if report_ok else None,
            "run_status": report.get("run_status") if report_ok else None,
        },
    )


def _poll_report(
    client: BossClient,
    canary_id: str,
    run_id: str | None,
    *,
    timeout: float,
    poll_timeout: float,
) -> tuple[int | None, Any, int]:
    deadline = time.monotonic() + max(poll_timeout, 0.0)
    last_status: int | None = None
    last_payload: Any = {}
    last_ms = 0
    current_id = run_id
    while True:
        resolved = _resolve_report_run_id(client, canary_id, current_id, timeout=timeout)
        if resolved:
            current_id = resolved
            last_status, last_payload, last_ms = client.request(
                "GET",
                f"/v1/qa/benchmark-apps/runs/{current_id}/report",
                timeout=timeout,
            )
            if is_2xx(last_status) and isinstance(last_payload, dict):
                if last_payload.get("run_status") in TERMINAL_RUN_STATES:
                    return last_status, last_payload, last_ms
        if time.monotonic() >= deadline:
            return last_status, last_payload, last_ms
        _sleep(LAUNCH_POLL_INTERVAL_SECONDS)


def _resolve_report_run_id(
    client: BossClient,
    canary_id: str,
    posted_run_id: str | None,
    *,
    timeout: float,
) -> str | None:
    if posted_run_id:
        status, payload, _ = client.request(
            "GET",
            f"/v1/qa/benchmark-apps/runs/{posted_run_id}/report",
            timeout=timeout,
        )
        if is_2xx(status):
            return posted_run_id
    status, payload, _ = client.request(
        "GET",
        "/v1/qa/benchmark-apps",
        query={"project_id": client.project_id},
        timeout=timeout,
    )
    if is_2xx(status):
        for app in _apps_from_payload(payload):
            if str(app.get("id") or "").strip().lower() != canary_id:
                continue
            built = app.get("built") if isinstance(app.get("built"), dict) else None
            built_id = str((built or {}).get("run_id") or "").strip()
            if built_id:
                return built_id
    status, payload, _ = client.request(
        "GET",
        "/v1/code-tasks",
        query={"project_id": client.project_id},
        timeout=timeout,
    )
    if is_2xx(status) and isinstance(payload, dict):
        for item in payload.get("code_tasks") or []:
            if not isinstance(item, dict):
                continue
            ctx = item.get("finalize_ctx") or {}
            benchmark = ctx.get("qa_benchmark") if isinstance(ctx, dict) else None
            if isinstance(benchmark, dict) and benchmark.get("id") == canary_id:
                found = str(item.get("id") or item.get("run_id") or "").strip()
                if found:
                    return found
    return posted_run_id


def _sleep(seconds: float) -> None:
    time.sleep(seconds)
