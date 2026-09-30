"""Isolated E2E contract for Shared-server + signals + CI/CD + OS/code scans.

Locks the five musts for `ai/grok/e2e-shared-server-sec`. Never invents host
metrics, anomaly scores, live GitHub Actions, or a Snyk run. Not public GA.
"""
from __future__ import annotations

from boss_engine.services import security_scan as sec
from boss_engine.services.telemetry_mining import mine
from boss_engine.services.telemetry_source import LogEvent

PROJECT_ID = "boss-dogfood"
VENDORS = ["github_actions", "gitlab_ci", "jenkins", "circleci", "buildkite"]
OSS_SCANNERS = ["semgrep", "trivy", "osv-scanner", "gitleaks", "checkov"]


def test_e2e_shared_server_status_empty_is_honest(client):
    """GET /v1/shared-server/status → 200 not_configured, metrics null, host down."""
    r = client.get("/v1/shared-server/status", params={"project_id": PROJECT_ID})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["product"] == "shared-server"
    assert body["status"] == "not_configured"
    assert body["metrics"] is None
    assert body["host"]["connected"] is False


def test_e2e_signals_honesty_no_invented_scores(client):
    """telemetry_mining + anomalies.scan report measured data or empty — never a score."""
    events = [LogEvent(ts=0.0, message=f"cache miss for key {i}", level="info") for i in range(20)]
    report = mine(events)
    assert report["events"] == 20
    assert report["signals"] == []
    assert report["signal_count"] == 0

    r = client.post(
        "/v1/anomalies/scan",
        json={"project_id": PROJECT_ID, "auto_lite": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["anomalies"] == []
    assert "score" not in body
    for item in body["anomalies"]:
        assert "score" not in item


def test_e2e_cicd_generate_embeds_security_gate_all_vendors(client):
    """CI generate includes the Semgrep/Trivy/OSV hook for all 5 vendors."""
    for vendor in VENDORS:
        gen = client.post(
            "/v1/cicd/generate",
            json={"vendor": vendor, "language": "python"},
        )
        assert gen.status_code == 200, (vendor, gen.text)
        lower = gen.json()["content"].lower()
        assert "semgrep" in lower, vendor
        assert "trivy" in lower, vendor
        assert "osv" in lower, vendor


def test_e2e_live_actions_requires_credentials(client):
    """Live GitHub Actions stays credential-gated — no invented cloud poll."""
    adapters = client.get("/v1/pipeline-adapters/catalog")
    assert adapters.status_code == 200, adapters.text
    gha = next(
        item
        for item in adapters.json()["items"]
        if item["kind"] == "github_actions"
    )
    assert gha["requires_credentials"] is True


def test_e2e_security_scan_latest_empty_is_200_not_404(client):
    """GET /latest with no run is honest 200 not_configured, never a missing-route 404."""
    r = client.get("/v1/security-scan/latest", params={"project_id": PROJECT_ID})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "not_configured"
    assert body["run"] is None
    assert body["summary"] is None


def test_e2e_security_scan_api_rejects_unauthenticated_run(client):
    """All API scan execution is gated before Snyk or any scanner is invoked."""
    r = client.post(
        "/v1/security-scan/run",
        json={
            "project_id": PROJECT_ID,
            "actor": "test_operator",
            "scanners": ["snyk"],
            "simulated": True,
        },
    )
    assert r.status_code == 401, r.text
    assert sec.list_runs(PROJECT_ID) == []


def test_e2e_security_scan_five_scanner_engine_fixture():
    """Internal scanner fixture still normalizes all five OSS adapters."""
    body = sec.run_scan(
        project_id=PROJECT_ID, actor="test_operator", path="/tmp/fixture",
        scanners=OSS_SCANNERS, simulated=True, engine=None,
    )
    assert body["simulated"] is True
    assert body["receipt_id"] is None
    assert body["dedupe_key"].startswith(f"security_scan:{PROJECT_ID}:")
    assert set(body["scanners"]) == set(OSS_SCANNERS)
    assert {item["scanner"] for item in body["scanner_results"]} == set(OSS_SCANNERS)
    assert all(item["status"] == "simulated" for item in body["scanner_results"])
    assert {f["scanner"] for f in body["findings"]} >= set(OSS_SCANNERS)
    assert set(sec.DEFAULT_SCANNERS) <= set(OSS_SCANNERS)
