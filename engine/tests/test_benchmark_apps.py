from boss_engine.services.benchmark_apps import (
    COMPLETION_CONTRACT,
    SCORING_DIMENSIONS,
    benchmark_task_input,
    completion_report,
    get_benchmark_app,
    list_benchmark_apps,
    tuning_proposal_inputs,
    tuning_recommendation,
)


def test_benchmark_catalog_covers_distinct_cross_layer_risks():
    apps = list_benchmark_apps()

    assert [app["id"] for app in apps] == ["bugbox", "hookwatch", "approveit"]
    assert all(app["completion_contract"] == COMPLETION_CONTRACT for app in apps)
    assert all("npm run healthcheck" in app["verification_commands"] for app in apps)
    assert "authorization" in get_benchmark_app("approveit")["focus"]
    assert sum(dimension["weight"] for dimension in SCORING_DIMENSIONS) == 100


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
    assert report["scorecard"]["overall_status"] == "provisional"
    capability = next(
        item
        for item in report["scorecard"]["dimensions"]
        if item["id"] == "capability"
    )
    assert capability["status"] == "not_scored"


def test_scorecard_uses_observed_runtime_metrics_without_inventing_subjective_scores():
    benchmark = benchmark_task_input("bugbox")["finalize_ctx"]["qa_benchmark"]
    report = completion_report(
        {
            "id": "crun_scored",
            "status": "completed",
            "usage": {"cost_usd_micros": 1_000_000},
            "state": {
                "design": {"plan": "Build and verify the issue tracker."},
                "tests": [{"command": "npm run verify", "exit_code": 0}],
                "tests_passed": True,
                "adversarial_verdict": "survived",
                "security_verdict": "clean",
                "regressions_ok": True,
                "consistency_verdict": "consistent",
                "turns": 20,
                "files_changed": 8,
            },
            "finalize_ctx": {
                "qa_benchmark": benchmark,
                "phase_timings": [{"phase": "implement", "ms": 120_000}],
                "repair_log": [],
            },
        },
        llm_calls=[{"total_tokens": 12_000}],
    )

    dimensions = {item["id"]: item for item in report["scorecard"]["dimensions"]}
    assert dimensions["speed"]["score"] == 100
    assert dimensions["cost"]["score"] == 100
    assert dimensions["tokens"]["score"] == 100
    assert dimensions["innovation"]["status"] == "not_scored"
    assert report["scorecard"]["overall_status"] == "provisional"


def test_tuning_packet_requires_measured_gap_and_a_developer_candidate_diff():
    benchmark = benchmark_task_input("bugbox")["finalize_ctx"]["qa_benchmark"]
    report = completion_report(
        {
            "id": "crun_tuning",
            "status": "completed",
            "usage": {"cost_usd_micros": 7_500_000},
            "state": {
                "design": {"plan": "Build and verify the issue tracker."},
                "tests": [{"command": "npm run verify", "exit_code": 0}],
            },
            "finalize_ctx": {
                "qa_benchmark": benchmark,
                "phase_timings": [{"phase": "implement", "ms": 1_800_000}],
            },
        }
    )

    recommendation = tuning_recommendation(report)
    proposal = tuning_proposal_inputs(
        report,
        change_ref="github://boss/pull/141",
        diff_summary="Adjust the strict coding workflow repair budget.",
    )

    assert recommendation["status"] == "ready_for_candidate"
    assert {gap["dimension"] for gap in recommendation["measured_gaps"]} >= {
        "speed",
        "cost",
    }
    assert proposal["change_ref"] == "github://boss/pull/141"
    assert "vary only" in proposal["test_plan"][0]
    assert "cannot apply" in recommendation["guardrails"][1]


def test_tuning_packet_refuses_to_invent_an_optimization_without_a_measured_gap():
    report = completion_report(
        {
            "id": "crun_unscored",
            "status": "completed",
            "state": {"design": {"plan": "Plan only."}, "tests": []},
            "finalize_ctx": {
                "qa_benchmark": benchmark_task_input("bugbox")["finalize_ctx"]["qa_benchmark"]
            },
        }
    )

    assert tuning_recommendation(report)["status"] == "not_ready"
    try:
        tuning_proposal_inputs(report, change_ref="pr://1", diff_summary="Try something")
    except ValueError as error:
        assert "No measured score gap" in str(error)
    else:
        raise AssertionError("proposal inputs should require observed tuning evidence")


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

    # route="direct" is the comparison path: straight to a code task, no panel. The DEFAULT
    # now convenes a PRD meeting first (see the test below), so this asks for the old route
    # explicitly rather than relying on a default that changed.
    result = api_main.start_qa_benchmark_app_endpoint(
        "bugbox",
        {"project_id": "boss-dogfood", "actor": "executive_console", "route": "direct"},
    )

    assert result == {
        "status": "queued",
        "run_id": "crun_benchmark",
        "benchmark": {"id": "bugbox", "name": "BugBox"},
    }
    # `balanced`, not `strict`: strict is withdrawn while it is being fixed, and balanced
    # still runs the full review panel -- it just always ships a PR to look at.
    assert recorded["profile"] == "balanced"
    assert recorded["risk_tier"] == 1
    assert recorded["finalize_ctx"]["qa_benchmark"]["id"] == "bugbox"


def test_tuning_proposal_endpoint_routes_measured_evidence_into_existing_change_control(
    monkeypatch,
):
    """Benchmark tuning may create a proposal, but never apply a change itself."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://localhost/boss")
    from apps.api.app import main as api_main
    from boss_engine.schemas import QABenchmarkTuningProposalRequest
    from boss_engine.services import code_task_service

    recorded = {}

    class _Rows:
        def mappings(self):
            return self

        def all(self):
            return []

    class _Connection:
        def execute(self, *_args, **_kwargs):
            return _Rows()

    class _Transaction:
        def __enter__(self):
            return _Connection()

        def __exit__(self, *_args):
            return False

    run = {
        "id": "crun_tuning",
        "project_id": "boss-dogfood",
        "status": "completed",
        "state": {
            "design": {"plan": "Implement the benchmark."},
            "tests": [{"command": "npm run verify", "exit_code": 1}],
        },
        "finalize_ctx": {
            "qa_benchmark": benchmark_task_input("bugbox")["finalize_ctx"]["qa_benchmark"]
        },
    }
    monkeypatch.setattr(api_main, "database_is_reachable", lambda _engine: True)
    monkeypatch.setattr(api_main, "begin", lambda _engine: _Transaction())
    monkeypatch.setattr(api_main, "require_project_actor", lambda *_args: None)
    monkeypatch.setattr(code_task_service, "load_code_task_run", lambda *_args: run)
    monkeypatch.setattr(
        api_main.self_improvement,
        "create_proposal",
        lambda _connection, **kwargs: recorded.update(kwargs) or {"id": "imp_tuning"},
    )
    monkeypatch.setattr(api_main, "self_improvement_detail_from_row", lambda row: row)

    result = api_main.create_qa_benchmark_tuning_proposal(
        "crun_tuning",
        QABenchmarkTuningProposalRequest(
            project_id="boss-dogfood",
            actor="vp_agents",
            change_ref="github://boss/pull/141",
            diff_summary="Adjust the coding workflow repair policy.",
        ),
    )

    assert result == {"id": "imp_tuning"}
    assert recorded["change_ref"] == "github://boss/pull/141"
    assert "functionality" in recorded["expected_impact"][0]
    assert "Revert" in recorded["rollback_plan"][0]
