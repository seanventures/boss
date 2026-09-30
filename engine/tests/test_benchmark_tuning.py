from boss_engine.services import benchmark_tuning
from boss_engine.services.benchmark_apps import benchmark_task_input


def test_terminal_benchmark_sweep_materializes_changed_evidence_without_applying_change(
    monkeypatch,
):
    run = {
        "id": "crun_terminal",
        "project_id": "boss-dogfood",
        "status": "completed",
        "state": {
            "design": {"plan": "Implement and verify the benchmark."},
            "tests": [{"command": "npm run verify", "exit_code": 1}],
        },
        "finalize_ctx": {
            "qa_benchmark": benchmark_task_input("bugbox")["finalize_ctx"]["qa_benchmark"]
        },
    }
    recorded = []
    monkeypatch.setattr(
        benchmark_tuning, "_terminal_benchmark_run_ids", lambda *_args: ["crun_terminal"]
    )
    monkeypatch.setattr(benchmark_tuning, "load_code_task_run", lambda *_args: run)
    monkeypatch.setattr(benchmark_tuning, "_llm_calls", lambda *_args: [])

    def fake_record(_connection, **kwargs):
        recorded.append(kwargs)
        return (
            {
                "id": "qbeval_1",
                "project_id": "boss-dogfood",
                "code_task_run_id": "crun_terminal",
                "benchmark_id": "bugbox",
                "scorecard_version": "1.0",
                "status": kwargs["recommendation"]["status"],
                "overall_score": 0,
                "observed_weight": 26,
                "changed": True,
            },
            True,
        )

    monkeypatch.setattr(benchmark_tuning, "_record_evaluation", fake_record)

    result = benchmark_tuning.sweep_terminal_benchmarks(object(), limit=100)

    assert result["status"] == "succeeded"
    assert result["checked_count"] == 1
    assert result["changed_count"] == 1
    assert result["ready_for_candidate_count"] == 1
    assert recorded[0]["recommendation"]["status"] == "ready_for_candidate"
    assert "change_ref" not in recorded[0]["recommendation"]
