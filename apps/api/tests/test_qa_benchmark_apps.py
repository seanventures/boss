def test_qa_benchmark_catalog_is_exposed_as_a_pinned_contract(client):
    response = client.get("/v1/qa/benchmark-apps")

    assert response.status_code == 200
    apps = response.json()["benchmark_apps"]
    assert [app["id"] for app in apps] == ["bugbox", "hookwatch", "approveit"]
    assert all(app["completion_contract"] for app in apps)
    assert all("npm run verify" not in app["verification_commands"] for app in apps)
    assert all("npm run healthcheck" in app["verification_commands"] for app in apps)


def test_catalog_name_match_is_related_not_built(client, monkeypatch):
    """Name-matched completed runs must not advertise certified `built` (report 404)."""

    def _fake_done(*_args, **_kwargs):
        return {
            "run_id": "crun_name_only",
            "run_status": "completed",
            "pull_request_url": None,
            "preview_id": None,
            "preview_url": None,
            "matched_on": "name",
        }

    monkeypatch.setattr(
        "boss_engine.services.meeting_service.completed_benchmark_build",
        _fake_done,
    )
    response = client.get("/v1/qa/benchmark-apps", params={"project_id": "boss-dogfood"})
    assert response.status_code == 200, response.text
    apps = {item["id"]: item for item in response.json()["benchmark_apps"]}
    assert "built" not in apps["bugbox"]
    assert apps["bugbox"]["related"]["matched_on"] == "name"
    assert apps["bugbox"]["related"]["reportable"] is False


def test_catalog_benchmark_id_match_is_built(client, monkeypatch):
    def _fake_done(*_args, **_kwargs):
        return {
            "run_id": "crun_bench",
            "run_status": "completed",
            "pull_request_url": None,
            "preview_id": None,
            "preview_url": None,
            "matched_on": "benchmark_id",
        }

    monkeypatch.setattr(
        "boss_engine.services.meeting_service.completed_benchmark_build",
        _fake_done,
    )
    response = client.get("/v1/qa/benchmark-apps", params={"project_id": "boss-dogfood"})
    assert response.status_code == 200, response.text
    apps = {item["id"]: item for item in response.json()["benchmark_apps"]}
    assert apps["bugbox"]["built"]["run_id"] == "crun_bench"
    assert apps["bugbox"]["built"]["reportable"] is True


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


def test_a_meeting_launched_benchmark_report_is_returned(client, engine):
    """Panel launch stamps the benchmark on the meeting. The catalog calls that run built.

    GET /report has to return that benchmark's report. The code task itself has no
    finalize_ctx.qa_benchmark — that is the run the audit 404'd.
    """
    import json

    from sqlalchemy import text

    from apps.api.app.db import begin

    with begin(engine) as connection:
        connection.execute(
            text(
                "INSERT INTO meeting_runs (id, project_id, task_id, status, phase, directive, "
                "constraints) VALUES ('mrun_panel_bugbox', 'boss-dogfood', 'task_panel_bugbox', "
                "'completed', 'done', 'Convene the panel for the issue tracker', "
                "CAST(:constraints AS JSONB))"
            ),
            {"constraints": json.dumps({"source": "qa_benchmark", "benchmark_id": "bugbox"})},
        )
        connection.execute(
            text(
                "INSERT INTO code_task_runs (id, project_id, task_id, status, phase, title) "
                "VALUES ('ctr_panel_bugbox', 'boss-dogfood', 'task_panel_bugbox', 'completed', "
                "'done', 'Panel build of the issue tracker')"
            )
        )

    catalog = client.get("/v1/qa/benchmark-apps", params={"project_id": "boss-dogfood"})
    assert catalog.status_code == 200, catalog.text
    apps = {item["id"]: item for item in catalog.json()["benchmark_apps"]}
    built = apps["bugbox"]["built"]
    assert built["run_id"] == "ctr_panel_bugbox"
    assert built["matched_on"] == "launched_as_benchmark"
    assert built["reportable"] is True

    report = client.get("/v1/qa/benchmark-apps/runs/ctr_panel_bugbox/report")
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["run_id"] == "ctr_panel_bugbox"
    assert body["benchmark_id"] == "bugbox"
    assert body["benchmark_name"] == "BugBox"

    from apps.api.app import identity_guard
    from apps.api.app.main import app
    from boss_engine.identity.declarations import Permit

    declared = [
        entry for entry in identity_guard.route_inventory(app)
        if entry.path == "/v1/qa/benchmark-apps/runs/{run_id}/report" and "GET" in entry.methods
    ]
    assert len(declared) == 1
    assert isinstance(declared[0].access, Permit)
    assert declared[0].access.describe() == "permit code_task:read @ code_task(path=run_id)"


