"""CI/CD multi-vendor product surface: catalog, generate, validate, provenance.

Proves:
* GET  /v1/cicd/catalog lists all five market vendors
* POST /v1/cicd/generate emits non-empty configs for every vendor
* POST /v1/cicd/validate accepts good YAML and rejects empty/broken
* POST /v1/cicd/runs records provenance; GET lists; dedupe is idempotent
* GET  /v1/cicd/status summarizes project state
* No live cloud calls (pure / dry-run)
"""
from __future__ import annotations

import pytest

from boss_engine.services.ci_config_generator import SUPPORTED_TARGETS
from boss_engine.services.cicd_pipelines import reset_for_tests


PROJECT_ID = "boss-dogfood"
VENDORS = ["github_actions", "gitlab_ci", "jenkins", "circleci", "buildkite"]
LANGUAGES = ["python", "typescript", "go"]


@pytest.fixture(autouse=True)
def _clean_ledger():
    reset_for_tests()
    yield
    reset_for_tests()


def test_catalog_lists_all_vendors(client):
    r = client.get("/v1/cicd/catalog")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 5
    ids = {v["id"] for v in body["vendors"]}
    assert ids == set(VENDORS)
    assert set(body["supported_generate_targets"]) == set(SUPPORTED_TARGETS)
    for vendor in body["vendors"]:
        assert vendor["config_path"]
        assert vendor["generates_config"] is True
        assert "description" in vendor
        assert "languages" in vendor
        # No secret values in catalog
        blob = str(vendor)
        assert "ghp_" not in blob
        assert "sk-" not in blob


