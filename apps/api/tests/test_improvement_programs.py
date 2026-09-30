from __future__ import annotations

import json

from sqlalchemy import text

from apps.api.app.config import get_settings


def _report(overall: float, speed: float) -> dict:
    return {
        "benchmark_id": "bugbox",
        "required_verification_commands": ["npm run verify"],
        "scorecard": {
            "version": "1.0",
            "baselines": {"tokens": 200_000},
            "dimensions": [
                {"id": "functionality", "weight": 80, "score": 100, "status": "scored"},
                {"id": "speed", "weight": 20, "score": speed, "status": "scored"},
            ],
            "observed_weight": 100,
            "overall_score": overall,
        },
    }


def _seed_evaluations(engine, project_id: str) -> None:
    with engine.begin() as connection:
        for run_id in ("run-control", "run-candidate"):
            connection.execute(
                text(
                    """
                    INSERT INTO code_task_runs (id, project_id, status, phase, title)
                    VALUES (:id, :project_id, 'completed', 'done', :title)
                    """
                ),
                {"id": run_id, "project_id": project_id, "title": run_id},
            )
        for evaluation_id, run_id, overall, speed in (
            ("eval-control", "run-control", 80, 60),
            ("eval-candidate", "run-candidate", 90, 100),
        ):
            report = _report(overall, speed)
            connection.execute(
                text(
                    """
                    INSERT INTO qa_benchmark_evaluations (
                        id, project_id, code_task_run_id, benchmark_id,
                        scorecard_version, status, overall_score, observed_weight,
                        report, recommendation, source_hash, evaluated_by
                    ) VALUES (
                        :id, :project_id, :run_id, 'bugbox', '1.0',
                        'ready_for_candidate', :overall, 100, CAST(:report AS JSONB),
                        '{}'::jsonb, :source_hash, 'test'
                    )
                    """
                ),
                {
                    "id": evaluation_id,
                    "project_id": project_id,
                    "run_id": run_id,
                    "overall": overall,
                    "report": json.dumps(report),
                    "source_hash": f"hash-{evaluation_id}",
                },
            )


def test_improvement_program_enforces_experiment_and_rollout_gates(client, engine):
    project_id = get_settings().default_project_id
    _seed_evaluations(engine, project_id)

    program_response = client.post(
        "/v1/improvement-programs",
        json={
            "project_id": project_id,
            "name": "Coding workflow quality",
            "objective": "Improve quality without hiding regressions.",
            "baseline_evaluation_id": "eval-control",
            "minimum_evidence_weight": 100,
            "minimum_overall_delta": 2,
            "hard_gates": {"functionality": 100},
            "actor": "vp_agents",
        },
    )
    assert program_response.status_code == 201, program_response.text
    program = program_response.json()

    invalid_trial = client.post(
        f"/v1/improvement-programs/{program['id']}/trials",
        json={
            "candidate_evaluation_id": "eval-candidate",
            "candidate_change_ref": "github://boss/pull/300",
            "candidate_diff_summary": "Changed two variables.",
            "changed_variable": "model_route",
            "control_configuration": {"model_route": "a", "repair_budget": 1},
            "candidate_configuration": {"model_route": "b", "repair_budget": 2},
            "isolation_evidence_refs": ["qa-run:pair"],
            "actor": "vp_agents",
        },
    )
    assert invalid_trial.status_code == 422

    trial_response = client.post(
        f"/v1/improvement-programs/{program['id']}/trials",
        json={
            "candidate_evaluation_id": "eval-candidate",
            "candidate_change_ref": "github://boss/pull/301",
            "candidate_diff_summary": "Increase only the repair budget.",
            "changed_variable": "repair_budget",
            "control_configuration": {"model_route": "a", "repair_budget": 1},
            "candidate_configuration": {"model_route": "a", "repair_budget": 2},
            "isolation_evidence_refs": ["qa-run:control", "qa-run:candidate"],
            "actor": "vp_agents",
        },
    )
    assert trial_response.status_code == 201, trial_response.text
    trial = trial_response.json()
    assert trial["status"] == "candidate_wins"
    assert trial["verdict"]["overall_delta"] == 10

    promote_response = client.post(
        f"/v1/improvement-programs/trials/{trial['id']}/promote",
        json={"actor": "vp_agents", "risk_tier": 2},
    )
    assert promote_response.status_code == 201, promote_response.text
    promoted = promote_response.json()
    proposal_id = promoted["proposal"]["id"]
    assert promoted["trial"]["status"] == "proposed"
    assert promoted["proposal"]["status"] == "proposed"

    too_early = client.post(
        f"/v1/improvement-programs/trials/{trial['id']}/adopt-baseline",
        json={"actor": "vp_agents"},
    )
    assert too_early.status_code == 422

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE self_improvement_proposals SET status='rolled_out' WHERE id=:id"
            ),
            {"id": proposal_id},
        )

    sweep_response = client.post(
        "/v1/qa/benchmark-apps/tuning-sweep",
        json={"project_id": project_id, "actor": "qa_benchmark_scheduler"},
    )
    assert sweep_response.status_code == 200, sweep_response.text
    assert sweep_response.json()["baseline_reconciliation"]["adopted_count"] == 1

    programs_response = client.get(
        "/v1/improvement-programs", params={"project_id": project_id}
    )
    trials_response = client.get(f"/v1/improvement-programs/{program['id']}/trials")
    assert (
        programs_response.json()["items"][0]["baseline_evaluation_id"]
        == "eval-candidate"
    )
    assert trials_response.json()["items"][0]["status"] == "adopted"

    with engine.begin() as connection:
        event_types = set(
            connection.execute(
                text(
                    """
                    SELECT event_type FROM events
                    WHERE entity_id IN (:program_id, :trial_id)
                    """
                ),
                {"program_id": program["id"], "trial_id": trial["id"]},
            ).scalars()
        )
    assert "improvement_program.created" in event_types
    assert "improvement_trial.evaluated" in event_types
    assert "improvement_trial.promoted" in event_types
    assert "improvement_program.baseline_adopted" in event_types
