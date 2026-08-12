"""HTTP unit tests for /v1/cicd using a minimal FastAPI app (no Postgres).

The full-suite conftest requires a shared DB that other agents thrash; these
tests mount only the cicd router so generate/catalog/validate/runs stay green
in isolation.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.app.routers.cicd_pipelines import router
from boss_engine.services.cicd_pipelines import reset_for_tests


VENDORS = ["github_actions", "gitlab_ci", "jenkins", "circleci", "buildkite"]


@pytest.fixture
def client():
    reset_for_tests()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c
    reset_for_tests()


def test_catalog(client):
    r = client.get("/v1/cicd/catalog")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 5
    assert {v["id"] for v in body["vendors"]} == set(VENDORS)


@pytest.mark.parametrize("vendor", VENDORS)
def test_generate_all_vendors(client, vendor):
    r = client.post(
        "/v1/cicd/generate",
        json={"vendor": vendor, "language": "python"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["content"].strip()
    assert body["path"]
    assert body["checksum"]


def test_generate_all_languages_all_vendors(client):
    for vendor in VENDORS:
        for language in ("python", "typescript", "go"):
            r = client.post(
                "/v1/cicd/generate",
                json={"vendor": vendor, "language": language},
            )
            assert r.status_code == 200, (vendor, language, r.text)
            assert len(r.json()["content"].strip()) > 40


def test_validate_and_runs_and_status(client):
    gen = client.post(
        "/v1/cicd/generate",
        json={"vendor": "circleci", "language": "go"},
    ).json()
    val = client.post(
        "/v1/cicd/validate",
        json={"yaml": gen["content"], "vendor": "circleci"},
    )
    assert val.status_code == 200
    assert val.json()["valid"] is True

    run = client.post(
        "/v1/cicd/runs",
        json={
            "project_id": "boss-dogfood",
            "vendor": "circleci",
            "status": "passed",
            "dedupe_key": "unit:1",
        },
    )
    assert run.status_code == 200
    rid = run.json()["id"]
    again = client.post(
        "/v1/cicd/runs",
        json={
            "project_id": "boss-dogfood",
            "vendor": "circleci",
            "status": "failed",
            "dedupe_key": "unit:1",
        },
    )
    assert again.json()["id"] == rid

    listed = client.get("/v1/cicd/runs", params={"project_id": "boss-dogfood"})
    assert listed.status_code == 200
    assert listed.json()["count"] == 1

    st = client.get("/v1/cicd/status", params={"project_id": "boss-dogfood"})
    assert st.status_code == 200
    assert st.json()["recent_runs"]
