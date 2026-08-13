"""Unit tests for scripts/deps_audit_strict.py (no live PyPI / OSV)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "deps_audit_strict.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("deps_audit_strict", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


audit = _load_module()


def test_runtime_pins_exclude_dev_only_toolchain():
    pins = audit.load_runtime_pins(ROOT)
    names = {pin.name for pin in pins}
    assert "pyjwt" in names
    assert "cryptography" in names
    assert "fastapi" in names
    assert "pytest" not in names
    assert "ruff" not in names
    assert "playwright" not in names
    sources = {pin.source for pin in pins}
    assert "engine/requirements.txt" in sources
    assert "apps/api/requirements.txt" in sources


def test_parse_requirement_line_handles_extras_and_comments():
    assert audit.parse_requirement_line("PyJWT[crypto]==2.13.0") == (
        "pyjwt",
        "PyJWT[crypto]==2.13.0",
    )
    assert audit.parse_requirement_line("# comment") is None
    assert audit.parse_requirement_line("playwright==1.60.0  # browsers") == (
        "playwright",
        "playwright==1.60.0",
    )


def test_classify_severity_from_cvss_and_labels():
    assert audit.classify_severity({"severity": "HIGH"}) == "high"
    assert audit.classify_severity({"severity": "critical"}) == "critical"
    assert audit.classify_severity({"severity": "moderate"}) == "moderate"
    assert audit.classify_severity({"severity": [{"score": "9.1"}]}) == "critical"
    assert audit.classify_severity({"severity": [{"score": "7.5"}]}) == "high"
    assert audit.classify_severity({"severity": [{"score": "5.0"}]}) == "moderate"
    assert audit.classify_severity({"id": "GHSA-xxxx"}) == "unknown"
    assert (
        audit.classify_severity(
            {"severity": [{"score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H"}]}
        )
        == "high"
    )
    assert (
        audit.classify_severity(
            {"severity": [{"score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:L/A:N"}]}
        )
        == "moderate"
    )
    assert audit.cvss_v3_base_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H") == 7.5
    assert audit.cvss_v3_base_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:L/A:N") == 6.5


def test_osv_enrichment_maps_unknown_via_ghsa():
    finding = audit.Finding(
        package="starlette",
        version="0.46.2",
        vuln_id="PYSEC-2026-249",
        severity="unknown",
        aliases=("GHSA-82w8-qh3p-5jfq",),
        description="",
    )

    def fake_fetch(vuln_id: str):
        assert vuln_id == "GHSA-82w8-qh3p-5jfq"
        return {
            "id": vuln_id,
            "database_specific": {"severity": "HIGH"},
            "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H"}],
        }

    enriched = audit.enrich_finding_severity(finding, fetch=fake_fetch)
    assert enriched.severity == "high"


def test_dedupe_findings_by_package_and_id():
    first = audit.Finding("starlette", "0.46.2", "PYSEC-1", "moderate", (), "")
    second = audit.Finding("starlette", "0.46.2", "PYSEC-1", "moderate", (), "")
    third = audit.Finding("starlette", "0.46.2", "PYSEC-2", "high", (), "")
    assert [f.vuln_id for f in audit.dedupe_findings([first, second, third])] == [
        "PYSEC-1",
        "PYSEC-2",
    ]


def test_high_and_unknown_findings_block(tmp_path):
    payload = {
        "dependencies": [
            {
                "name": "demo",
                "version": "1.0.0",
                "vulns": [
                    {"id": "GHSA-high", "severity": "high", "aliases": []},
                ],
            }
        ]
    }
    fixture = tmp_path / "high.json"
    fixture.write_text(json.dumps(payload), encoding="utf-8")
    code, report = audit.evaluate(root=ROOT, python=sys.executable, json_in=fixture)
    assert code == 1
    assert "GHSA-high" in report
    assert "severity=high" in report


def test_critical_finding_blocks(tmp_path):
    payload = {
        "dependencies": [
            {
                "name": "demo",
                "version": "1.0.0",
                "vulns": [{"id": "GHSA-crit", "severity": "critical"}],
            }
        ]
    }
    fixture = tmp_path / "crit.json"
    fixture.write_text(json.dumps(payload), encoding="utf-8")
    code, _report = audit.evaluate(root=ROOT, python=sys.executable, json_in=fixture)
    assert code == 1


def test_unknown_severity_is_fail_closed(tmp_path):
    payload = {
        "dependencies": [
            {
                "name": "demo",
                "version": "1.0.0",
                "vulns": [{"id": "GHSA-unk", "aliases": ["CVE-2024-0001"]}],
            }
        ]
    }
    fixture = tmp_path / "unk.json"
    fixture.write_text(json.dumps(payload), encoding="utf-8")
    code, report = audit.evaluate(root=ROOT, python=sys.executable, json_in=fixture)
    assert code == 1
    assert "severity=unknown" in report


def test_moderate_only_does_not_fail(tmp_path):
    payload = {
        "dependencies": [
            {
                "name": "demo",
                "version": "1.0.0",
                "vulns": [{"id": "GHSA-mod", "severity": "moderate"}],
            }
        ]
    }
    fixture = tmp_path / "mod.json"
    fixture.write_text(json.dumps(payload), encoding="utf-8")
    code, report = audit.evaluate(root=ROOT, python=sys.executable, json_in=fixture)
    assert code == 0
    assert "non-blocking" in report


def test_clean_audit_passes(tmp_path):
    fixture = tmp_path / "clean.json"
    fixture.write_text(json.dumps({"dependencies": []}), encoding="utf-8")
    code, report = audit.evaluate(root=ROOT, python=sys.executable, json_in=fixture)
    assert code == 0
    assert "no published advisories" in report


def test_main_json_in_exit_codes(tmp_path):
    clean = tmp_path / "clean.json"
    clean.write_text(json.dumps({"dependencies": []}), encoding="utf-8")
    assert audit.main(["--root", str(ROOT), "--json-in", str(clean)]) == 0

    dirty = tmp_path / "dirty.json"
    dirty.write_text(
        json.dumps(
            {
                "dependencies": [
                    {
                        "name": "demo",
                        "version": "1.0",
                        "vulns": [{"id": "GHSA-x", "severity": "high"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert audit.main(["--root", str(ROOT), "--json-in", str(dirty)]) == 1
