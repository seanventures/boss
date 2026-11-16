"""BOSSAPP-1257: one Security screen reads recorded CVEs and the packages the scan named.

The rows come from the existing scanners. `security_scan.parsers` is what a done app scan
stored. `services.security_scan` is the in-memory run store. This reader does not scan, and it
does not invent a package, a CVE, or a count.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from boss_engine.security_scan import parsers
from boss_engine.services import security_scan as oss
from boss_engine.services.cve_sbom_screen import read_screen
from boss_engine.services.core import make_id

ROOT = "/src"


def _osv(
    package: str, version: str, advisory: str, *, cve: str = "", summary: str = "Recorded advisory"
) -> dict:
    group = {
        "ids": [advisory],
        "aliases": [cve] if cve else [],
        "max_severity": "7.2" if cve else "4.5",
    }
    raw = {
        "results": [
            {
                "source": {"path": f"{ROOT}/package-lock.json"},
                "packages": [
                    {
                        "package": {"name": package, "version": version, "ecosystem": "npm"},
                        "vulnerabilities": [
                            {
                                "id": advisory,
                                "summary": summary,
                                "aliases": [cve] if cve else [],
                                "affected": [{"ranges": [{"events": [{"fixed": "9.9.9"}]}]}],
                            }
                        ],
                        "groups": [group],
                    }
                ],
            }
        ],
    }
    findings, _meta = parsers.parse_osv_json(json.dumps(raw), root=__import__("pathlib").Path(ROOT))
    assert len(findings) == 1
    return findings[0]


@pytest.fixture()
def eng():
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.fail("DATABASE_URL must be the test postgres")
    from boss_engine.db import ensure_schema

    engine = create_engine(url, future=True)
    ensure_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def project(eng):
    pid = f"p1257_{uuid4().hex[:8]}"
    org, ws = f"org_{pid}", f"ws_{pid}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:i, 't')"), {"i": org})
        c.execute(
            text("INSERT INTO workspaces (id, org_id, name) VALUES (:i, :o, 't')"),
            {"i": ws, "o": org},
        )
        c.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:i, :w, :i, :i)"),
            {"i": pid, "w": ws},
        )
    oss.reset_store()
    yield pid
    oss.reset_store()
    with eng.begin() as c:
        c.execute(
            text(
                "DELETE FROM security_findings WHERE scan_id IN "
                "(SELECT id FROM security_scans WHERE project_id = :p)"
            ),
            {"p": pid},
        )
        c.execute(text("DELETE FROM security_scans WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM project_agents WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})
        c.execute(text("DELETE FROM workspaces WHERE id = :w"), {"w": ws})
        c.execute(text("DELETE FROM orgs WHERE id = :o"), {"o": org})


def _scan(c, pid: str, app: str, status: str, when: datetime) -> str:
    sid = make_id("scan")
    c.execute(
        text(
            "INSERT INTO security_scans (id, project_id, app_key, status, started_at, ended_at) "
            "VALUES (:i, :p, :a, :st, :t, :t)"
        ),
        {"i": sid, "p": pid, "a": app, "st": status, "t": when},
    )
    return sid


def _store(c, scan_id: str, finding) -> str:
    fid = make_id("sfind")
    c.execute(
        text(
            "INSERT INTO security_findings (id, scan_id, fingerprint, tool, rule_id, title, severity, "
            "location, evidence, status) VALUES (:i, :s, :fp, :tool, :rule, :title, :sev, "
            "CAST(:loc AS JSONB), CAST(:ev AS JSONB), 'open')"
        ),
        {
            "i": fid,
            "s": scan_id,
            "fp": finding.fingerprint or uuid4().hex,
            "tool": finding.tool,
            "rule": finding.rule_id,
            "title": finding.title,
            "sev": finding.severity,
            "loc": json.dumps(finding.location),
            "ev": json.dumps(finding.evidence),
        },
    )
    return fid


def _seed_oss(pid: str, findings: list[dict]) -> str:
    run_id = make_id("secscan")
    with oss._lock:
        oss._RUNS[run_id] = {
            "id": run_id,
            "project_id": pid,
            "simulated": False,
            "status": "completed",
            "findings": findings,
            "summary": {},
            "created_at": "2026-11-15T00:00:00+00:00",
        }
        oss._RUNS_BY_PROJECT.setdefault(pid, []).insert(0, run_id)
    return run_id


def test_no_recorded_scan_is_empty(eng, project):
    with eng.begin() as c:
        screen = read_screen(c, project)
    assert screen["recorded"] is False
    assert screen["apps"] == []
    assert screen["sbom"] == []
    assert "cve_count" not in screen and "total" not in screen


def test_every_app_shows_the_parser_package_and_not_an_older_or_code_finding(eng, project):
    now = datetime.now(timezone.utc)
    lodash = _osv(
        "lodash",
        "4.17.20",
        "GHSA-35jh-r3h4-6jhm",
        cve="CVE-2021-23337",
        summary="Prototype pollution in lodash",
    )
    stale = _osv("left-pad", "1.0.0", "GHSA-left-pad-pad-padx", cve="CVE-2019-0001")
    minimist = _osv(
        "minimist", "1.2.5", "GHSA-vh95-rmgr-6w4m", summary="Prototype pollution in minimist"
    )
    with eng.begin() as c:
        old = _scan(c, project, "web", "done", now - timedelta(days=2))
        latest = _scan(c, project, "web", "done", now)
        api = _scan(c, project, "api", "done", now)
        _store(c, old, stale)
        lodash_id = _store(c, latest, lodash)
        _store(
            c,
            latest,
            type(
                "F",
                (),
                {
                    "fingerprint": uuid4().hex,
                    "tool": "semgrep",
                    "rule_id": "javascript.express.sqli",
                    "title": "SQL injection",
                    "severity": "high",
                    "location": {"file": "app.js", "line": 12},
                    "evidence": {},
                },
            )(),
        )
        _store(c, api, minimist)
        c.execute(
            text(
                "INSERT INTO project_agents (id, project_id, app_key, app_name, role_key) "
                "VALUES (:i, :p, 'billing', 'Billing', 'engineer')"
            ),
            {"i": make_id("pa"), "p": project},
        )
        failed = _scan(c, project, "jobs", "failed", now)
        assert failed
        screen = read_screen(c, project)

    assert "cve_count" not in screen and "total" not in screen
    apps = {a["app_key"]: a for a in screen["apps"]}
    assert set(apps) >= {"web", "api", "billing", "jobs"}
    assert apps["billing"]["recorded"] is False and apps["billing"]["findings"] == []
    assert apps["billing"]["app_name"] == "Billing"
    assert apps["jobs"]["recorded"] is False and apps["jobs"]["status"] == "failed"
    web = apps["web"]
    assert web["recorded"] is True and web["status"] == "done"
    assert [f["id"] for f in web["findings"]] == [lodash_id]
    assert web["findings"][0]["package"] == "lodash"
    assert web["findings"][0]["severity"] == "high"
    assert web["findings"][0]["cve"] == "CVE-2021-23337"
    assert web["findings"][0]["version"] == "4.17.20"
    api_row = apps["api"]["findings"][0]
    assert api_row["package"] == "minimist" and api_row["severity"] == minimist.severity
    assert api_row["cve"] == ""
    names = {(row["app_key"], row["name"], row["version"]) for row in screen["sbom"]}
    assert names == {("web", "lodash", "4.17.20"), ("api", "minimist", "1.2.5")}
    assert screen["recorded"] is True


def test_a_simulated_run_does_not_become_recorded_cves(eng, project):
    oss.run_scan(project_id=project, simulated=True, scanners=["trivy", "osv-scanner"], engine=None)
    with eng.begin() as c:
        screen = read_screen(c, project)
    blob = json.dumps(screen)
    assert "CVE-2024-0001" not in blob
    assert "CVE-2024-1111" not in blob
    assert "example-package" not in blob.lower()
    assert screen["sbom"] == []
    assert screen["recorded"] is False


def test_an_oss_run_keeps_only_a_package_the_run_recorded(eng, project):
    _seed_oss(
        project,
        [
            {
                "severity": "critical",
                "rule_id": "CVE-2024-5555",
                "path": "package-lock.json",
                "message": "recorded",
                "scanner": "trivy",
                "cve": "CVE-2024-5555",
                "package": "marked",
                "app_key": "tools",
            },
            {
                "severity": "high",
                "rule_id": "CVE-2024-9999",
                "path": "requirements.txt",
                "message": "no package field",
                "scanner": "trivy",
                "cve": "CVE-2024-9999",
            },
        ],
    )
    with eng.begin() as c:
        screen = read_screen(c, project)
    apps = {a["app_key"]: a for a in screen["apps"]}
    assert list(apps) == ["tools"]
    [row] = apps["tools"]["findings"]
    assert row["package"] == "marked" and row["severity"] == "critical"
    assert row["cve"] == "CVE-2024-5555"
    assert screen["sbom"] == [
        {
            "app_key": "tools",
            "name": "marked",
            "version": "",
            "ecosystem": "",
            "lockfile": "package-lock.json",
        }
    ]
    assert "CVE-2024-9999" not in json.dumps(screen)
