"""Unit tests for scripts/qa/canary_apps.py (no live network)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.qa.canary_apps import (  # noqa: E402
    CANARY_IDS,
    dry_run_catalog,
    run_canaries,
)
from scripts.qa.lib import CheckResult  # noqa: E402


class FakeClient:
    """In-memory BossClient stand-in. No sockets."""

    def __init__(
        self,
        routes: dict[tuple[str, str], tuple[int, Any]] | None = None,
        *,
        project_id: str = "boss-dogfood",
        actor: str = "executive_console",
    ) -> None:
        self.project_id = project_id
        self.actor = actor
        self.api_url = "https://boss-api.test"
        self.routes = routes or {}
        self.calls: list[dict[str, Any]] = []

    def add(self, method: str, path: str, status: int, payload: Any) -> None:
        self.routes[(method.upper(), path)] = (status, payload)

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
        authenticate: bool = True,
    ) -> tuple[int, Any, int]:
        self.calls.append(
            {
                "method": method.upper(),
                "path": path,
                "query": query,
                "body": body,
                "authenticate": authenticate,
            }
        )
        hit = self.routes.get((method.upper(), path))
        if hit is None:
            return 404, {"detail": "not found"}, 1
        status, payload = hit
        return status, payload, 1


def _valid_app(app_id: str, *, built: dict[str, Any] | None = None) -> dict[str, Any]:
    app: dict[str, Any] = {
        "id": app_id,
        "name": app_id,
        "completion_contract": [
            "Written implementation plan is attached to the governed run."
        ],
        "verification_commands": [
            "npm run build",
            "npm run typecheck",
            "npm run lint",
            "npm run test:api",
            "npm run test:e2e",
            "npm run healthcheck",
        ],
    }
    if built is not None:
        app["built"] = built
    return app


def _catalog(ids: tuple[str, ...] = CANARY_IDS, *, built: dict[str, dict] | None = None) -> dict:
    apps = []
    for app_id in ids:
        apps.append(_valid_app(app_id, built=(built or {}).get(app_id)))
    return {"benchmark_apps": apps}


def _by_name(checks: list[CheckResult]) -> dict[str, CheckResult]:
    return {check.name: check for check in checks}


def test_dry_run_catalog_shape():
    catalog = dry_run_catalog()
    assert catalog["ids"] == list(CANARY_IDS)
    assert catalog["count"] == 3
    assert catalog["modes"] == ["catalog", "reuse", "fixture", "launch"]


def test_catalog_payload_with_three_apps_all_ok():
    client = FakeClient()
    client.add("GET", "/v1/qa/benchmark-apps", 200, _catalog())
    checks = run_canaries(client, mode="catalog")
    assert checks
    assert all(check.phase == "canaries" for check in checks)
    named = _by_name(checks)
    assert named["canary.catalog.ids"].ok
    for app_id in CANARY_IDS:
        assert named[f"canary.{app_id}.catalog"].ok
        assert named[f"canary.{app_id}.catalog"].required
    assert all(check.ok for check in checks if check.required)


def test_catalog_missing_approveit_fails():
    client = FakeClient()
    client.add("GET", "/v1/qa/benchmark-apps", 200, _catalog(("bugbox", "hookwatch")))
    checks = run_canaries(client, mode="catalog")
    named = _by_name(checks)
    assert named["canary.approveit.catalog"].ok is False
    assert named["canary.approveit.catalog"].required is True
    assert named["canary.catalog.ids"].ok is False
    assert named["canary.bugbox.catalog"].ok
    assert named["canary.hookwatch.catalog"].ok


def test_fixture_report_with_passed_true_fails_honesty():
    client = FakeClient()
    for app_id in CANARY_IDS:
        run_id = f"crun_{app_id}"
        client.add(
            "POST",
            f"/v1/qa/benchmark-apps/{app_id}/runs",
            201,
            {"status": "simulated", "run_id": run_id, "simulated": True},
        )
        client.add(
            "GET",
            f"/v1/qa/benchmark-apps/runs/{run_id}/report",
            200,
            {"run_id": run_id, "passed": True, "simulated": True, "run_status": "simulated"},
        )
    checks = run_canaries(client, mode="fixture")
    required = [check for check in checks if check.required]
    assert required
    assert all(check.ok is False for check in required)
    assert all(check.phase == "canaries" for check in checks)
    assert any("passed=true" in check.detail for check in required)
    for call in client.calls:
        if call["method"] == "POST":
            assert call["body"]["route"] == "direct"
            assert call["body"]["simulated"] is True
            assert call["body"].get("open_pr") is not True


def test_fixture_report_simulated_not_passed_ok():
    client = FakeClient()
    for app_id in CANARY_IDS:
        run_id = f"crun_{app_id}"
        client.add(
            "POST",
            f"/v1/qa/benchmark-apps/{app_id}/runs",
            201,
            {"status": "simulated", "run_id": run_id, "simulated": True},
        )
        client.add(
            "GET",
            f"/v1/qa/benchmark-apps/runs/{run_id}/report",
            200,
            {
                "run_id": run_id,
                "passed": False,
                "simulated": True,
                "run_status": "simulated",
            },
        )
    checks = run_canaries(client, mode="fixture")
    required = [check for check in checks if check.required]
    assert required
    assert all(check.ok for check in required)
    assert all(check.evidence.get("simulated") is True for check in required)
    assert all(check.evidence.get("passed") is False for check in required)


def test_launch_without_env_flag_raises(monkeypatch):
    monkeypatch.delenv("BOSS_QA_ALLOW_CANARIES", raising=False)
    client = FakeClient()
    with pytest.raises(RuntimeError, match="BOSS_QA_ALLOW_CANARIES"):
        run_canaries(client, mode="launch")
    assert client.calls == []


def test_reuse_with_no_built_is_not_required_fail():
    client = FakeClient()
    client.add("GET", "/v1/qa/benchmark-apps", 200, _catalog())
    checks = run_canaries(client, mode="reuse")
    named = _by_name(checks)
    for app_id in CANARY_IDS:
        skip = named[f"canary.{app_id}.not_built"]
        assert skip.required is False
        assert skip.ok is True
    required = [check for check in checks if check.required]
    assert all(check.ok for check in required)


def test_reuse_stale_built_pointer_is_not_required_fail():
    client = FakeClient()
    client.add(
        "GET",
        "/v1/qa/benchmark-apps",
        200,
        {
            "benchmark_apps": [
                _valid_app("bugbox", built={"run_id": "crun_missing"}),
                _valid_app("hookwatch"),
                _valid_app("approveit"),
            ]
        },
    )
    checks = run_canaries(client, mode="reuse")
    named = _by_name(checks)
    stale = named["canary.bugbox.built_stale"]
    assert stale.required is False
    assert stale.ok is False
    required = [check for check in checks if check.required]
    assert all(check.ok for check in required)


def test_fixture_422_old_image_is_optional_skip():
    client = FakeClient()
    for app_id in CANARY_IDS:
        client.add(
            "POST",
            f"/v1/qa/benchmark-apps/{app_id}/runs",
            422,
            {"detail": "unexpected field simulated"},
        )
    checks = run_canaries(client, mode="fixture")
    assert checks
    assert all(check.required is False for check in checks)
    assert all(check.ok for check in checks)
    assert all(check.name.endswith(".fixture.unsupported") for check in checks)
