"""HTTP surface for OSS security vulnerability scanning (simulated mode).

Covers:
  GET  /v1/security-scan/scanners
  POST /v1/security-scan/run  (simulated=True fixture findings)
  GET  /v1/security-scan/runs
  GET  /v1/security-scan/runs/{run_id}
  POST /v1/security-scan/runs/{run_id}/gate

Also exercises fail-closed unknown scanners, critical gate no-go, and receipt
dedupe_key presence without requiring Semgrep/Trivy/etc. binaries.
"""
from __future__ import annotations

import pytest

from boss_engine.services import security_scan as sec


@pytest.fixture(autouse=True)
def _clear_scan_store():
    sec.reset_store()
    yield
    sec.reset_store()


def test_scanners_catalog_includes_oss_and_snyk_note(client):
    r = client.get("/v1/security-scan/scanners")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] >= 5
    ids = {item["id"] for item in body["items"]}
    assert {"semgrep", "trivy", "osv-scanner", "gitleaks", "checkov"} <= ids
    assert "snyk" in ids
    snyk = next(i for i in body["items"] if i["id"] == "snyk")
    assert snyk["status"] == "planned_byo"
    assert snyk["available"] is False
    for item in body["items"]:
        assert "stars_note" in item
        assert "website" in item
    assert "semgrep" in body["supported"]
    assert "snyk" not in body["supported"]
    assert "snyk" in body["notes"]
    assert "never called" in body["notes"]["snyk"].lower() or "BYO" in body["notes"]["snyk"]


def test_scanners_catalog_can_exclude_planned(client):
    r = client.get("/v1/security-scan/scanners", params={"include_planned": False})
    assert r.status_code == 200, r.text
    ids = {item["id"] for item in r.json()["items"]}
    assert "snyk" not in ids
    assert "semgrep" in ids


