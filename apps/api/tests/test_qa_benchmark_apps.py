def test_qa_benchmark_catalog_is_exposed_as_a_pinned_contract(client):
    response = client.get("/v1/qa/benchmark-apps")

    assert response.status_code == 200
    apps = response.json()["benchmark_apps"]
    assert [app["id"] for app in apps] == ["bugbox", "hookwatch", "approveit"]
    assert all(app["completion_contract"] for app in apps)
    assert all("npm run verify" not in app["verification_commands"] for app in apps)
    assert all("npm run healthcheck" in app["verification_commands"] for app in apps)


def test_simulated_direct_run_is_not_a_certified_pass(client, engine):
    from sqlalchemy import text

    from apps.api.app.db import begin

    created = client.post(
        "/v1/qa/benchmark-apps/bugbox/runs",
        json={
            "project_id": "boss-dogfood",
            "actor": "executive_console",
            "route": "direct",
            "simulated": True,
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "simulated"
    assert body["simulated"] is True
    assert body["job_enqueued"] is False
    assert body["passed"] is False
    run_id = body["run_id"]
    assert run_id

    report = client.get(f"/v1/qa/benchmark-apps/runs/{run_id}/report")
    assert report.status_code == 200, report.text
    payload = report.json()
    assert payload["passed"] is not True
    assert payload.get("simulated") is True
    assert payload["run_status"] == "simulated"

    with begin(engine) as connection:
        job_count = connection.execute(
            text("SELECT count(*) FROM jobs WHERE payload->>'run_id' = :id"),
            {"id": run_id},
        ).scalar_one()
    assert int(job_count) == 0


def test_simulated_defaults_to_direct_and_rejects_open_pr(client):
    created = client.post(
        "/v1/qa/benchmark-apps/hookwatch/runs",
        json={
            "project_id": "boss-dogfood",
            "actor": "executive_console",
            "simulated": True,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "simulated"
    assert created.json()["passed"] is False

    rejected = client.post(
        "/v1/qa/benchmark-apps/approveit/runs",
        json={
            "project_id": "boss-dogfood",
            "actor": "executive_console",
            "route": "direct",
            "simulated": True,
            "open_pr": True,
        },
    )
    assert rejected.status_code == 422, rejected.text
    assert "cannot open a PR" in rejected.text
