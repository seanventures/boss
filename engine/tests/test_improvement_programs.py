from __future__ import annotations

import pytest

from boss_engine.services import improvement_programs as programs


class _Result:
    def mappings(self):
        return self

    def first(self):
        return None


class _Connection:
    def __init__(self):
        self.calls = []

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        return _Result()


def _evaluation(
    evaluation_id: str,
    *,
    overall: float,
    weight: int = 100,
    functionality: float = 100,
    speed: float = 80,
):
    return {
        "id": evaluation_id,
        "project_id": "project-1",
        "benchmark_id": "bugbox",
        "scorecard_version": "1.0",
        "observed_weight": weight,
        "overall_score": overall,
        "report": {
            "required_verification_commands": ["npm run verify"],
            "scorecard": {
                "version": "1.0",
                "baselines": {"tokens": 200_000},
                "dimensions": [
                    {
                        "id": "functionality",
                        "weight": 80,
                        "status": "scored",
                        "score": functionality,
                    },
                    {
                        "id": "speed",
                        "weight": 20,
                        "status": "scored",
                        "score": speed,
                    },
                ],
            },
        },
    }


def _program(control):
    contract = programs.metric_contract(control)
    return {
        "id": "improg-1",
        "project_id": "project-1",
        "name": "Coding workflow quality",
        "objective": "Improve quality without hiding cost or regressions.",
        "benchmark_id": "bugbox",
        "baseline_evaluation_id": control["id"],
        "metric_contract": contract,
        "metric_contract_hash": programs.json_hash(contract),
        "minimum_evidence_weight": 80,
        "minimum_overall_delta": 2.0,
        "maximum_dimension_regression": 0.0,
        "hard_gates": {"functionality": 100},
        "status": "active",
    }


def _patch_trial_dependencies(monkeypatch, control, candidate):
    program = _program(control)
    monkeypatch.setattr(programs, "get_program", lambda *_args, **_kwargs: program)
    monkeypatch.setattr(
        programs,
        "get_evaluation",
        lambda _connection, evaluation_id, **_kwargs: (
            control if evaluation_id == control["id"] else candidate
        ),
    )
    monkeypatch.setattr(programs, "emit_event", lambda *_args, **_kwargs: "evt-1")
    captured = {}

    def fake_get_trial(_connection, trial_id, **_kwargs):
        return {"id": trial_id, **captured}

    monkeypatch.setattr(programs, "get_trial", fake_get_trial)
    return program, captured


def test_trial_records_single_variable_candidate_winner(monkeypatch):
    control = _evaluation("eval-control", overall=88, speed=40)
    candidate = _evaluation("eval-candidate", overall=96, speed=80)
    _, captured = _patch_trial_dependencies(monkeypatch, control, candidate)
    connection = _Connection()

    original_execute = connection.execute

    def capture_execute(statement, params=None):
        if "INSERT INTO improvement_trials" in str(statement):
            values = params or {}
            captured.update(
                {
                    "status": values["status"],
                    "verdict_hash": values["verdict_hash"],
                    "candidate_change_ref": values["candidate_change_ref"],
                }
            )
        return original_execute(statement, params)

    connection.execute = capture_execute
    trial = programs.create_trial(
        connection,
        program_id="improg-1",
        candidate_evaluation_id="eval-candidate",
        candidate_change_ref="github://boss/pull/200",
        candidate_diff_summary="Increase the deterministic repair budget.",
        changed_variable="repair_budget",
        control_configuration={"model": "same", "repair_budget": 1},
        candidate_configuration={"model": "same", "repair_budget": 2},
        isolation_evidence_refs=["qa-run:control", "qa-run:candidate"],
        actor="vp_agents",
    )

    assert trial["status"] == "candidate_wins"
    assert trial["verdict_hash"]
    assert trial["candidate_change_ref"] == "github://boss/pull/200"


def test_trial_fails_closed_when_candidate_drops_evidence(monkeypatch):
    control = _evaluation("eval-control", overall=88, weight=100, speed=40)
    candidate = _evaluation("eval-candidate", overall=96, weight=80, speed=80)
    _, captured = _patch_trial_dependencies(monkeypatch, control, candidate)
    connection = _Connection()

    original_execute = connection.execute

    def capture_execute(statement, params=None):
        if "INSERT INTO improvement_trials" in str(statement):
            captured["status"] = (params or {})["status"]
        return original_execute(statement, params)

    connection.execute = capture_execute
    trial = programs.create_trial(
        connection,
        program_id="improg-1",
        candidate_evaluation_id="eval-candidate",
        candidate_change_ref="github://boss/pull/201",
        candidate_diff_summary="Candidate with less evidence.",
        changed_variable="model_route",
        control_configuration={"model_route": "a"},
        candidate_configuration={"model_route": "b"},
        isolation_evidence_refs=["qa-run:pair"],
        actor="vp_agents",
    )

    assert trial["status"] == "inconclusive"


def test_trial_rejects_multi_variable_experiment(monkeypatch):
    control = _evaluation("eval-control", overall=88)
    candidate = _evaluation("eval-candidate", overall=96)
    _patch_trial_dependencies(monkeypatch, control, candidate)

    with pytest.raises(ValueError, match="Exactly one configuration variable"):
        programs.create_trial(
            _Connection(),
            program_id="improg-1",
            candidate_evaluation_id="eval-candidate",
            candidate_change_ref="github://boss/pull/202",
            candidate_diff_summary="Changed two variables.",
            changed_variable="model_route",
            control_configuration={"model_route": "a", "repair_budget": 1},
            candidate_configuration={"model_route": "b", "repair_budget": 2},
            isolation_evidence_refs=["qa-run:pair"],
            actor="vp_agents",
        )


def test_baseline_cannot_move_before_governed_rollout(monkeypatch):
    trial = {
        "id": "trial-1",
        "project_id": "project-1",
        "program_id": "program-1",
        "candidate_evaluation_id": "eval-candidate",
        "proposal_id": "proposal-1",
        "status": "proposed",
    }
    monkeypatch.setattr(programs, "get_trial", lambda *_args, **_kwargs: trial)
    monkeypatch.setattr(
        programs,
        "_row",
        lambda *_args, **_kwargs: {"id": "proposal-1", "status": "canary_succeeded"},
    )

    with pytest.raises(ValueError, match="completed governed rollout"):
        programs.adopt_trial_baseline(
            _Connection(), trial_id="trial-1", actor="vp_agents"
        )
