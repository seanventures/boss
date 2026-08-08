"""Measured, governed feedback loops for BOSS workflow improvement.

The loop is deliberately split into measurement, comparison, and promotion.  It
never generates a change, applies a candidate, or treats correlation as causation.
A candidate must arrive as a traceable PR/config diff, survive a mechanically
checked single-variable comparison, and then enter the existing approval/canary/
staged-rollout state machine.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, json_hash, make_id


PROGRAM_STATUSES = {"active", "paused", "retired"}


def _row(connection: Connection, query: str, params: dict[str, Any]) -> dict[str, Any]:
    value = connection.execute(text(query), params).mappings().first()
    if not value:
        raise ValueError("Improvement program evidence was not found.")
    return dict(value)


def get_evaluation(
    connection: Connection, evaluation_id: str, *, for_update: bool = False
) -> dict[str, Any]:
    lock = "FOR UPDATE" if for_update else ""
    return _row(
        connection,
        f"""
        SELECT id, project_id, code_task_run_id, benchmark_id, scorecard_version,
               status, overall_score, observed_weight, report, recommendation,
               source_hash, evaluated_by, created_at, updated_at
        FROM qa_benchmark_evaluations
        WHERE id=:evaluation_id
        {lock}
        """,
        {"evaluation_id": evaluation_id},
    )


def get_program(
    connection: Connection, program_id: str, *, for_update: bool = False
) -> dict[str, Any]:
    lock = "FOR UPDATE" if for_update else ""
    return _row(
        connection,
        f"""
        SELECT id, project_id, name, objective, benchmark_id,
               baseline_evaluation_id, metric_contract, metric_contract_hash,
               minimum_evidence_weight, minimum_overall_delta,
               maximum_dimension_regression, hard_gates, status, created_by,
               created_at, updated_at
        FROM improvement_programs
        WHERE id=:program_id
        {lock}
        """,
        {"program_id": program_id},
    )


def get_trial(
    connection: Connection, trial_id: str, *, for_update: bool = False
) -> dict[str, Any]:
    lock = "FOR UPDATE" if for_update else ""
    return _row(
        connection,
        f"""
        SELECT id, project_id, program_id, control_evaluation_id,
               candidate_evaluation_id, candidate_change_ref,
               candidate_diff_summary, changed_variable, control_configuration,
               candidate_configuration, isolation_evidence_refs, status, verdict,
               verdict_hash, proposal_id, created_by, created_at, updated_at
        FROM improvement_trials
        WHERE id=:trial_id
        {lock}
        """,
        {"trial_id": trial_id},
    )


def list_programs(
    connection: Connection, *, project_id: str, limit: int = 50
) -> list[dict[str, Any]]:
    rows = (
        connection.execute(
            text(
                """
            SELECT id, project_id, name, objective, benchmark_id,
                   baseline_evaluation_id, metric_contract, metric_contract_hash,
                   minimum_evidence_weight, minimum_overall_delta,
                   maximum_dimension_regression, hard_gates, status, created_by,
                   created_at, updated_at
            FROM improvement_programs
            WHERE project_id=:project_id
            ORDER BY updated_at DESC
            LIMIT :limit
            """
            ),
            {"project_id": project_id, "limit": limit},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def list_trials(
    connection: Connection, *, program_id: str, limit: int = 50
) -> list[dict[str, Any]]:
    rows = (
        connection.execute(
            text(
                """
            SELECT id, project_id, program_id, control_evaluation_id,
                   candidate_evaluation_id, candidate_change_ref,
                   candidate_diff_summary, changed_variable, control_configuration,
                   candidate_configuration, isolation_evidence_refs, status, verdict,
                   verdict_hash, proposal_id, created_by, created_at, updated_at
            FROM improvement_trials
            WHERE program_id=:program_id
            ORDER BY updated_at DESC
            LIMIT :limit
            """
            ),
            {"program_id": program_id, "limit": limit},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def metric_contract(evaluation: dict[str, Any]) -> dict[str, Any]:
    report = evaluation.get("report") or {}
    scorecard = report.get("scorecard") or {}
    dimensions = scorecard.get("dimensions") or []
    return {
        "benchmark_id": evaluation["benchmark_id"],
        "scorecard_version": evaluation["scorecard_version"],
        "baselines": scorecard.get("baselines") or {},
        "dimensions": [
            {"id": item.get("id"), "weight": item.get("weight")}
            for item in dimensions
            if isinstance(item, dict) and item.get("id")
        ],
        "required_verification_commands": report.get("required_verification_commands")
        or [],
    }


def create_program(
    connection: Connection,
    *,
    project_id: str,
    name: str,
    objective: str,
    baseline_evaluation_id: str,
    actor: str,
    minimum_evidence_weight: int = 50,
    minimum_overall_delta: float = 1.0,
    maximum_dimension_regression: float = 0.0,
    hard_gates: dict[str, float] | None = None,
) -> dict[str, Any]:
    name = (name or "").strip()
    objective = (objective or "").strip()
    actor = (actor or "").strip()
    if not name or not objective or not actor:
        raise ValueError("name, objective, and actor are required.")
    if not 1 <= minimum_evidence_weight <= 100:
        raise ValueError("minimum_evidence_weight must be between 1 and 100.")
    if minimum_overall_delta < 0 or maximum_dimension_regression < 0:
        raise ValueError("comparison thresholds cannot be negative.")
    gates = hard_gates or {"functionality": 100.0}
    if not gates or any(
        float(value) < 0 or float(value) > 100 for value in gates.values()
    ):
        raise ValueError("hard_gates must contain scores between 0 and 100.")
    baseline = get_evaluation(connection, baseline_evaluation_id)
    if baseline["project_id"] != project_id:
        raise ValueError("Baseline evaluation belongs to a different project.")
    if int(baseline["observed_weight"]) < minimum_evidence_weight:
        raise ValueError("Baseline evaluation does not meet the evidence-weight floor.")
    contract = metric_contract(baseline)
    contract_hash = json_hash(contract)
    program_id = make_id("improg")
    connection.execute(
        text(
            """
            INSERT INTO improvement_programs (
                id, project_id, name, objective, benchmark_id,
                baseline_evaluation_id, metric_contract, metric_contract_hash,
                minimum_evidence_weight, minimum_overall_delta,
                maximum_dimension_regression, hard_gates, status, created_by
            ) VALUES (
                :id, :project_id, :name, :objective, :benchmark_id,
                :baseline_evaluation_id, CAST(:metric_contract AS JSONB),
                :metric_contract_hash, :minimum_evidence_weight,
                :minimum_overall_delta, :maximum_dimension_regression,
                CAST(:hard_gates AS JSONB), 'active', :actor
            )
            """
        ),
        {
            "id": program_id,
            "project_id": project_id,
            "name": name,
            "objective": objective,
            "benchmark_id": baseline["benchmark_id"],
            "baseline_evaluation_id": baseline_evaluation_id,
            "metric_contract": json.dumps(contract),
            "metric_contract_hash": contract_hash,
            "minimum_evidence_weight": minimum_evidence_weight,
            "minimum_overall_delta": minimum_overall_delta,
            "maximum_dimension_regression": maximum_dimension_regression,
            "hard_gates": json.dumps(gates),
            "actor": actor,
        },
    )
    emit_event(
        connection,
        project_id,
        "improvement_program.created",
        actor,
        "improvement_program",
        program_id,
        {
            "baseline_evaluation_id": baseline_evaluation_id,
            "benchmark_id": baseline["benchmark_id"],
            "metric_contract_hash": contract_hash,
        },
    )
    return get_program(connection, program_id)


def update_program_status(
    connection: Connection, *, program_id: str, status: str, actor: str
) -> dict[str, Any]:
    if status not in PROGRAM_STATUSES:
        raise ValueError("status must be active, paused, or retired.")
    program = get_program(connection, program_id, for_update=True)
    connection.execute(
        text(
            """
            UPDATE improvement_programs
            SET status=:status, updated_at=now()
            WHERE id=:program_id
            """
        ),
        {"program_id": program_id, "status": status},
    )
    emit_event(
        connection,
        program["project_id"],
        "improvement_program.status_changed",
        actor,
        "improvement_program",
        program_id,
        {"from_status": program["status"], "to_status": status},
    )
    return get_program(connection, program_id)


def _dimension_scores(evaluation: dict[str, Any]) -> dict[str, float | None]:
    scorecard = (evaluation.get("report") or {}).get("scorecard") or {}
    return {
        str(item["id"]): float(item["score"]) if item.get("score") is not None else None
        for item in scorecard.get("dimensions") or []
        if isinstance(item, dict) and item.get("id")
    }


def _changed_keys(control: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    return sorted(
        key
        for key in set(control) | set(candidate)
        if control.get(key) != candidate.get(key)
    )


def create_trial(
    connection: Connection,
    *,
    program_id: str,
    candidate_evaluation_id: str,
    candidate_change_ref: str,
    candidate_diff_summary: str,
    changed_variable: str,
    control_configuration: dict[str, Any],
    candidate_configuration: dict[str, Any],
    isolation_evidence_refs: list[str],
    actor: str,
) -> dict[str, Any]:
    program = get_program(connection, program_id, for_update=True)
    if program["status"] != "active":
        raise ValueError("Improvement program must be active to evaluate a trial.")
    candidate_change_ref = (candidate_change_ref or "").strip()
    candidate_diff_summary = (candidate_diff_summary or "").strip()
    changed_variable = (changed_variable or "").strip()
    actor = (actor or "").strip()
    evidence_refs = [item.strip() for item in isolation_evidence_refs if item.strip()]
    if not all((candidate_change_ref, candidate_diff_summary, changed_variable, actor)):
        raise ValueError(
            "candidate change, changed variable, diff summary, and actor are required."
        )
    if not control_configuration or not candidate_configuration:
        raise ValueError(
            "control_configuration and candidate_configuration are required."
        )
    changed_keys = _changed_keys(control_configuration, candidate_configuration)
    if changed_keys != [changed_variable]:
        raise ValueError(
            "Exactly one configuration variable must change and it must equal changed_variable."
        )
    if not evidence_refs:
        raise ValueError("At least one isolation evidence reference is required.")

    control = get_evaluation(connection, program["baseline_evaluation_id"])
    candidate = get_evaluation(connection, candidate_evaluation_id)
    if candidate["id"] == control["id"]:
        raise ValueError("Candidate evaluation must differ from the pinned control.")
    if candidate["project_id"] != program["project_id"]:
        raise ValueError("Candidate evaluation belongs to a different project.")
    candidate_contract = metric_contract(candidate)
    contract_hash = json_hash(candidate_contract)
    if contract_hash != program["metric_contract_hash"]:
        raise ValueError(
            "Candidate benchmark or scorecard contract differs from the pinned baseline."
        )

    reasons: list[str] = []
    outcome = "candidate_wins"
    minimum_weight = int(program["minimum_evidence_weight"])
    control_weight = int(control["observed_weight"])
    candidate_weight = int(candidate["observed_weight"])
    if candidate_weight < minimum_weight or candidate_weight < control_weight:
        outcome = "inconclusive"
        reasons.append(
            "Candidate evidence coverage is below the required control/evidence floor."
        )

    control_scores = _dimension_scores(control)
    candidate_scores = _dimension_scores(candidate)
    deltas: dict[str, float | None] = {}
    for dimension, control_score in control_scores.items():
        candidate_score = candidate_scores.get(dimension)
        deltas[dimension] = (
            round(candidate_score - control_score, 3)
            if control_score is not None and candidate_score is not None
            else None
        )
        if control_score is not None and candidate_score is None:
            outcome = "inconclusive"
            reasons.append(f"Candidate is missing observed {dimension} evidence.")

    if outcome != "inconclusive":
        for dimension, minimum in (program.get("hard_gates") or {}).items():
            score = candidate_scores.get(dimension)
            if score is None or score < float(minimum):
                outcome = "rejected"
                reasons.append(f"Hard gate {dimension}>={minimum} was not met.")
        max_regression = float(program["maximum_dimension_regression"])
        regressed = [
            dimension
            for dimension, delta in deltas.items()
            if delta is not None and delta < -max_regression
        ]
        if regressed:
            outcome = "rejected"
            reasons.append(
                "Observed dimensions regressed: " + ", ".join(regressed) + "."
            )
        control_overall = control.get("overall_score")
        candidate_overall = candidate.get("overall_score")
        if control_overall is None or candidate_overall is None:
            outcome = "inconclusive"
            reasons.append("Both overall scores must be observed.")
            overall_delta = None
        else:
            overall_delta = round(float(candidate_overall) - float(control_overall), 3)
            if overall_delta < float(program["minimum_overall_delta"]):
                outcome = "rejected"
                reasons.append(
                    "Overall improvement did not meet the program's minimum delta."
                )
    else:
        overall_delta = None

    if outcome == "candidate_wins":
        reasons.append(
            "Candidate met all pinned evidence, hard-gate, and regression thresholds."
        )
    verdict = {
        "outcome": outcome,
        "control_evaluation_id": control["id"],
        "candidate_evaluation_id": candidate["id"],
        "metric_contract_hash": program["metric_contract_hash"],
        "changed_variable": changed_variable,
        "changed_keys": changed_keys,
        "control_observed_weight": control_weight,
        "candidate_observed_weight": candidate_weight,
        "control_overall_score": control.get("overall_score"),
        "candidate_overall_score": candidate.get("overall_score"),
        "overall_delta": overall_delta,
        "dimension_deltas": deltas,
        "reasons": reasons,
    }
    verdict_hash = json_hash(verdict)
    trial_id = make_id("imtrial")
    connection.execute(
        text(
            """
            INSERT INTO improvement_trials (
                id, project_id, program_id, control_evaluation_id,
                candidate_evaluation_id, candidate_change_ref,
                candidate_diff_summary, changed_variable, control_configuration,
                candidate_configuration, isolation_evidence_refs, status, verdict,
                verdict_hash, created_by
            ) VALUES (
                :id, :project_id, :program_id, :control_evaluation_id,
                :candidate_evaluation_id, :candidate_change_ref,
                :candidate_diff_summary, :changed_variable,
                CAST(:control_configuration AS JSONB),
                CAST(:candidate_configuration AS JSONB),
                CAST(:isolation_evidence_refs AS JSONB), :status,
                CAST(:verdict AS JSONB), :verdict_hash, :actor
            )
            """
        ),
        {
            "id": trial_id,
            "project_id": program["project_id"],
            "program_id": program_id,
            "control_evaluation_id": control["id"],
            "candidate_evaluation_id": candidate["id"],
            "candidate_change_ref": candidate_change_ref,
            "candidate_diff_summary": candidate_diff_summary,
            "changed_variable": changed_variable,
            "control_configuration": json.dumps(control_configuration),
            "candidate_configuration": json.dumps(candidate_configuration),
            "isolation_evidence_refs": json.dumps(evidence_refs),
            "status": outcome,
            "verdict": json.dumps(verdict),
            "verdict_hash": verdict_hash,
            "actor": actor,
        },
    )
    emit_event(
        connection,
        program["project_id"],
        "improvement_trial.evaluated",
        actor,
        "improvement_trial",
        trial_id,
        {
            "program_id": program_id,
            "outcome": outcome,
            "verdict_hash": verdict_hash,
            "candidate_change_ref": candidate_change_ref,
        },
    )
    return get_trial(connection, trial_id)


def promote_trial(
    connection: Connection,
    *,
    trial_id: str,
    actor: str,
    risk_tier: int,
    self_improvement_service: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    trial = get_trial(connection, trial_id, for_update=True)
    if trial["status"] != "candidate_wins":
        raise ValueError(
            "Only a winning, unpromoted trial can create an improvement proposal."
        )
    program = get_program(connection, trial["program_id"])
    verdict = trial["verdict"] or {}
    evidence_refs = trial["isolation_evidence_refs"] or []
    proposal = self_improvement_service.create_proposal(
        connection,
        project_id=trial["project_id"],
        title=f"Promote measured improvement: {program['name']}",
        summary=(
            f"Trial {trial_id} compared pinned control {trial['control_evaluation_id']} "
            f"with candidate {trial['candidate_evaluation_id']} and recorded verdict "
            f"{trial['verdict_hash']}. The candidate may proceed only through approval, "
            "canary, staged rollout, and rollback controls."
        ),
        change_ref=trial["candidate_change_ref"],
        diff_summary=trial["candidate_diff_summary"],
        expected_impact=[
            f"Improve overall benchmark score by {verdict.get('overall_delta')} points.",
            f"Preserve the pinned metric contract {program['metric_contract_hash']}.",
        ],
        test_plan=[
            f"Reproduce improvement trial {trial_id} against the pinned baseline.",
            "Run invariant canary and eval suites before rollout.",
            "Reject any lower evidence coverage or hard-gate regression.",
        ],
        canary_scope=[
            f"Limit canary to benchmark {program['benchmark_id']} and the changed variable "
            f"{trial['changed_variable']}.",
            "Do not include production customer workloads before rollout approval.",
        ],
        rollback_plan=[
            f"Revert {trial['candidate_change_ref']} through normal change control.",
            f"Restore baseline evaluation {program['baseline_evaluation_id']} configuration.",
        ],
        success_metrics=[
            f"Overall delta remains at least {program['minimum_overall_delta']}.",
            "No observed dimension regresses beyond the configured threshold.",
            "All configured hard gates remain satisfied.",
            *[f"Preserve isolation evidence {ref}." for ref in evidence_refs],
        ],
        actor=actor,
        risk_tier=risk_tier,
    )
    connection.execute(
        text(
            """
            UPDATE improvement_trials
            SET status='proposed', proposal_id=:proposal_id, updated_at=now()
            WHERE id=:trial_id
            """
        ),
        {"trial_id": trial_id, "proposal_id": proposal["id"]},
    )
    emit_event(
        connection,
        trial["project_id"],
        "improvement_trial.promoted",
        actor,
        "improvement_trial",
        trial_id,
        {"proposal_id": proposal["id"], "verdict_hash": trial["verdict_hash"]},
    )
    return get_trial(connection, trial_id), proposal


def adopt_trial_baseline(
    connection: Connection, *, trial_id: str, actor: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    trial = get_trial(connection, trial_id, for_update=True)
    if trial["status"] != "proposed" or not trial.get("proposal_id"):
        raise ValueError(
            "Trial must have a governed proposal before baseline adoption."
        )
    proposal = _row(
        connection,
        "SELECT id, status FROM self_improvement_proposals WHERE id=:proposal_id FOR UPDATE",
        {"proposal_id": trial["proposal_id"]},
    )
    if proposal["status"] != "rolled_out":
        raise ValueError(
            "Baseline can move only after the proposal completed governed rollout."
        )
    program = get_program(connection, trial["program_id"], for_update=True)
    if program["baseline_evaluation_id"] != trial["control_evaluation_id"]:
        raise ValueError(
            "Program baseline changed after this trial; rerun against the current baseline."
        )
    candidate = get_evaluation(connection, trial["candidate_evaluation_id"])
    contract = metric_contract(candidate)
    contract_hash = json_hash(contract)
    if contract_hash != program["metric_contract_hash"]:
        raise ValueError(
            "Candidate metric contract no longer matches the program baseline."
        )
    connection.execute(
        text(
            """
            UPDATE improvement_programs
            SET baseline_evaluation_id=:evaluation_id,
                metric_contract=CAST(:metric_contract AS JSONB),
                metric_contract_hash=:metric_contract_hash,
                updated_at=now()
            WHERE id=:program_id
            """
        ),
        {
            "program_id": program["id"],
            "evaluation_id": candidate["id"],
            "metric_contract": json.dumps(contract),
            "metric_contract_hash": contract_hash,
        },
    )
    connection.execute(
        text(
            "UPDATE improvement_trials SET status='adopted', updated_at=now() WHERE id=:id"
        ),
        {"id": trial_id},
    )
    emit_event(
        connection,
        trial["project_id"],
        "improvement_program.baseline_adopted",
        actor,
        "improvement_program",
        program["id"],
        {
            "trial_id": trial_id,
            "proposal_id": trial["proposal_id"],
            "prior_baseline_evaluation_id": program["baseline_evaluation_id"],
            "baseline_evaluation_id": candidate["id"],
        },
    )
    return get_program(connection, program["id"]), get_trial(connection, trial_id)


def reconcile_program_baselines(
    connection: Connection,
    *,
    project_id: str | None = None,
    actor: str = "improvement_program_scheduler",
    limit: int = 100,
) -> dict[str, Any]:
    """Adopt candidates whose separately governed rollout is durably complete.

    This is safe on the worker idle loop: the query can only observe a proposal
    already marked ``rolled_out`` by the existing state machine. Adoption changes
    the measurement control, not provider/model/workflow configuration.
    """
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500.")
    scope = "AND trial.project_id=:project_id" if project_id else ""
    params: dict[str, Any] = {"limit": limit}
    if project_id:
        params["project_id"] = project_id
    trial_ids = (
        connection.execute(
            text(
                f"""
            SELECT trial.id
            FROM improvement_trials trial
            JOIN self_improvement_proposals proposal ON proposal.id=trial.proposal_id
            WHERE trial.status='proposed' AND proposal.status='rolled_out'
              {scope}
            ORDER BY trial.updated_at
            LIMIT :limit
            """
            ),
            params,
        )
        .scalars()
        .all()
    )
    adopted: list[str] = []
    superseded: list[str] = []
    for trial_id in trial_ids:
        trial_id = str(trial_id)
        trial = get_trial(connection, trial_id, for_update=True)
        program = get_program(connection, trial["program_id"], for_update=True)
        if program["baseline_evaluation_id"] != trial["control_evaluation_id"]:
            connection.execute(
                text(
                    """
                    UPDATE improvement_trials
                    SET status='superseded', updated_at=now()
                    WHERE id=:trial_id
                    """
                ),
                {"trial_id": trial_id},
            )
            emit_event(
                connection,
                trial["project_id"],
                "improvement_trial.superseded",
                actor,
                "improvement_trial",
                trial_id,
                {
                    "tested_baseline_evaluation_id": trial["control_evaluation_id"],
                    "current_baseline_evaluation_id": program["baseline_evaluation_id"],
                    "proposal_id": trial["proposal_id"],
                },
            )
            superseded.append(trial_id)
            continue
        adopt_trial_baseline(connection, trial_id=trial_id, actor=actor)
        adopted.append(trial_id)
    return {
        "checked_count": len(trial_ids),
        "adopted_count": len(adopted),
        "superseded_count": len(superseded),
        "items": adopted,
        "superseded_items": superseded,
    }