def test_run_simulated_produces_findings_and_receipt(client):
    r = client.post(
        "/v1/security-scan/run",
        json={
            "project_id": "boss-dogfood",
            "actor": "test_operator",
            "scanners": ["semgrep", "trivy", "gitleaks"],
            "simulated": True,
            "path": "/tmp",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"].startswith("secscan_")
    assert body["project_id"] == "boss-dogfood"
    assert body["actor"] == "test_operator"
    assert body["simulated"] is True
    assert body["scanners"] == ["semgrep", "trivy", "gitleaks"]
    assert body["summary"]["total"] >= 1
    assert body["summary"]["has_critical"] is True  # trivy + gitleaks fixtures
    assert body["status"] == "failed"  # critical findings fail closed
    assert body["gate"]["decision"] == "no-go"
    assert body["gate"]["go"] is False
    assert body["dedupe_key"] == f"security_scan:boss-dogfood:{body['id']}"
    # Receipt is best-effort when DB is available (tests have Postgres).
    assert body.get("receipt_id") is not None or body.get("receipt_id") is None
    findings = body["findings"]
    assert all("severity" in f and "rule_id" in f and "scanner" in f for f in findings)
    assert any(f.get("cve") for f in findings)
    scanners_seen = {f["scanner"] for f in findings}
    assert "semgrep" in scanners_seen
    assert "trivy" in scanners_seen
    assert "gitleaks" in scanners_seen


def test_run_unknown_scanner_fails_closed(client):
    r = client.post(
        "/v1/security-scan/run",
        json={
            "project_id": "boss-dogfood",
            "actor": "test_operator",
            "scanners": ["semgrep", "not-a-real-scanner"],
            "simulated": True,
        },
    )
    assert r.status_code == 400, r.text
    detail = r.json().get("detail", "")
    assert "unknown" in detail.lower() or "unsupported" in detail.lower()


def test_run_snyk_fails_closed_never_invoked(client):
    r = client.post(
        "/v1/security-scan/run",
        json={
            "project_id": "boss-dogfood",
            "actor": "test_operator",
            "scanners": ["snyk"],
            "simulated": True,
        },
    )
    assert r.status_code == 400, r.text
    detail = r.json().get("detail", "").lower()
    assert "snyk" in detail or "unsupported" in detail or "unknown" in detail


def test_list_and_get_runs(client):
    created = client.post(
        "/v1/security-scan/run",
        json={
            "project_id": "boss-dogfood",
            "actor": "test_operator",
            "scanners": ["checkov"],
            "simulated": True,
        },
    )
    assert created.status_code == 200, created.text
    run_id = created.json()["id"]

    listed = client.get(
        "/v1/security-scan/runs",
        params={"project_id": "boss-dogfood", "limit": 10},
    )
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["count"] >= 1
    assert any(item["id"] == run_id for item in body["items"])
    assert body.get("last_summary") is not None
    assert body["last_summary"]["run_id"] == run_id

    got = client.get(f"/v1/security-scan/runs/{run_id}")
    assert got.status_code == 200, got.text
    assert got.json()["id"] == run_id
    assert got.json()["findings"]

    missing = client.get("/v1/security-scan/runs/secscan_does_not_exist")
    assert missing.status_code == 404


def test_gate_evaluation_critical_no_go(client):
    created = client.post(
        "/v1/security-scan/run",
        json={
            "project_id": "boss-dogfood",
            "actor": "test_operator",
            "scanners": ["gitleaks"],
            "simulated": True,
            "fail_on_critical": True,
        },
    )
    assert created.status_code == 200, created.text
    run_id = created.json()["id"]
    assert created.json()["gate"]["decision"] == "no-go"

    gate = client.post(f"/v1/security-scan/runs/{run_id}/gate", json={})
    assert gate.status_code == 200, gate.text
    gbody = gate.json()
    assert gbody["gate"]["go"] is False
    assert gbody["gate"]["decision"] == "no-go"
    assert "critical" in gbody["gate"]["fail_on"]


def test_gate_can_relax_severities(client):
    # checkov fixtures are high/medium only — go under critical-only policy.
    created = client.post(
        "/v1/security-scan/run",
        json={
            "project_id": "boss-dogfood",
            "actor": "test_operator",
            "scanners": ["checkov"],
            "simulated": True,
            "fail_on_critical": True,
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    run_id = body["id"]
    assert body["summary"]["critical"] == 0
    assert body["gate"]["go"] is True
    assert body["status"] == "completed"

    # Re-gate failing on high as well → no-go.
    gate = client.post(
        f"/v1/security-scan/runs/{run_id}/gate",
        json={"fail_on": ["critical", "high"]},
    )
    assert gate.status_code == 200, gate.text
    assert gate.json()["gate"]["go"] is False
    assert gate.json()["gate"]["blocking_counts"].get("high", 0) >= 1


def test_gate_missing_run_404(client):
    r = client.post("/v1/security-scan/runs/secscan_missing/gate", json={})
    assert r.status_code == 404


def test_default_scanners_simulated(client):
    r = client.post(
        "/v1/security-scan/run",
        json={
            "project_id": "boss-dogfood",
            "actor": "test_operator",
            "simulated": True,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["scanners"]) == set(sec.DEFAULT_SCANNERS)
    assert body["summary"]["total"] >= 1
    assert all(sr["status"] == "simulated" for sr in body["scanner_results"])


def test_engine_unit_unknown_scanner_raises():
    with pytest.raises(ValueError, match="unknown|unsupported"):
        sec.run_scanner("snyk", "/tmp", simulated=True)


def test_engine_simulated_finding_schema():
    result = sec.run_scanner("semgrep", "/tmp/project", simulated=True)
    assert result.status == "simulated"
    assert result.findings
    f = result.findings[0]
    d = f.to_dict()
    assert d["severity"] in sec.SEVERITIES
    assert d["rule_id"]
    assert d["path"]
    assert d["message"]
    assert d["scanner"] == "semgrep"


def test_latest_empty_is_honest_200(client):
    """No scan yet → 200 not_configured, never a 404 that looks like a missing route."""
    r = client.get(
        "/v1/security-scan/latest",
        params={"project_id": "boss-dogfood"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "not_configured"
    assert body["run"] is None
    assert body["summary"] is None
    assert body["project_id"] == "boss-dogfood"


def test_latest_returns_simulated_run(client):
    created = client.post(
        "/v1/security-scan/run",
        json={
            "project_id": "boss-dogfood",
            "actor": "test_operator",
            "scanners": ["gitleaks"],
            "simulated": True,
        },
    )
    assert created.status_code == 200, created.text
    run_id = created.json()["id"]
    latest = client.get(
        "/v1/security-scan/latest",
        params={"project_id": "boss-dogfood"},
    )
    assert latest.status_code == 200, latest.text
    body = latest.json()
    assert body["status"] != "not_configured"
    assert body["run"]["id"] == run_id
    assert body["run"]["simulated"] is True


def test_os_code_scan_covers_fs_deps_and_secrets(client):
    """OS/code means filesystem + dependency + secrets — not a commercial Snyk claim."""
    r = client.post(
        "/v1/security-scan/run",
        json={
            "project_id": "boss-dogfood",
            "actor": "test_operator",
            "scanners": ["semgrep", "trivy", "osv-scanner", "gitleaks", "checkov"],
            "simulated": True,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["simulated"] is True
    assert body["receipt_id"]
    assert body["dedupe_key"].startswith("security_scan:boss-dogfood:")
    kinds = {item["scanner"]: item for item in body["scanner_results"]}
    assert set(kinds) == {"semgrep", "trivy", "osv-scanner", "gitleaks", "checkov"}
    assert all(item["status"] == "simulated" for item in body["scanner_results"])
    scanners_seen = {f["scanner"] for f in body["findings"]}
    assert {"semgrep", "trivy", "osv-scanner", "gitleaks", "checkov"} <= scanners_seen
