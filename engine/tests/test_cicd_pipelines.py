"""Pure unit tests for multi-vendor CI/CD generate + catalog + validate + ledger.

No Postgres, no FastAPI app — proves generator completeness for all vendors.
"""
from __future__ import annotations

import pytest

from boss_engine.services.ci_config_generator import (
    SUPPORTED_TARGETS,
    TARGET_PATHS,
    CIConfigGenerator,
)
from boss_engine.services.cicd_pipelines import (
    catalog,
    generate,
    list_runs,
    record_run,
    reset_for_tests,
    status,
    validate_yaml_structure,
)


VENDORS = list(SUPPORTED_TARGETS)
LANGUAGES = ["python", "typescript", "go"]


@pytest.fixture(autouse=True)
def _clean():
    reset_for_tests()
    yield
    reset_for_tests()


def test_supported_targets_are_five_market_vendors():
    assert set(SUPPORTED_TARGETS) == {
        "github_actions",
        "gitlab_ci",
        "jenkins",
        "circleci",
        "buildkite",
    }
    for t in SUPPORTED_TARGETS:
        assert t in TARGET_PATHS


@pytest.mark.parametrize("vendor", VENDORS)
@pytest.mark.parametrize("language", LANGUAGES)
def test_generator_emits_nonempty_for_all_vendors(vendor, language):
    result = CIConfigGenerator().generate(language=language, target=vendor)
    assert result["target"] == vendor
    assert result["path"] == TARGET_PATHS[vendor]
    assert result["content"].strip()
    assert len(result["content"]) > 40
    assert len(result["checksum"]) == 64
    assert "test" in result["commands"]
    assert "build" in result["commands"]


def test_generator_deterministic():
    g = CIConfigGenerator()
    a = g.generate(language="python", target="gitlab_ci")
    b = g.generate(language="python", target="gitlab_ci")
    assert a == b


def test_catalog_lists_vendors():
    cat = catalog()
    assert cat["count"] == 5
    ids = {v["id"] for v in cat["vendors"]}
    assert ids == set(VENDORS)
    for v in cat["vendors"]:
        assert v["generates_config"] is True
        assert v["config_path"]


@pytest.mark.parametrize("vendor", VENDORS)
def test_validate_accepts_generated(vendor):
    gen = generate(vendor=vendor, language="python")
    result = validate_yaml_structure(gen["content"], vendor=vendor)
    assert result["valid"] is True, result
    assert result["errors"] == []


def test_validate_rejects_empty_and_broken():
    empty = validate_yaml_structure("  ")
    assert empty["valid"] is False
    broken = validate_yaml_structure("jobs: [\n  - x: [", vendor="github_actions")
    assert broken["valid"] is False


def test_provenance_ledger_dedupe_and_status():
    run = record_run(
        project_id="p1",
        vendor="github_actions",
        status="passed",
        dedupe_key="dk-1",
        delivery_run_id="del_1",
    )
    again = record_run(
        project_id="p1",
        vendor="github_actions",
        status="failed",
        dedupe_key="dk-1",
    )
    assert again["id"] == run["id"]
    assert again["status"] == "passed"
    listed = list_runs(project_id="p1")
    assert listed["count"] == 1
    st = status(project_id="p1")
    assert st["recent_runs"][0]["id"] == run["id"]
    assert st["links"]["delivery"]


def test_record_run_rejects_bad_status():
    with pytest.raises(ValueError):
        record_run(project_id="p1", vendor="jenkins", status="exploded")


def test_generated_templates_include_security_scan_gate():
    """Every vendor template carries a Semgrep/Trivy/OSV security-scan gate hook."""
    for vendor in VENDORS:
        result = generate(vendor=vendor, language="python")
        blob = result["content"].lower()
        assert "semgrep" in blob, vendor
        assert "trivy" in blob, vendor
        assert "osv" in blob, vendor
        assert "snyk" not in blob or "never" in blob, vendor


def test_catalog_notes_live_actions_are_credential_gated():
    cat = catalog()
    notes = " ".join(cat["notes"]).lower()
    assert "credential" in notes
    gha = next(v for v in cat["vendors"] if v["id"] == "github_actions")
    assert gha["live_adapter"] is True