@pytest.mark.parametrize("vendor", VENDORS)
@pytest.mark.parametrize("language", LANGUAGES)
def test_generate_all_vendors_nonempty(client, vendor, language):
    r = client.post(
        "/v1/cicd/generate",
        json={"vendor": vendor, "language": language},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["target"] == vendor
    assert body["vendor"] == vendor
    assert body["path"]
    assert body["content"]
    assert len(body["content"].strip()) > 40
    assert body["checksum"]
    assert len(body["checksum"]) == 64
    assert "test" in body["commands"]
    assert "build" in body["commands"]
    # Deploy handoff remains governed
    assert "ChangeSet" in body["content"] or "BOSS" in body["content"]


def test_generate_with_framework_alias(client):
    r = client.post(
        "/v1/cicd/generate",
        json={
            "vendor": "github",
            "language": "javascript",
            "framework": "nextjs",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["target"] == "github_actions"
    assert "npm" in body["commands"]["setup"]


def test_generate_unsupported_language_422(client):
    r = client.post(
        "/v1/cicd/generate",
        json={"vendor": "github_actions", "language": "cobol"},
    )
    assert r.status_code == 422, r.text


def test_generate_unsupported_vendor_422(client):
    r = client.post(
        "/v1/cicd/generate",
        json={"vendor": "travis", "language": "python"},
    )
    assert r.status_code == 422, r.text


def test_validate_good_github_actions_yaml(client):
    gen = client.post(
        "/v1/cicd/generate",
        json={"vendor": "github_actions", "language": "python"},
    ).json()
    r = client.post(
        "/v1/cicd/validate",
        json={"yaml": gen["content"], "vendor": "github_actions"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid"] is True
    assert body["errors"] == []
    assert "jobs" in body["summary"]["keys"]


def test_validate_good_gitlab_and_circle_and_buildkite(client):
    for vendor in ("gitlab_ci", "circleci", "buildkite"):
        gen = client.post(
            "/v1/cicd/generate",
            json={"vendor": vendor, "language": "go"},
        ).json()
        r = client.post(
            "/v1/cicd/validate",
            json={"yaml": gen["content"], "vendor": vendor},
        )
        assert r.status_code == 200, r.text
        assert r.json()["valid"] is True, (vendor, r.text)


def test_validate_jenkinsfile(client):
    gen = client.post(
        "/v1/cicd/generate",
        json={"vendor": "jenkins", "language": "python"},
    ).json()
    r = client.post(
        "/v1/cicd/validate",
        json={"yaml": gen["content"], "vendor": "jenkins"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid"] is True
    assert body["summary"]["kind"] == "jenkinsfile"


def test_validate_empty_and_broken(client):
    empty = client.post("/v1/cicd/validate", json={"yaml": "   "})
    assert empty.status_code == 200
    assert empty.json()["valid"] is False
    assert empty.json()["errors"]

    broken = client.post(
        "/v1/cicd/validate",
        json={"yaml": "jobs: [\n  - broken: [", "vendor": "github_actions"},
    )
    assert broken.status_code == 200
    assert broken.json()["valid"] is False
    assert any("parse" in e.lower() for e in broken.json()["errors"])


def test_record_list_runs_and_dedupe(client):
    first = client.post(
        "/v1/cicd/runs",
        json={
            "project_id": PROJECT_ID,
            "vendor": "github_actions",
            "status": "passed",
            "external_id": "run-42",
            "repository": "acme/app",
            "ref": "main",
            "commit_sha": "abc123",
            "dedupe_key": "gha:acme/app:run-42",
            "delivery_run_id": "delivery_1",
            "actor": "ceo",
        },
    )
    assert first.status_code == 200, first.text
    run = first.json()
    assert run["id"].startswith("cicd_run_")
    assert run["project_id"] == PROJECT_ID
    assert run["vendor"] == "github_actions"
    assert run["status"] == "passed"
    assert run["source"] == "boss_provenance_ledger"
    assert run["delivery_run_id"] == "delivery_1"

    # Idempotent on dedupe_key
    again = client.post(
        "/v1/cicd/runs",
        json={
            "project_id": PROJECT_ID,
            "vendor": "github_actions",
            "status": "failed",
            "dedupe_key": "gha:acme/app:run-42",
        },
    )
    assert again.status_code == 200
    assert again.json()["id"] == run["id"]
    assert again.json()["status"] == "passed"  # not mutated

    listed = client.get(
        "/v1/cicd/runs",
        params={"project_id": PROJECT_ID},
    )
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["count"] >= 1
    assert any(item["id"] == run["id"] for item in body["items"])


def test_record_run_invalid_status_422(client):
    r = client.post(
        "/v1/cicd/runs",
        json={
            "project_id": PROJECT_ID,
            "vendor": "jenkins",
            "status": "exploded",
        },
    )
    assert r.status_code == 422, r.text


def test_status_summary(client):
    client.post(
        "/v1/cicd/runs",
        json={
            "project_id": PROJECT_ID,
            "vendor": "gitlab_ci",
            "status": "running",
            "repository": "acme/svc",
        },
    )
    r = client.get("/v1/cicd/status", params={"project_id": PROJECT_ID})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project_id"] == PROJECT_ID
    assert len(body["vendors"]) == 5
    assert body["recent_runs"]
    assert body["run_counts"]["total_listed"] >= 1
    assert "gitlab_ci" in body["run_counts"]["by_vendor"]
    assert body["links"]["delivery"]
    assert body["links"]["deployments"]
    assert body["links"]["pipeline_adapters"]


def test_legacy_ci_config_generate_still_works(client):
    """Existing /v1/ci-config/generate remains the thin dry-run for GHA/Jenkins."""
    r = client.post(
        "/v1/ci-config/generate",
        json={"language": "python", "target": "github_actions"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["path"] == ".github/workflows/ci.yml"


def test_generate_embeds_security_scan_gate_all_vendors(client):
    """catalog → generate → validate E2E includes a Semgrep/Trivy/OSV gate hook."""
    cat = client.get("/v1/cicd/catalog")
    assert cat.status_code == 200
    notes = " ".join(cat.json()["notes"]).lower()
    assert "credential" in notes

    for vendor in VENDORS:
        gen = client.post(
            "/v1/cicd/generate",
            json={"vendor": vendor, "language": "python"},
        )
        assert gen.status_code == 200, gen.text
        content = gen.json()["content"]
        lower = content.lower()
        assert "semgrep" in lower, vendor
        assert "trivy" in lower, vendor
        assert "osv" in lower, vendor
        valid = client.post(
            "/v1/cicd/validate",
            json={"yaml": content, "vendor": vendor},
        )
        assert valid.status_code == 200, valid.text
        assert valid.json()["valid"] is True, (vendor, valid.text)


def test_status_does_not_claim_live_github_actions(client):
    """Live GitHub Actions poll stays credential-gated — status is a ledger, not cloud."""
    r = client.get("/v1/cicd/status", params={"project_id": PROJECT_ID})
    assert r.status_code == 200, r.text
    body = r.json()
    notes = " ".join(body.get("notes") or []).lower()
    assert "credential" in notes
    adapters = client.get("/v1/pipeline-adapters/catalog")
    assert adapters.status_code == 200, adapters.text
    gha = next(
        item
        for item in adapters.json()["items"]
        if item["kind"] == "github_actions"
    )
    assert gha["requires_credentials"] is True
