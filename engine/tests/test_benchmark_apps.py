from boss_engine.services.benchmark_apps import (
    COMPLETION_CONTRACT,
    benchmark_task_input,
    completion_report,
    get_benchmark_app,
    list_benchmark_apps,
)


def test_benchmark_catalog_covers_distinct_cross_layer_risks():
    apps = list_benchmark_apps()

    assert [app["id"] for app in apps] == ["bugbox", "hookwatch", "approveit"]
    assert all(app["completion_contract"] == COMPLETION_CONTRACT for app in apps)
    assert all("npm run healthcheck" in app["verification_commands"] for app in apps)
    assert "authorization" in get_benchmark_app("approveit")["focus"]


def test_benchmark_task_is_pinned_to_the_governed_evidence_contract():
    task = benchmark_task_input("hookwatch")

    assert task["title"].startswith("QA benchmark: HookWatch")
    assert task["test_commands"] == ["npm run verify"]
    assert task["finalize_ctx"]["qa_benchmark"]["id"] == "hookwatch"
    assert "built-in mock destination" in task["instructions"]
    assert "npm run test:e2e" in task["instructions"]


def test_completion_report_never_certifies_missing_evidence():
    report = completion_report(
        {
            "id": "crun_missing",
            "status": "completed",
            "state": {"design": {"plan": "Use a local database."}, "tests": []},
            "finalize_ctx": {
                "qa_benchmark": benchmark_task_input("bugbox")["finalize_ctx"][
                    "qa_benchmark"
                ]
            },
        }
    )

    assert report["passed"] is False
    assert report["results"][1]["status"] == "not_recorded"
    assert "not certified" in report["message"]


def test_completion_report_requires_plan_and_successful_authoritative_verify():
    benchmark = benchmark_task_input("approveit")["finalize_ctx"]["qa_benchmark"]
    report = completion_report(
        {
            "id": "crun_green",
            "status": "completed",
            "state": {
                "design": {
                    "plan": "Model request and audit entities, then test transitions."
                },
                "tests": [
                    {"command": "npm run verify", "exit_code": 0, "timed_out": False}
                ],
            },
            "finalize_ctx": {"qa_benchmark": benchmark},
        }
    )

    assert report["passed"] is True
    assert [result["status"] for result in report["results"]] == ["pass", "pass"]


def test_launch_endpoint_routes_a_benchmark_into_the_governed_code_task(monkeypatch):
    """The QA feature must use the existing task/lease/budget path, not a side channel."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://localhost/boss")
    from apps.api.app import main as api_main
    from boss_engine.services import code_task_service

    recorded = {}

    class _Transaction:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    def fake_start(connection, **kwargs):
        recorded.update(kwargs)
        assert connection is not None
        return "crun_benchmark"

    monkeypatch.setattr(api_main, "database_is_reachable", lambda _engine: True)
    monkeypatch.setattr(api_main, "begin", lambda _engine: _Transaction())
    monkeypatch.setattr(api_main, "require_project_actor", lambda *_args: None)
    monkeypatch.setattr(code_task_service, "start_code_task", fake_start)

    result = api_main.start_qa_benchmark_app_endpoint(
        "bugbox", {"project_id": "boss-dogfood", "actor": "executive_console"}
    )

    assert result == {
        "status": "queued",
        "run_id": "crun_benchmark",
        "benchmark": {"id": "bugbox", "name": "BugBox"},
    }
    assert recorded["profile"] == "strict"
    assert recorded["risk_tier"] == 2
    assert recorded["finalize_ctx"]["qa_benchmark"]["id"] == "bugbox"