def _insert_code_task(connection, *, run_id: str, title: str, task_id: str | None = None) -> None:
    from sqlalchemy import text

    connection.execute(
        text(
            "INSERT INTO code_task_runs (id, project_id, task_id, status, phase, title) "
            "VALUES (:id, 'boss-dogfood', :task_id, 'completed', 'done', :title)"
        ),
        {"id": run_id, "task_id": task_id, "title": title},
    )


def test_a_code_task_that_is_not_a_benchmark_still_404s(client, engine):
    from apps.api.app.db import begin

    with begin(engine) as connection:
        _insert_code_task(connection, run_id="ctr_plain", title="Ordinary fix")

    missing = client.get("/v1/qa/benchmark-apps/runs/ctr_plain/report")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "code task is not a QA benchmark run"


def test_a_name_match_is_not_a_benchmark_report(client, engine):
    from apps.api.app.db import begin

    with begin(engine) as connection:
        _insert_code_task(
            connection, run_id="ctr_name_only", title="QA benchmark: BugBox"
        )

    catalog = client.get("/v1/qa/benchmark-apps", params={"project_id": "boss-dogfood"})
    apps = {item["id"]: item for item in catalog.json()["benchmark_apps"]}
    assert "built" not in apps["bugbox"]
    assert apps["bugbox"]["related"]["matched_on"] == "name"
    assert apps["bugbox"]["related"]["reportable"] is False

    report = client.get("/v1/qa/benchmark-apps/runs/ctr_name_only/report")
    assert report.status_code == 404
    assert report.json()["detail"] == "code task is not a QA benchmark run"


def test_an_unknown_meeting_benchmark_id_is_not_a_report(client, engine):
    import json

    from sqlalchemy import text

    from apps.api.app.db import begin

    with begin(engine) as connection:
        connection.execute(
            text(
                "INSERT INTO meeting_runs (id, project_id, task_id, status, phase, directive, "
                "constraints) VALUES ('mrun_unknown_bench', 'boss-dogfood', 'task_unknown_bench', "
                "'completed', 'done', 'Build something else', CAST(:constraints AS JSONB))"
            ),
            {"constraints": json.dumps({"source": "qa_benchmark", "benchmark_id": "not-a-catalog-app"})},
        )
        _insert_code_task(
            connection,
            run_id="ctr_unknown_bench",
            title="Unlabeled build",
            task_id="task_unknown_bench",
        )

    report = client.get("/v1/qa/benchmark-apps/runs/ctr_unknown_bench/report")
    assert report.status_code == 404
    assert report.json()["detail"] == "code task is not a QA benchmark run"


def test_a_meeting_on_another_task_does_not_make_this_run_a_benchmark(client, engine):
    import json

    from sqlalchemy import text

    from apps.api.app.db import begin

    with begin(engine) as connection:
        connection.execute(
            text(
                "INSERT INTO meeting_runs (id, project_id, task_id, status, phase, directive, "
                "constraints) VALUES ('mrun_other_task', 'boss-dogfood', 'task_other', "
                "'completed', 'done', 'Convene BugBox', CAST(:constraints AS JSONB))"
            ),
            {"constraints": json.dumps({"source": "qa_benchmark", "benchmark_id": "bugbox"})},
        )
        _insert_code_task(
            connection, run_id="ctr_unlinked", title="Different work", task_id="task_this"
        )

    report = client.get("/v1/qa/benchmark-apps/runs/ctr_unlinked/report")
    assert report.status_code == 404
    assert report.json()["detail"] == "code task is not a QA benchmark run"
