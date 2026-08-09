"""Durable ground-truth and assurance runtime.

The runtime is intentionally deterministic. Models and agent groups may produce
candidate evidence, but only these measured records and policy gates may make a
claim authoritative or permit a governed production effect.
"""

from __future__ import annotations

import hashlib
import json
import re
from fnmatch import fnmatchcase
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .budget import BudgetGovernor
from .core import create_blocking_decision, emit_event, json_hash, make_id, now_utc
from .jobs import JobLeaseService, enqueue_job


class AssuranceError(RuntimeError):
    pass


class AssuranceNotFoundError(AssuranceError):
    pass


class AssuranceConflictError(AssuranceError):
    pass


class AssuranceEvidenceError(AssuranceError):
    pass


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return value


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _row(row: Any) -> dict[str, Any]:
    return {key: _iso(value) for key, value in dict(row).items()}


def _required_text(value: Any, name: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{name} is required.")
    return cleaned


def _required_list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} must contain at least one item.")
    return value


class AssuranceService:
    CLAIM_REQUIREMENTS = {
        "fixed": {"red_pre_fix", "green_post_change"},
        "solved": {"red_pre_fix", "green_post_change"},
        "caused_by": {"isolation_experiment"},
        "regressed": {"prior_passing_baseline", "current_failure"},
        "healthy": {"metric_observation"},
        "status": {"reproducible_measurement"},
    }
    CONTEXT_MARKERS = {
        "negated": ("do not", "don't", "never", "avoid", "not use", "must not"),
        "historical": (
            "previously",
            "historically",
            "used to",
            "was suggested",
            "old advice",
        ),
    }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.jobs = JobLeaseService(settings)
        self.budget = BudgetGovernor(settings)

    # ------------------------------------------------------------------
    # Ground-truth metrics and regression signals
    # ------------------------------------------------------------------
    def create_metric(
        self,
        connection: Connection,
        *,
        project_id: str,
        metric_key: str,
        metric_kind: str,
        unit: str,
        comparator: str,
        baseline: float,
        threshold: float,
        threshold_upper: Optional[float],
        cadence_seconds: int,
        alert_sla_seconds: int,
        owner_ref: str,
        evidence_query: dict[str, Any],
        metadata: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        metric_key = _required_text(metric_key, "metric_key")
        if not evidence_query:
            raise ValueError("evidence_query must be a non-empty object.")
        if cadence_seconds <= 0 or alert_sla_seconds <= 0:
            raise ValueError("cadence_seconds and alert_sla_seconds must be positive.")
        existing = (
            connection.execute(
                text(
                    "SELECT * FROM ground_truth_metric_definitions "
                    "WHERE project_id=:p AND metric_key=:k AND status='active'"
                ),
                {"p": project_id, "k": metric_key},
            )
            .mappings()
            .first()
        )
        if existing:
            raise AssuranceConflictError(
                f"Metric '{metric_key}' already has an active version."
            )
        version = int(
            connection.execute(
                text(
                    "SELECT COALESCE(max(version),0)+1 FROM ground_truth_metric_definitions "
                    "WHERE project_id=:p AND metric_key=:k"
                ),
                {"p": project_id, "k": metric_key},
            ).scalar_one()
        )
        metric_id = make_id("gtm")
        row = (
            connection.execute(
                text(
                    """
                INSERT INTO ground_truth_metric_definitions (
                  id,project_id,metric_key,version,metric_kind,unit,comparator,
                  baseline,threshold,threshold_upper,cadence_seconds,
                  alert_sla_seconds,owner_ref,evidence_query,metadata,created_by
                ) VALUES (
                  :id,:p,:k,:v,:kind,:unit,:comparator,:baseline,:threshold,
                  :upper,:cadence,:sla,:owner,CAST(:query AS JSONB),
                  CAST(:metadata AS JSONB),:actor
                ) RETURNING *
                """
                ),
                {
                    "id": metric_id,
                    "p": project_id,
                    "k": metric_key,
                    "v": version,
                    "kind": metric_kind,
                    "unit": _required_text(unit, "unit"),
                    "comparator": comparator,
                    "baseline": baseline,
                    "threshold": threshold,
                    "upper": threshold_upper,
                    "cadence": cadence_seconds,
                    "sla": alert_sla_seconds,
                    "owner": _required_text(owner_ref, "owner_ref"),
                    "query": json.dumps(evidence_query),
                    "metadata": json.dumps(metadata or {}),
                    "actor": actor,
                },
            )
            .mappings()
            .one()
        )
        emit_event(
            connection,
            project_id,
            "ground_truth.metric_created",
            actor,
            "ground_truth_metric_definition",
            metric_id,
            {"metric_key": metric_key, "version": version},
        )
        job = self._enqueue_metric_evaluation(
            connection,
            project_id=project_id,
            metric=dict(row),
            dedupe_key=f"ground-truth:{metric_id}:initial",
        )
        return {**_row(row), "evaluation_job_id": job["id"]}

    @staticmethod
    def _enqueue_metric_evaluation(
        connection: Connection,
        *,
        project_id: str,
        metric: dict[str, Any],
        dedupe_key: str,
    ) -> dict[str, Any]:
        return enqueue_job(
            connection,
            project_id,
            "ground_truth.evaluate",
            {
                "metric_definition_id": metric["id"],
                "metric_definition_version": int(metric["version"]),
                "metric_key": metric["metric_key"],
                "evidence_query": metric["evidence_query"],
            },
            priority=30,
            max_attempts=3,
            dedupe_key=dedupe_key,
            workload_class="verification",
            data_boundary="internal",
        )

    def schedule_due_metric_evaluations(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        force: bool = False,
        metric_definition_id: Optional[str] = None,
    ) -> dict[str, Any]:
        metric_filter = (
            " AND m.id=:metric" if metric_definition_id is not None else ""
        )
        metrics = connection.execute(
            text(
                f"""
                SELECT m.*,
                  (SELECT max(observed_at) FROM ground_truth_observations o
                   WHERE o.metric_definition_id=m.id) AS last_observed_at
                FROM ground_truth_metric_definitions m
                WHERE m.project_id=:p AND m.status='active'
                  {metric_filter}
                ORDER BY m.metric_key
                FOR UPDATE
                """
            ),
            {"p": project_id, "metric": metric_definition_id},
        ).mappings().all()
        now = now_utc()
        queued: list[dict[str, Any]] = []
        overdue: list[dict[str, Any]] = []
        for raw in metrics:
            metric = dict(raw)
            last = metric.get("last_observed_at") or metric["created_at"]
            age_seconds = max(0, int((now - last).total_seconds()))
            due = force or metric.get("last_observed_at") is None or age_seconds >= int(
                metric["cadence_seconds"]
            )
            active_job = connection.execute(
                text(
                    """
                    SELECT id FROM jobs
                    WHERE project_id=:p AND kind='ground_truth.evaluate'
                      AND payload->>'metric_definition_id'=:metric
                      AND status IN ('queued','failed_retryable','leased')
                      AND (status <> 'leased' OR lease_expires_at > now())
                    LIMIT 1
                    """
                ),
                {"p": project_id, "metric": metric["id"]},
            ).scalar()
            if due and not active_job:
                due_at = last + timedelta(seconds=int(metric["cadence_seconds"]))
                job = self._enqueue_metric_evaluation(
                    connection,
                    project_id=project_id,
                    metric=metric,
                    dedupe_key=f"ground-truth:{metric['id']}:{int(due_at.timestamp())}",
                )
                if job["status"] in {"queued", "failed_retryable"}:
                    queued.append(
                        {"metric_definition_id": metric["id"], "job_id": job["id"]}
                    )
            if age_seconds < int(metric["alert_sla_seconds"]):
                continue
            pending_decision = connection.execute(
                text(
                    """
                    SELECT id FROM decisions
                    WHERE project_id=:p AND status='pending'
                      AND recommendation->>'metric_definition_id'=:metric
                      AND recommendation->>'action'='restore_metric_evaluator'
                    LIMIT 1
                    """
                ),
                {"p": project_id, "metric": metric["id"]},
            ).scalar()
            if not pending_decision:
                pending_decision = create_blocking_decision(
                    connection,
                    project_id,
                    None,
                    f"Ground-truth evaluator for '{metric['metric_key']}' is overdue",
                    {
                        "action": "restore_metric_evaluator",
                        "metric_definition_id": metric["id"],
                        "owner_ref": metric["owner_ref"],
                    },
                    {
                        "age_seconds": age_seconds,
                        "cadence_seconds": metric["cadence_seconds"],
                        "alert_sla_seconds": metric["alert_sla_seconds"],
                    },
                    "ground_truth_evaluator_overdue",
                    risk_tier=2,
                )
                emit_event(
                    connection,
                    project_id,
                    "ground_truth.evaluator_overdue",
                    actor,
                    "ground_truth_metric_definition",
                    metric["id"],
                    {"decision_id": pending_decision, "age_seconds": age_seconds},
                )
            overdue.append(
                {
                    "metric_definition_id": metric["id"],
                    "decision_id": pending_decision,
                    "age_seconds": age_seconds,
                }
            )
        return {
            "project_id": project_id,
            "queued": queued,
            "overdue": overdue,
            "evaluated_metric_count": len(metrics),
        }

    def list_metrics(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = (
            connection.execute(
                text(
                    """
                SELECT m.*,
                  o.id AS last_observation_id,
                  o.value AS current_value,
                  o.status AS current_status,
                  o.evidence AS current_evidence,
                  o.observed_at AS last_observed_at,
                  good.id AS last_good_observation_id,
                  good.value AS last_good_value,
                  good.observed_at AS last_good_observed_at,
                  history.trend,
                  s.id AS regression_signal_id,
                  s.status AS regression_status,
                  s.breach_started_at,
                  CASE WHEN s.status IN ('open','acknowledged')
                    THEN EXTRACT(EPOCH FROM (now()-s.breach_started_at))::bigint
                    ELSE 0 END AS breach_duration_seconds
                FROM ground_truth_metric_definitions m
                LEFT JOIN LATERAL (
                  SELECT * FROM ground_truth_observations o
                  WHERE o.metric_definition_id=m.id
                  ORDER BY o.observed_at DESC LIMIT 1
                ) o ON true
                LEFT JOIN LATERAL (
                  SELECT id,value,observed_at FROM ground_truth_observations good
                  WHERE good.metric_definition_id=m.id AND good.status='healthy'
                  ORDER BY good.observed_at DESC LIMIT 1
                ) good ON true
                LEFT JOIN LATERAL (
                  SELECT jsonb_agg(jsonb_build_object(
                    'value',sample.value,'status',sample.status,
                    'observed_at',sample.observed_at
                  ) ORDER BY sample.observed_at) AS trend
                  FROM (
                    SELECT value,status,observed_at
                    FROM ground_truth_observations recent
                    WHERE recent.metric_definition_id=m.id
                    ORDER BY observed_at DESC LIMIT 20
                  ) sample
                ) history ON true
                LEFT JOIN LATERAL (
                  SELECT * FROM regression_signals s
                  WHERE s.metric_definition_id=m.id
                  ORDER BY s.created_at DESC LIMIT 1
                ) s ON true
                WHERE m.project_id=:p AND m.status='active'
                ORDER BY m.metric_key
                """
                ),
                {"p": project_id},
            )
            .mappings()
            .all()
        )
        items = []
        now = now_utc()
        for row in rows:
            item = _row(row)
            observed = row["last_observed_at"]
            if observed is None:
                age_seconds = max(0, int((now - row["created_at"]).total_seconds()))
                health_status = "pending" if age_seconds < row["alert_sla_seconds"] else "stale"
            else:
                age_seconds = max(0, int((now - observed).total_seconds()))
                health_status = (
                    "stale"
                    if age_seconds > int(row["cadence_seconds"])
                    else row["current_status"]
                )
            item["observation_age_seconds"] = age_seconds
            item["evaluation_overdue"] = age_seconds > int(row["cadence_seconds"])
            item["alert_sla_breached"] = age_seconds > int(row["alert_sla_seconds"])
            item["health_status"] = health_status
            item["trend"] = item.get("trend") or []
            items.append(item)
        return items

    @staticmethod
    def _metric_status(metric: dict[str, Any], value: float) -> str:
        comparator = metric["comparator"]
        threshold = float(metric["threshold"])
        if comparator == "gte":
            healthy = value >= threshold
        elif comparator == "lte":
            healthy = value <= threshold
        elif comparator == "eq":
            healthy = value == threshold
        else:
            upper = metric.get("threshold_upper")
            if upper is None:
                raise ValueError("between comparator requires threshold_upper.")
            healthy = threshold <= value <= float(upper)
        return "healthy" if healthy else "breached"

    def record_observation(
        self,
        connection: Connection,
        *,
        project_id: str,
        metric_definition_id: str,
        value: float,
        evidence: dict[str, Any],
        evaluator_job_id: str,
        evaluator_worker_id: str,
        lease_token: str,
        dedupe_key: str,
    ) -> dict[str, Any]:
        if not evidence:
            raise AssuranceEvidenceError(
                "A metric observation requires supporting evidence."
            )
        existing = (
            connection.execute(
                text(
                    "SELECT * FROM ground_truth_observations "
                    "WHERE project_id=:p AND dedupe_key=:d"
                ),
                {"p": project_id, "d": dedupe_key},
            )
            .mappings()
            .first()
        )
        if existing:
            if (
                existing["metric_definition_id"] != metric_definition_id
                or existing["evaluator_job_id"] != evaluator_job_id
                or existing["evaluator_worker_id"] != evaluator_worker_id
            ):
                raise AssuranceConflictError(
                    "Observation dedupe key belongs to another evaluator execution."
                )
            return {"observation": _row(existing), "duplicate": True, "signal": None}
        leased_job = self.jobs.require_active_lease(
            connection, evaluator_worker_id, evaluator_job_id, lease_token
        )
        if leased_job["project_id"] != project_id:
            raise AssuranceConflictError("Evaluator job belongs to another project.")
        if leased_job["kind"] != "ground_truth.evaluate" or (
            leased_job.get("payload") or {}
        ).get("metric_definition_id") != metric_definition_id:
            raise AssuranceConflictError(
                "Evaluator job is not bound to this metric definition."
            )
        metric_row = (
            connection.execute(
                text(
                    "SELECT * FROM ground_truth_metric_definitions "
                    "WHERE id=:id AND project_id=:p AND status='active' FOR UPDATE"
                ),
                {"id": metric_definition_id, "p": project_id},
            )
            .mappings()
            .first()
        )
        if not metric_row:
            raise AssuranceNotFoundError("Active ground-truth metric not found.")
        metric = dict(metric_row)
        status = self._metric_status(metric, float(value))
        observation_id = make_id("gto")
        row = (
            connection.execute(
                text(
                    """
                INSERT INTO ground_truth_observations (
                  id,project_id,metric_definition_id,metric_definition_version,
                  value,status,evaluator_job_id,evaluator_worker_id,lease_evidence,
                  evidence,dedupe_key
                ) VALUES (
                  :id,:p,:metric,:version,:value,:status,:job,:worker,
                  CAST(:lease AS JSONB),CAST(:evidence AS JSONB),:dedupe
                ) RETURNING *
                """
                ),
                {
                    "id": observation_id,
                    "p": project_id,
                    "metric": metric_definition_id,
                    "version": metric["version"],
                    "value": value,
                    "status": status,
                    "job": evaluator_job_id,
                    "worker": evaluator_worker_id,
                    "lease": json.dumps(
                        {
                            "job_id": evaluator_job_id,
                            "worker_id": evaluator_worker_id,
                            "heartbeat_at": _iso(leased_job.get("heartbeat_at")),
                            "lease_expires_at": _iso(
                                leased_job.get("lease_expires_at")
                            ),
                        }
                    ),
                    "evidence": json.dumps(evidence),
                    "dedupe": _required_text(dedupe_key, "dedupe_key"),
                },
            )
            .mappings()
            .one()
        )
        signal = self._update_regression_signal(
            connection,
            project_id=project_id,
            metric=metric,
            observation=_row(row),
            actor=evaluator_worker_id,
        )
        emit_event(
            connection,
            project_id,
            "ground_truth.observation_recorded",
            evaluator_worker_id,
            "ground_truth_observation",
            observation_id,
            {
                "metric_definition_id": metric_definition_id,
                "status": status,
                "value": value,
            },
        )
        self.jobs.complete(
            connection,
            evaluator_worker_id,
            evaluator_job_id,
            lease_token,
            {"metric_definition_id": metric_definition_id, "observation_id": observation_id},
        )
        return {"observation": _row(row), "duplicate": False, "signal": signal}

    def _update_regression_signal(
        self,
        connection: Connection,
        *,
        project_id: str,
        metric: dict[str, Any],
        observation: dict[str, Any],
        actor: str,
    ) -> Optional[dict[str, Any]]:
        active = (
            connection.execute(
                text(
                    """
                SELECT * FROM regression_signals
                WHERE project_id=:p AND metric_definition_id=:m
                  AND status IN ('open','acknowledged')
                ORDER BY created_at DESC LIMIT 1 FOR UPDATE
                """
                ),
                {"p": project_id, "m": metric["id"]},
            )
            .mappings()
            .first()
        )
        if observation["status"] == "breached":
            if active:
                updated = (
                    connection.execute(
                        text(
                            "UPDATE regression_signals SET latest_observation_id=:o,updated_at=now() "
                            "WHERE id=:id RETURNING *"
                        ),
                        {"o": observation["id"], "id": active["id"]},
                    )
                    .mappings()
                    .one()
                )
                return _row(updated)
            decision_id = create_blocking_decision(
                connection,
                project_id,
                None,
                f"Ground-truth metric '{metric['metric_key']}' breached its threshold",
                {
                    "action": "investigate_regression",
                    "metric_definition_id": metric["id"],
                    "observation_id": observation["id"],
                    "owner_ref": metric["owner_ref"],
                },
                {
                    "baseline": metric["baseline"],
                    "threshold": metric["threshold"],
                    "current_value": observation["value"],
                    "alert_sla_seconds": metric["alert_sla_seconds"],
                },
                "ground_truth_threshold_breach",
                risk_tier=2,
            )
            signal_id = make_id("reg")
            created = (
                connection.execute(
                    text(
                        """
                    INSERT INTO regression_signals (
                      id,project_id,metric_definition_id,first_observation_id,
                      latest_observation_id,decision_id,dedupe_key,breach_started_at
                    ) VALUES (:id,:p,:m,:o,:o,:decision,:dedupe,:started) RETURNING *
                    """
                    ),
                    {
                        "id": signal_id,
                        "p": project_id,
                        "m": metric["id"],
                        "o": observation["id"],
                        "decision": decision_id,
                        "dedupe": f"regression:{metric['id']}:{observation['id']}",
                        "started": observation["observed_at"],
                    },
                )
                .mappings()
                .one()
            )
            emit_event(
                connection,
                project_id,
                "ground_truth.regression_opened",
                actor,
                "regression_signal",
                signal_id,
                {"metric_definition_id": metric["id"], "decision_id": decision_id},
            )
            return _row(created)
        if active:
            recovered = (
                connection.execute(
                    text(
                        """
                    UPDATE regression_signals
                    SET status='recovered',latest_observation_id=:o,
                        recovered_at=now(),updated_at=now()
                    WHERE id=:id RETURNING *
                    """
                    ),
                    {"o": observation["id"], "id": active["id"]},
                )
                .mappings()
                .one()
            )
            emit_event(
                connection,
                project_id,
                "ground_truth.regression_recovered",
                actor,
                "regression_signal",
                active["id"],
                {"observation_id": observation["id"]},
            )
            return _row(recovered)
        return None

    def change_metric_baseline(
        self,
        connection: Connection,
        *,
        project_id: str,
        metric_definition_id: str,
        baseline: float,
        threshold: float,
        threshold_upper: Optional[float],
        reason: str,
        actor: str,
        approved_decision_id: Optional[str] = None,
    ) -> dict[str, Any]:
        reason = _required_text(reason, "reason")
        current = (
            connection.execute(
                text(
                    "SELECT * FROM ground_truth_metric_definitions "
                    "WHERE id=:id AND project_id=:p AND status='active' FOR UPDATE"
                ),
                {"id": metric_definition_id, "p": project_id},
            )
            .mappings()
            .first()
        )
        if not current:
            raise AssuranceNotFoundError("Active ground-truth metric not found.")
        open_signal = connection.execute(
            text(
                "SELECT id FROM regression_signals WHERE project_id=:p "
                "AND metric_definition_id=:m AND status IN ('open','acknowledged') LIMIT 1"
            ),
            {"p": project_id, "m": metric_definition_id},
        ).scalar()
        if open_signal:
            approved = None
            if approved_decision_id:
                approved = connection.execute(
                    text(
                        "SELECT id FROM decisions WHERE id=:id AND project_id=:p "
                        "AND status='approved'"
                    ),
                    {"id": approved_decision_id, "p": project_id},
                ).scalar()
            if not approved:
                raise AssuranceConflictError(
                    "A degraded metric cannot normalize its baseline without an approved decision."
                )
        old = dict(current)
        connection.execute(
            text(
                "UPDATE ground_truth_metric_definitions "
                "SET status='superseded',updated_at=now() WHERE id=:id"
            ),
            {"id": metric_definition_id},
        )
        new_id = make_id("gtm")
        new_row = (
            connection.execute(
                text(
                    """
                INSERT INTO ground_truth_metric_definitions (
                  id,project_id,metric_key,version,metric_kind,unit,comparator,
                  baseline,threshold,threshold_upper,cadence_seconds,
                  alert_sla_seconds,owner_ref,evidence_query,metadata,created_by
                ) VALUES (
                  :id,:p,:k,:v,:kind,:unit,:comparator,:baseline,:threshold,
                  :upper,:cadence,:sla,:owner,CAST(:query AS JSONB),
                  CAST(:metadata AS JSONB),:actor
                ) RETURNING *
                """
                ),
                {
                    "id": new_id,
                    "p": project_id,
                    "k": old["metric_key"],
                    "v": int(old["version"]) + 1,
                    "kind": old["metric_kind"],
                    "unit": old["unit"],
                    "comparator": old["comparator"],
                    "baseline": baseline,
                    "threshold": threshold,
                    "upper": threshold_upper,
                    "cadence": old["cadence_seconds"],
                    "sla": old["alert_sla_seconds"],
                    "owner": old["owner_ref"],
                    "query": json.dumps(old["evidence_query"]),
                    "metadata": json.dumps(
                        {
                            **(old.get("metadata") or {}),
                            "baseline_change_reason": reason,
                        }
                    ),
                    "actor": actor,
                },
            )
            .mappings()
            .one()
        )
        emit_event(
            connection,
            project_id,
            "ground_truth.baseline_changed",
            actor,
            "ground_truth_metric_definition",
            new_id,
            {
                "supersedes": metric_definition_id,
                "reason": reason,
                "approved_decision_id": approved_decision_id,
            },
        )
        return _row(new_row)

    # ------------------------------------------------------------------
    # Settled conclusions and pre-publication tripwire
    # ------------------------------------------------------------------
    def create_conclusion(
        self,
        connection: Connection,
        *,
        project_id: str,
        conclusion_key: str,
        conclusion: str,
        scope: dict[str, Any],
        owner_ref: str,
        tell_patterns: list[Any],
        cause_menu: list[Any],
        evidence_refs: list[Any],
        reopen_conditions: list[Any],
        review_at: Optional[datetime],
        expires_at: Optional[datetime],
        actor: str,
    ) -> dict[str, Any]:
        conclusion_key = _required_text(conclusion_key, "conclusion_key")
        _required_list(tell_patterns, "tell_patterns")
        _required_list(cause_menu, "cause_menu")
        _required_list(evidence_refs, "evidence_refs")
        prior = (
            connection.execute(
                text(
                    "SELECT * FROM settled_conclusions WHERE project_id=:p "
                    "AND conclusion_key=:k AND status='current' FOR UPDATE"
                ),
                {"p": project_id, "k": conclusion_key},
            )
            .mappings()
            .first()
        )
        version = 1
        if prior:
            version = int(prior["version"]) + 1
            connection.execute(
                text("UPDATE settled_conclusions SET status='superseded' WHERE id=:id"),
                {"id": prior["id"]},
            )
        conclusion_id = make_id("conclusion")
        row = (
            connection.execute(
                text(
                    """
                INSERT INTO settled_conclusions (
                  id,project_id,conclusion_key,version,conclusion,scope,owner_ref,
                  tell_patterns,cause_menu,evidence_refs,reopen_conditions,
                  review_at,expires_at,created_by
                ) VALUES (
                  :id,:p,:k,:v,:conclusion,CAST(:scope AS JSONB),:owner,
                  CAST(:tells AS JSONB),CAST(:causes AS JSONB),
                  CAST(:evidence AS JSONB),CAST(:reopen AS JSONB),
                  :review_at,:expires_at,:actor
                ) RETURNING *
                """
                ),
                {
                    "id": conclusion_id,
                    "p": project_id,
                    "k": conclusion_key,
                    "v": version,
                    "conclusion": _required_text(conclusion, "conclusion"),
                    "scope": json.dumps(scope or {}),
                    "owner": _required_text(owner_ref, "owner_ref"),
                    "tells": json.dumps(tell_patterns),
                    "causes": json.dumps(cause_menu),
                    "evidence": json.dumps(evidence_refs),
                    "reopen": json.dumps(reopen_conditions or []),
                    "review_at": review_at,
                    "expires_at": expires_at,
                    "actor": actor,
                },
            )
            .mappings()
            .one()
        )
        emit_event(
            connection,
            project_id,
            "ground_truth.conclusion_published",
            actor,
            "settled_conclusion",
            conclusion_id,
            {"conclusion_key": conclusion_key, "version": version},
        )
        return _row(row)

    def list_conclusions(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = (
            connection.execute(
                text(
                    "SELECT * FROM settled_conclusions WHERE project_id=:p "
                    "ORDER BY conclusion_key,version DESC"
                ),
                {"p": project_id},
            )
            .mappings()
            .all()
        )
        return [_row(row) for row in rows]

    @classmethod
    def _classify_context(cls, content: str, start: int, end: int) -> str:
        prefix = content[max(0, start - 80) : start].lower()
        line_start = content.rfind("\n", 0, start) + 1
        line_end = content.find("\n", end)
        if line_end < 0:
            line_end = len(content)
        line = content[line_start:line_end].strip()
        if line.startswith((">", "```", '"', "'")) or (
            start > 0 and content[start - 1] in {'"', "'", "`"}
        ):
            return "quoted"
        for kind, markers in cls.CONTEXT_MARKERS.items():
            if any(marker in prefix for marker in markers):
                return kind
        return "actionable"

    @staticmethod
    def _pattern_matches(content: str, raw: Any) -> list[tuple[str, int, int]]:
        if isinstance(raw, dict):
            pattern = _required_text(raw.get("pattern"), "tell_patterns[].pattern")
            kind = str(raw.get("kind") or "phrase")
        else:
            pattern = _required_text(raw, "tell_patterns[]")
            kind = "phrase"
        if kind == "regex":
            try:
                return [
                    (pattern, match.start(), match.end())
                    for match in re.finditer(pattern, content, re.I)
                ]
            except re.error as error:
                raise ValueError(f"Invalid tell-pattern regex: {error}") from error
        normalized = re.escape(pattern).replace(r"\ ", r"\s+")
        return [
            (pattern, match.start(), match.end())
            for match in re.finditer(normalized, content, re.I)
        ]

    def scan_publication(
        self,
        connection: Connection,
        *,
        project_id: str,
        publication_boundary: str,
        content: str,
        dedupe_key: str,
    ) -> dict[str, Any]:
        content = _required_text(content, "content")
        existing = (
            connection.execute(
                text(
                    "SELECT * FROM closed_question_matches "
                    "WHERE project_id=:p AND dedupe_key=:d"
                ),
                {"p": project_id, "d": dedupe_key},
            )
            .mappings()
            .first()
        )
        if existing:
            return {
                "allowed": existing["status"] != "blocked",
                "match": _row(existing),
                "duplicate": True,
            }
        conclusions = (
            connection.execute(
                text(
                    """
                SELECT * FROM settled_conclusions
                WHERE project_id=:p AND status='current'
                  AND effective_at <= now()
                  AND (expires_at IS NULL OR expires_at > now())
                ORDER BY effective_at DESC
                """
                ),
                {"p": project_id},
            )
            .mappings()
            .all()
        )
        hits: list[dict[str, Any]] = []
        selected: Optional[dict[str, Any]] = None
        selected_context: Optional[dict[str, Any]] = None
        for conclusion_row in conclusions:
            conclusion = dict(conclusion_row)
            scope = _json(conclusion.get("scope"), {})
            boundaries = scope.get("publication_boundaries") or []
            if (
                boundaries
                and publication_boundary not in boundaries
                and "*" not in boundaries
            ):
                continue
            for raw in _json(conclusion.get("tell_patterns"), []):
                for pattern, start, end in self._pattern_matches(content, raw):
                    classification = self._classify_context(content, start, end)
                    hit = {
                        "settled_conclusion_id": conclusion["id"],
                        "pattern": pattern,
                        "start": start,
                        "end": end,
                        "classification": classification,
                    }
                    hits.append(hit)
                    if selected_context is None:
                        selected_context = conclusion
                    if classification == "actionable" and selected is None:
                        selected = conclusion
        if not hits:
            return {"allowed": True, "match": None, "duplicate": False}
        if selected is None:
            selected = selected_context
        assert selected is not None
        actionable = any(hit["classification"] == "actionable" for hit in hits)
        classification = "actionable" if actionable else hits[0]["classification"]
        decision_id = None
        status = "blocked" if actionable else "allowed_context"
        stop_response = {
            "message": "STOP: this is a closed question."
            if actionable
            else "Context-only reference allowed.",
            "settled_conclusion": selected["conclusion"],
            "cause_menu": _json(selected["cause_menu"], []),
            "evidence_refs": _json(selected["evidence_refs"], []),
            "reopen_conditions": _json(selected["reopen_conditions"], []),
        }
        if actionable:
            decision_id = create_blocking_decision(
                connection,
                project_id,
                None,
                "A publication attempted to repeat a settled, closed-question prior",
                {
                    "action": "use_settled_conclusion_or_reopen_with_new_evidence",
                    "settled_conclusion_id": selected["id"],
                },
                {
                    "publication_boundary": publication_boundary,
                    "matched_patterns": hits,
                },
                "closed_question_tripwire",
                risk_tier=2,
            )
        match_id = make_id("cqmatch")
        row = (
            connection.execute(
                text(
                    """
                INSERT INTO closed_question_matches (
                  id,project_id,settled_conclusion_id,settled_conclusion_version,
                  publication_boundary,content_hash,matched_patterns,
                  context_classification,status,stop_response,decision_id,dedupe_key
                ) VALUES (
                  :id,:p,:conclusion,:version,:boundary,:hash,CAST(:hits AS JSONB),
                  :classification,:status,CAST(:response AS JSONB),:decision,:dedupe
                ) RETURNING *
                """
                ),
                {
                    "id": match_id,
                    "p": project_id,
                    "conclusion": selected["id"],
                    "version": selected["version"],
                    "boundary": publication_boundary,
                    "hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "hits": json.dumps(hits),
                    "classification": classification,
                    "status": status,
                    "response": json.dumps(stop_response),
                    "decision": decision_id,
                    "dedupe": _required_text(dedupe_key, "dedupe_key"),
                },
            )
            .mappings()
            .one()
        )
        emit_event(
            connection,
            project_id,
            "ground_truth.closed_question_blocked"
            if actionable
            else "ground_truth.closed_question_context_allowed",
            "publication_guard",
            "closed_question_match",
            match_id,
            {"decision_id": decision_id, "publication_boundary": publication_boundary},
        )
        return {"allowed": not actionable, "match": _row(row), "duplicate": False}

    def reopen_conclusion(
        self,
        connection: Connection,
        *,
        project_id: str,
        conclusion_id: str,
        expected_version: int,
        reason: str,
        new_evidence_refs: list[Any],
        approved_decision_id: str,
        actor: str,
    ) -> dict[str, Any]:
        _required_text(reason, "reason")
        _required_list(new_evidence_refs, "new_evidence_refs")
        conclusion = (
            connection.execute(
                text(
                    "SELECT * FROM settled_conclusions WHERE id=:id AND project_id=:p "
                    "AND status='current' FOR UPDATE"
                ),
                {"id": conclusion_id, "p": project_id},
            )
            .mappings()
            .first()
        )
        if not conclusion:
            raise AssuranceNotFoundError("Current settled conclusion not found.")
        if int(conclusion["version"]) != expected_version:
            raise AssuranceConflictError("Settled conclusion version is stale.")
        approved = connection.execute(
            text(
                "SELECT id FROM decisions WHERE id=:id AND project_id=:p AND status='approved'"
            ),
            {"id": approved_decision_id, "p": project_id},
        ).scalar()
        if not approved:
            raise AssuranceConflictError(
                "Reopening a settled conclusion requires an approved decision."
            )
        row = (
            connection.execute(
                text(
                    "UPDATE settled_conclusions SET status='reopened' WHERE id=:id RETURNING *"
                ),
                {"id": conclusion_id},
            )
            .mappings()
            .one()
        )
        emit_event(
            connection,
            project_id,
            "ground_truth.conclusion_reopened",
            actor,
            "settled_conclusion",
            conclusion_id,
            {
                "reason": reason,
                "new_evidence_refs": new_evidence_refs,
                "decision_id": approved_decision_id,
            },
        )
        return _row(row)

    # ------------------------------------------------------------------
    # Assurance cases, typed claims, immutable evidence, and claim gates
    # ------------------------------------------------------------------
    def create_case(
        self,
        connection: Connection,
        *,
        project_id: str,
        subject_type: str,
        subject_ref: str,
        title: str,
        risk_tier: int,
        references: dict[str, Any],
        dedupe_key: str,
        actor: str,
    ) -> dict[str, Any]:
        existing = (
            connection.execute(
                text(
                    "SELECT * FROM assurance_cases WHERE project_id=:p AND dedupe_key=:d"
                ),
                {"p": project_id, "d": dedupe_key},
            )
            .mappings()
            .first()
        )
        if existing:
            return {**_row(existing), "duplicate": True}
        case_id = make_id("acase")
        fields = {
            "metric_refs": references.get("metric_refs") or [],
            "settled_conclusion_refs": references.get("settled_conclusion_refs") or [],
            "surface_refs": references.get("surface_refs") or [],
            "agent_group_run_refs": references.get("agent_group_run_refs") or [],
            "workflow_run_refs": references.get("workflow_run_refs") or [],
            "skill_invocation_refs": references.get("skill_invocation_refs") or [],
            "integration_connection_refs": references.get("integration_connection_refs")
            or [],
            "decision_refs": references.get("decision_refs") or [],
        }
        row = (
            connection.execute(
                text(
                    """
                INSERT INTO assurance_cases (
                  id,project_id,subject_type,subject_ref,title,risk_tier,
                  metric_refs,settled_conclusion_refs,surface_refs,
                  agent_group_run_refs,workflow_run_refs,skill_invocation_refs,
                  integration_connection_refs,decision_refs,dedupe_key,created_by
                ) VALUES (
                  :id,:p,:subject_type,:subject_ref,:title,:risk,
                  CAST(:metrics AS JSONB),CAST(:conclusions AS JSONB),
                  CAST(:surfaces AS JSONB),CAST(:groups AS JSONB),
                  CAST(:workflows AS JSONB),CAST(:skills AS JSONB),
                  CAST(:integrations AS JSONB),CAST(:decisions AS JSONB),
                  :dedupe,:actor
                ) RETURNING *
                """
                ),
                {
                    "id": case_id,
                    "p": project_id,
                    "subject_type": _required_text(subject_type, "subject_type"),
                    "subject_ref": _required_text(subject_ref, "subject_ref"),
                    "title": _required_text(title, "title"),
                    "risk": risk_tier,
                    "metrics": json.dumps(fields["metric_refs"]),
                    "conclusions": json.dumps(fields["settled_conclusion_refs"]),
                    "surfaces": json.dumps(fields["surface_refs"]),
                    "groups": json.dumps(fields["agent_group_run_refs"]),
                    "workflows": json.dumps(fields["workflow_run_refs"]),
                    "skills": json.dumps(fields["skill_invocation_refs"]),
                    "integrations": json.dumps(fields["integration_connection_refs"]),
                    "decisions": json.dumps(fields["decision_refs"]),
                    "dedupe": _required_text(dedupe_key, "dedupe_key"),
                    "actor": actor,
                },
            )
            .mappings()
            .one()
        )
        emit_event(
            connection,
            project_id,
            "assurance.case_created",
            actor,
            "assurance_case",
            case_id,
            {"subject_ref": subject_ref},
        )
        return {**_row(row), "duplicate": False}

    def create_claim(
        self,
        connection: Connection,
        *,
        project_id: str,
        assurance_case_id: str,
        claim_type: str,
        statement: str,
        evidence_contract: dict[str, Any],
        producer_ref: str,
        dedupe_key: str,
        actor: str,
    ) -> dict[str, Any]:
        if claim_type not in self.CLAIM_REQUIREMENTS:
            raise ValueError("Unsupported claim_type.")
        case = self._case(connection, project_id, assurance_case_id, for_update=True)
        existing = (
            connection.execute(
                text(
                    "SELECT * FROM assurance_claims WHERE project_id=:p AND dedupe_key=:d"
                ),
                {"p": project_id, "d": dedupe_key},
            )
            .mappings()
            .first()
        )
        if existing:
            return {**_row(existing), "duplicate": True}
        contract = {
            "required_evidence_types": sorted(self.CLAIM_REQUIREMENTS[claim_type]),
            "max_age_seconds": 86400,
            **(evidence_contract or {}),
        }
        claim_id = make_id("claim")
        row = (
            connection.execute(
                text(
                    """
                INSERT INTO assurance_claims (
                  id,project_id,assurance_case_id,claim_type,statement,
                  evidence_contract,producer_ref,dedupe_key
                ) VALUES (
                  :id,:p,:case,:type,:statement,CAST(:contract AS JSONB),:producer,:dedupe
                ) RETURNING *
                """
                ),
                {
                    "id": claim_id,
                    "p": project_id,
                    "case": case["id"],
                    "type": claim_type,
                    "statement": _required_text(statement, "statement"),
                    "contract": json.dumps(contract),
                    "producer": _required_text(producer_ref, "producer_ref"),
                    "dedupe": _required_text(dedupe_key, "dedupe_key"),
                },
            )
            .mappings()
            .one()
        )
        emit_event(
            connection,
            project_id,
            "assurance.claim_created",
            actor,
            "assurance_claim",
            claim_id,
            {"claim_type": claim_type},
        )
        return {**_row(row), "duplicate": False}

    def add_evidence(
        self,
        connection: Connection,
        *,
        project_id: str,
        assurance_case_id: str,
        claim_id: Optional[str],
        evidence_type: str,
        uri: Optional[str],
        payload: dict[str, Any],
        provenance: dict[str, Any],
        reproduction: dict[str, Any],
        content_hash: Optional[str],
        valid_until: Optional[datetime],
        actor: str,
    ) -> dict[str, Any]:
        self._case(connection, project_id, assurance_case_id)
        if claim_id:
            claim = self._claim(connection, project_id, claim_id)
            if claim["assurance_case_id"] != assurance_case_id:
                raise AssuranceEvidenceError(
                    "Evidence claim must belong to the selected Assurance Case."
                )
        if not provenance or not reproduction:
            raise AssuranceEvidenceError(
                "Evidence requires provenance and reproduction instructions."
            )
        digest = content_hash or json_hash(
            {"uri": uri, "payload": payload, "provenance": provenance}
        )
        existing = (
            connection.execute(
                text(
                    """
                SELECT * FROM assurance_evidence WHERE project_id=:p
                  AND claim_id IS NOT DISTINCT FROM :claim
                  AND content_hash=:hash AND evidence_type=:type
                """
                ),
                {
                    "p": project_id,
                    "claim": claim_id,
                    "hash": digest,
                    "type": evidence_type,
                },
            )
            .mappings()
            .first()
        )
        if existing:
            return {**_row(existing), "duplicate": True}
        evidence_id = make_id("evidence")
        row = (
            connection.execute(
                text(
                    """
                INSERT INTO assurance_evidence (
                  id,project_id,assurance_case_id,claim_id,evidence_type,
                  content_hash,uri,payload,provenance,reproduction,valid_until,created_by
                ) VALUES (
                  :id,:p,:case,:claim,:type,:hash,:uri,CAST(:payload AS JSONB),
                  CAST(:provenance AS JSONB),CAST(:reproduction AS JSONB),:valid,:actor
                ) RETURNING *
                """
                ),
                {
                    "id": evidence_id,
                    "p": project_id,
                    "case": assurance_case_id,
                    "claim": claim_id,
                    "type": _required_text(evidence_type, "evidence_type"),
                    "hash": digest,
                    "uri": uri,
                    "payload": json.dumps(payload or {}),
                    "provenance": json.dumps(provenance),
                    "reproduction": json.dumps(reproduction),
                    "valid": valid_until,
                    "actor": actor,
                },
            )
            .mappings()
            .one()
        )
        emit_event(
            connection,
            project_id,
            "assurance.evidence_attached",
            actor,
            "assurance_evidence",
            evidence_id,
            {"claim_id": claim_id, "evidence_type": evidence_type},
        )
        return {**_row(row), "duplicate": False}

    def invalidate_evidence(
        self,
        connection: Connection,
        *,
        project_id: str,
        evidence_id: str,
        reason: str,
        actor: str,
    ) -> dict[str, Any]:
        row = (
            connection.execute(
                text(
                    """
                UPDATE assurance_evidence SET invalidated_at=now(),invalidation_reason=:reason
                WHERE id=:id AND project_id=:p AND invalidated_at IS NULL RETURNING *
                """
                ),
                {
                    "id": evidence_id,
                    "p": project_id,
                    "reason": _required_text(reason, "reason"),
                },
            )
            .mappings()
            .first()
        )
        if not row:
            raise AssuranceNotFoundError("Active evidence not found.")
        connection.execute(
            text(
                "UPDATE assurance_claims SET status='invalidated',updated_at=now() "
                "WHERE id=:claim"
            ),
            {"claim": row["claim_id"]},
        )
        emit_event(
            connection,
            project_id,
            "assurance.evidence_invalidated",
            actor,
            "assurance_evidence",
            evidence_id,
            {"reason": reason, "claim_id": row["claim_id"]},
        )
        return _row(row)

    def evaluate_claim(
        self,
        connection: Connection,
        *,
        project_id: str,
        claim_id: str,
        dedupe_key: str,
        actor: str,
    ) -> dict[str, Any]:
        claim = self._claim(connection, project_id, claim_id, for_update=True)
        existing = (
            connection.execute(
                text(
                    "SELECT * FROM claim_verifications WHERE project_id=:p AND dedupe_key=:d"
                ),
                {"p": project_id, "d": dedupe_key},
            )
            .mappings()
            .first()
        )
        if existing:
            return {"claim": claim, "verification": _row(existing), "duplicate": True}
        rows = (
            connection.execute(
                text(
                    """
                SELECT * FROM assurance_evidence
                WHERE project_id=:p AND claim_id=:claim AND invalidated_at IS NULL
                  AND (valid_until IS NULL OR valid_until > now())
                ORDER BY created_at
                """
                ),
                {"p": project_id, "claim": claim_id},
            )
            .mappings()
            .all()
        )
        evidence = [_row(row) for row in rows]
        present = {item["evidence_type"] for item in evidence}
        contract = _json(claim.get("evidence_contract"), {})
        required = set(
            contract.get("required_evidence_types")
            or self.CLAIM_REQUIREMENTS[claim["claim_type"]]
        )
        missing = sorted(required - present)
        reasons: list[str] = []
        if missing:
            reasons.append("missing evidence: " + ", ".join(missing))
        if claim["claim_type"] in {"fixed", "solved"} and not missing:
            red = next(
                item for item in evidence if item["evidence_type"] == "red_pre_fix"
            )
            green = next(
                item
                for item in evidence
                if item["evidence_type"] == "green_post_change"
            )
            red_contract = _json(red.get("payload"), {}).get("contract_ref")
            green_contract = _json(green.get("payload"), {}).get("contract_ref")
            if not red_contract or red_contract != green_contract:
                reasons.append(
                    "red-to-green evidence must use the same verification contract"
                )
        if claim["claim_type"] == "caused_by" and not missing:
            isolation = next(
                item
                for item in evidence
                if item["evidence_type"] == "isolation_experiment"
            )
            changed = _json(isolation.get("payload"), {}).get("changed_variables") or []
            if len(changed) != 1:
                reasons.append("causal evidence must change exactly one variable")
        outcome = (
            "passed"
            if not reasons
            else (
                "inconclusive" if claim["claim_type"] == "caused_by" else "inconclusive"
            )
        )
        status = (
            "authoritative"
            if outcome == "passed"
            else ("inconclusive" if claim["claim_type"] == "caused_by" else "blocked")
        )
        decision_id = None
        if status != "authoritative":
            decision_id = create_blocking_decision(
                connection,
                project_id,
                None,
                f"Claim '{claim['claim_type']}' lacks reproducible evidence",
                {"action": "attach_required_evidence", "claim_id": claim_id},
                {"reasons": reasons, "required_evidence_types": sorted(required)},
                "claim_evidence_gate",
                risk_tier=2,
            )
        updated = (
            connection.execute(
                text(
                    "UPDATE assurance_claims SET status=:status,updated_at=now() WHERE id=:id RETURNING *"
                ),
                {"status": status, "id": claim_id},
            )
            .mappings()
            .one()
        )
        verification_id = make_id("claimver")
        verification = (
            connection.execute(
                text(
                    """
                INSERT INTO claim_verifications (
                  id,project_id,assurance_case_id,claim_id,claim_version,
                  outcome,reasons,evidence_refs,dedupe_key,verified_by
                ) VALUES (
                  :id,:p,:case,:claim,:version,:outcome,CAST(:reasons AS JSONB),
                  CAST(:evidence AS JSONB),:dedupe,:actor
                ) RETURNING *
                """
                ),
                {
                    "id": verification_id,
                    "p": project_id,
                    "case": claim["assurance_case_id"],
                    "claim": claim_id,
                    "version": claim["version"],
                    "outcome": outcome,
                    "reasons": json.dumps(reasons),
                    "evidence": json.dumps([item["id"] for item in evidence]),
                    "dedupe": _required_text(dedupe_key, "dedupe_key"),
                    "actor": actor,
                },
            )
            .mappings()
            .one()
        )
        emit_event(
            connection,
            project_id,
            "assurance.claim_evaluated",
            actor,
            "assurance_claim",
            claim_id,
            {"outcome": outcome, "decision_id": decision_id},
        )
        scheduled_verifications = None
        if status == "authoritative":
            scheduled_verifications = self.schedule_due_verifications(
                connection,
                project_id=project_id,
                actor=actor,
                claim_id=claim_id,
            )
        return {
            "claim": _row(updated),
            "verification": _row(verification),
            "decision_id": decision_id,
            "scheduled_verifications": scheduled_verifications,
            "duplicate": False,
        }

    def evaluate_case(
        self,
        connection: Connection,
        *,
        project_id: str,
        assurance_case_id: str,
        org_rules: dict[str, Any],
        project_rules: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        case = self._case(connection, project_id, assurance_case_id, for_update=True)
        effective, conflicts = self._compose_policy(
            org_rules or {}, project_rules or {}
        )
        claims = [
            _row(row)
            for row in connection.execute(
                text(
                    "SELECT * FROM assurance_claims WHERE assurance_case_id=:id ORDER BY created_at"
                ),
                {"id": assurance_case_id},
            )
            .mappings()
            .all()
        ]
        statuses = {claim["status"] for claim in claims}
        reasons: list[str] = list(conflicts)
        if not claims:
            reasons.append("assurance case has no claims")
        if "refuted" in statuses:
            status = "refuted"
        elif conflicts or any(
            value in statuses for value in {"blocked", "invalidated"}
        ):
            status = "blocked"
        elif not claims or any(
            value in statuses for value in {"draft", "pending", "inconclusive"}
        ):
            status = "inconclusive"
        elif statuses == {"authoritative"}:
            status = "accepted"
        else:
            status = "blocked"
        snapshot_payload = {
            "org": org_rules,
            "project": project_rules,
            "effective": effective,
            "conflicts": conflicts,
        }
        snapshot_hash = json_hash(snapshot_payload)
        snapshot_id = make_id("aps")
        snapshot = (
            connection.execute(
                text(
                    """
                INSERT INTO assurance_policy_snapshots (
                  id,project_id,assurance_case_id,assurance_case_version,
                  org_rules,project_rules,effective_rules,conflicts,content_hash
                ) VALUES (
                  :id,:p,:case,:version,CAST(:org AS JSONB),CAST(:project AS JSONB),
                  CAST(:effective AS JSONB),CAST(:conflicts AS JSONB),:hash
                ) ON CONFLICT (assurance_case_id,assurance_case_version,content_hash)
                  DO UPDATE SET content_hash=EXCLUDED.content_hash
                RETURNING *
                """
                ),
                {
                    "id": snapshot_id,
                    "p": project_id,
                    "case": assurance_case_id,
                    "version": case["version"],
                    "org": json.dumps(org_rules or {}),
                    "project": json.dumps(project_rules or {}),
                    "effective": json.dumps(effective),
                    "conflicts": json.dumps(conflicts),
                    "hash": snapshot_hash,
                },
            )
            .mappings()
            .one()
        )
        updated = (
            connection.execute(
                text(
                    """
                UPDATE assurance_cases SET status=:status,policy_snapshot=CAST(:snapshot AS JSONB),
                  final_reason=CAST(:reason AS JSONB),version=version+1,updated_at=now()
                WHERE id=:id RETURNING *
                """
                ),
                {
                    "status": status,
                    "snapshot": json.dumps({"id": snapshot["id"], **snapshot_payload}),
                    "reason": json.dumps(
                        {"reasons": reasons, "claim_statuses": sorted(statuses)}
                    ),
                    "id": assurance_case_id,
                },
            )
            .mappings()
            .one()
        )
        decision_id = None
        if status in {"blocked", "refuted"}:
            decision_id = create_blocking_decision(
                connection,
                project_id,
                None,
                f"Assurance Case '{case['title']}' cannot be accepted",
                {
                    "action": "resolve_assurance_case",
                    "assurance_case_id": assurance_case_id,
                },
                {"status": status, "reasons": reasons},
                "assurance_case_blocked",
                risk_tier=max(2, int(case["risk_tier"])),
            )
        emit_event(
            connection,
            project_id,
            "assurance.case_evaluated",
            actor,
            "assurance_case",
            assurance_case_id,
            {
                "status": status,
                "policy_snapshot_id": snapshot["id"],
                "decision_id": decision_id,
            },
        )
        return {
            "case": _row(updated),
            "policy_snapshot": _row(snapshot),
            "claims": claims,
            "decision_id": decision_id,
        }

    @staticmethod
    def _compose_policy(
        org_rules: dict[str, Any], project_rules: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        effective = dict(org_rules)
        conflicts: list[str] = []
        for key, value in project_rules.items():
            higher = org_rules.get(key)
            if isinstance(higher, bool) and higher and value is False:
                conflicts.append(
                    f"project policy cannot weaken mandatory org rule '{key}'"
                )
                effective[key] = True
            elif isinstance(higher, list) and isinstance(value, list):
                effective[key] = sorted({*higher, *value})
            else:
                effective[key] = value
        return effective, conflicts

    def list_cases(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = (
            connection.execute(
                text(
                    """
                SELECT c.*,
                  (SELECT count(*) FROM assurance_claims cl WHERE cl.assurance_case_id=c.id) AS claim_count,
                  (SELECT count(*) FROM assurance_evidence e WHERE e.assurance_case_id=c.id AND e.invalidated_at IS NULL) AS evidence_count,
                  (SELECT count(*) FROM verification_runs v WHERE v.assurance_case_id=c.id) AS verification_count
                FROM assurance_cases c WHERE c.project_id=:p
                ORDER BY c.updated_at DESC
                """
                ),
                {"p": project_id},
            )
            .mappings()
            .all()
        )
        return [_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Production-surface ownership
    # ------------------------------------------------------------------
    def acquire_surface(
        self,
        connection: Connection,
        *,
        project_id: str,
        surface_key: str,
        environment: str,
        owner_ref: str,
        coordination_route: str,
        reason: str,
        dedupe_key: str,
        actor: str,
        expires_at: Optional[datetime] = None,
    ) -> dict[str, Any]:
        surface_key = _required_text(surface_key, "surface_key")
        environment = _required_text(environment, "environment")
        existing_transition = (
            connection.execute(
                text(
                    "SELECT * FROM ownership_transitions WHERE project_id=:p AND dedupe_key=:d"
                ),
                {"p": project_id, "d": dedupe_key},
            )
            .mappings()
            .first()
        )
        if existing_transition:
            ownership = (
                connection.execute(
                    text("SELECT * FROM surface_ownerships WHERE id=:id"),
                    {"id": existing_transition["surface_ownership_id"]},
                )
                .mappings()
                .one()
            )
            return {
                "allowed": True,
                "ownership": _row(ownership),
                "transition": _row(existing_transition),
                "duplicate": True,
            }
        current = (
            connection.execute(
                text(
                    "SELECT * FROM surface_ownerships WHERE project_id=:p AND surface_key=:s "
                    "AND environment=:e FOR UPDATE"
                ),
                {"p": project_id, "s": surface_key, "e": environment},
            )
            .mappings()
            .first()
        )
        if (
            current
            and current["status"] == "active"
            and (current["expires_at"] is None or current["expires_at"] > now_utc())
        ):
            if current["owner_ref"] != owner_ref:
                decision_id = create_blocking_decision(
                    connection,
                    project_id,
                    None,
                    f"Production surface '{surface_key}' is already owned",
                    {
                        "action": "coordinate_with_surface_owner",
                        "owner_ref": current["owner_ref"],
                        "coordination_route": current["coordination_route"],
                    },
                    {"surface_key": surface_key, "environment": environment},
                    "surface_ownership_conflict",
                    risk_tier=3,
                )
                emit_event(
                    connection,
                    project_id,
                    "surface_ownership.acquire_blocked",
                    actor,
                    "surface_ownership",
                    current["id"],
                    {"decision_id": decision_id, "current_owner": current["owner_ref"]},
                )
                return {
                    "allowed": False,
                    "ownership": _row(current),
                    "decision_id": decision_id,
                    "duplicate": False,
                }
            return {
                "allowed": True,
                "ownership": _row(current),
                "transition": None,
                "duplicate": True,
            }
        if current:
            new_version = int(current["version"]) + 1
            row = (
                connection.execute(
                    text(
                        """
                    UPDATE surface_ownerships SET owner_ref=:owner,coordination_route=:route,
                      version=:version,status='active',expires_at=:expires,acquired_by=:actor,
                      acquired_at=now(),updated_at=now() WHERE id=:id RETURNING *
                    """
                    ),
                    {
                        "owner": owner_ref,
                        "route": coordination_route,
                        "version": new_version,
                        "expires": expires_at,
                        "actor": actor,
                        "id": current["id"],
                    },
                )
                .mappings()
                .one()
            )
            ownership_id = current["id"]
            from_owner = current["owner_ref"]
            from_version = current["version"]
        else:
            ownership_id = make_id("surface")
            new_version = 1
            row = (
                connection.execute(
                    text(
                        """
                    INSERT INTO surface_ownerships (
                      id,project_id,surface_key,environment,owner_ref,
                      coordination_route,version,expires_at,acquired_by
                    ) VALUES (:id,:p,:surface,:environment,:owner,:route,1,:expires,:actor)
                    RETURNING *
                    """
                    ),
                    {
                        "id": ownership_id,
                        "p": project_id,
                        "surface": surface_key,
                        "environment": environment,
                        "owner": owner_ref,
                        "route": coordination_route,
                        "expires": expires_at,
                        "actor": actor,
                    },
                )
                .mappings()
                .one()
            )
            from_owner = None
            from_version = None
        transition_id = make_id("ownx")
        transition = (
            connection.execute(
                text(
                    """
                INSERT INTO ownership_transitions (
                  id,project_id,surface_ownership_id,transition_kind,from_owner_ref,
                  to_owner_ref,from_version,to_version,reason,actor,dedupe_key
                ) VALUES (:id,:p,:ownership,'acquire',:from_owner,:owner,:from_version,
                  :to_version,:reason,:actor,:dedupe) RETURNING *
                """
                ),
                {
                    "id": transition_id,
                    "p": project_id,
                    "ownership": ownership_id,
                    "from_owner": from_owner,
                    "owner": owner_ref,
                    "from_version": from_version,
                    "to_version": new_version,
                    "reason": _required_text(reason, "reason"),
                    "actor": actor,
                    "dedupe": dedupe_key,
                },
            )
            .mappings()
            .one()
        )
        emit_event(
            connection,
            project_id,
            "surface_ownership.acquired",
            actor,
            "surface_ownership",
            ownership_id,
            {"owner_ref": owner_ref, "version": new_version},
        )
        return {
            "allowed": True,
            "ownership": _row(row),
            "transition": _row(transition),
            "duplicate": False,
        }

    def transfer_surface(
        self,
        connection: Connection,
        *,
        project_id: str,
        ownership_id: str,
        expected_version: int,
        to_owner_ref: str,
        coordination_route: str,
        reason: str,
        dedupe_key: str,
        actor: str,
        emergency: bool = False,
        approved_decision_id: Optional[str] = None,
    ) -> dict[str, Any]:
        current = (
            connection.execute(
                text(
                    "SELECT * FROM surface_ownerships WHERE id=:id AND project_id=:p FOR UPDATE"
                ),
                {"id": ownership_id, "p": project_id},
            )
            .mappings()
            .first()
        )
        if not current:
            raise AssuranceNotFoundError("Surface ownership not found.")
        if int(current["version"]) != expected_version:
            raise AssuranceConflictError("Surface ownership version is stale.")
        if not emergency and actor != current["owner_ref"]:
            raise AssuranceConflictError(
                "Only the current owner may transfer this surface."
            )
        if emergency:
            approved = connection.execute(
                text(
                    "SELECT id FROM decisions WHERE id=:id AND project_id=:p AND status='approved'"
                ),
                {"id": approved_decision_id, "p": project_id},
            ).scalar()
            if not approved:
                raise AssuranceConflictError(
                    "Emergency takeover requires an approved decision."
                )
        version = expected_version + 1
        row = (
            connection.execute(
                text(
                    """
                UPDATE surface_ownerships SET owner_ref=:owner,coordination_route=:route,
                  version=:version,status='active',updated_at=now()
                WHERE id=:id RETURNING *
                """
                ),
                {
                    "owner": _required_text(to_owner_ref, "to_owner_ref"),
                    "route": _required_text(coordination_route, "coordination_route"),
                    "version": version,
                    "id": ownership_id,
                },
            )
            .mappings()
            .one()
        )
        kind = "emergency_takeover" if emergency else "transfer"
        transition = (
            connection.execute(
                text(
                    """
                INSERT INTO ownership_transitions (
                  id,project_id,surface_ownership_id,transition_kind,from_owner_ref,
                  to_owner_ref,from_version,to_version,reason,decision_id,actor,dedupe_key
                ) VALUES (:id,:p,:ownership,:kind,:from_owner,:to_owner,:from_version,
                  :to_version,:reason,:decision,:actor,:dedupe) RETURNING *
                """
                ),
                {
                    "id": make_id("ownx"),
                    "p": project_id,
                    "ownership": ownership_id,
                    "kind": kind,
                    "from_owner": current["owner_ref"],
                    "to_owner": to_owner_ref,
                    "from_version": expected_version,
                    "to_version": version,
                    "reason": _required_text(reason, "reason"),
                    "decision": approved_decision_id,
                    "actor": actor,
                    "dedupe": dedupe_key,
                },
            )
            .mappings()
            .one()
        )
        emit_event(
            connection,
            project_id,
            f"surface_ownership.{kind}",
            actor,
            "surface_ownership",
            ownership_id,
            {
                "from_owner": current["owner_ref"],
                "to_owner": to_owner_ref,
                "version": version,
            },
        )
        return {"ownership": _row(row), "transition": _row(transition)}

    def preflight_surface(
        self,
        connection: Connection,
        *,
        project_id: str,
        surface_key: str,
        environment: str,
        actor_ref: str,
        expected_version: int,
        effect_ref: str,
        dedupe_key: str,
    ) -> dict[str, Any]:
        existing = (
            connection.execute(
                text(
                    "SELECT * FROM ownership_transitions WHERE project_id=:p AND dedupe_key=:d"
                ),
                {"p": project_id, "d": dedupe_key},
            )
            .mappings()
            .first()
        )
        if existing:
            return {"allowed": True, "transition": _row(existing), "duplicate": True}
        ownership = (
            connection.execute(
                text(
                    "SELECT * FROM surface_ownerships WHERE project_id=:p AND surface_key=:surface "
                    "AND environment=:environment FOR UPDATE"
                ),
                {"p": project_id, "surface": surface_key, "environment": environment},
            )
            .mappings()
            .first()
        )
        reason = None
        if not ownership:
            reason = "surface is unowned"
        elif ownership["status"] != "active":
            reason = f"surface ownership is {ownership['status']}"
        elif (
            ownership["expires_at"] is not None and ownership["expires_at"] <= now_utc()
        ):
            reason = "surface ownership is expired"
        elif ownership["owner_ref"] != actor_ref:
            reason = f"surface is owned by {ownership['owner_ref']}"
        elif int(ownership["version"]) != expected_version:
            reason = "surface ownership version is stale"
        if reason:
            decision_id = create_blocking_decision(
                connection,
                project_id,
                None,
                f"Production effect '{effect_ref}' failed its ownership preflight",
                {
                    "action": "coordinate_with_surface_owner",
                    "owner_ref": ownership["owner_ref"] if ownership else None,
                    "coordination_route": ownership["coordination_route"]
                    if ownership
                    else None,
                },
                {
                    "surface_key": surface_key,
                    "environment": environment,
                    "reason": reason,
                },
                "surface_ownership_preflight_failed",
                risk_tier=3,
            )
            emit_event(
                connection,
                project_id,
                "surface_ownership.preflight_blocked",
                actor_ref,
                "surface_ownership",
                ownership["id"] if ownership else surface_key,
                {
                    "effect_ref": effect_ref,
                    "reason": reason,
                    "decision_id": decision_id,
                },
            )
            return {
                "allowed": False,
                "ownership": _row(ownership) if ownership else None,
                "reason": reason,
                "decision_id": decision_id,
                "duplicate": False,
            }
        transition = (
            connection.execute(
                text(
                    """
                INSERT INTO ownership_transitions (
                  id,project_id,surface_ownership_id,transition_kind,from_owner_ref,
                  to_owner_ref,from_version,to_version,reason,actor,dedupe_key
                ) VALUES (:id,:p,:ownership,'preflight',:owner,:owner,:version,
                  :version,:reason,:actor,:dedupe) RETURNING *
                """
                ),
                {
                    "id": make_id("ownx"),
                    "p": project_id,
                    "ownership": ownership["id"],
                    "owner": actor_ref,
                    "version": expected_version,
                    "reason": f"ownership preflight for {effect_ref}",
                    "actor": actor_ref,
                    "dedupe": dedupe_key,
                },
            )
            .mappings()
            .one()
        )
        return {
            "allowed": True,
            "ownership": _row(ownership),
            "transition": _row(transition),
            "duplicate": False,
        }

    def list_surfaces(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = (
            connection.execute(
                text(
                    """
                SELECT s.*,
                  (SELECT count(*) FROM ownership_transitions t WHERE t.surface_ownership_id=s.id) AS transition_count
                FROM surface_ownerships s WHERE s.project_id=:p
                ORDER BY s.environment,s.surface_key
                """
                ),
                {"p": project_id},
            )
            .mappings()
            .all()
        )
        return [_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Standing adversarial verifier
    # ------------------------------------------------------------------
    def create_verification_policy(
        self,
        connection: Connection,
        *,
        project_id: str,
        policy_key: str,
        surface_patterns: list[Any],
        claim_types: list[str],
        risk_tiers: list[int],
        cadence_seconds: int,
        sample_rate: float,
        verification_method: dict[str, Any],
        independence_policy: dict[str, Any],
        escalation_target: str,
        estimated_cost_usd_micros: int,
        actor: str,
    ) -> dict[str, Any]:
        policy_key = _required_text(policy_key, "policy_key")
        _required_list(claim_types, "claim_types")
        if cadence_seconds <= 0 or not 0 <= sample_rate <= 1:
            raise ValueError("cadence_seconds and sample_rate are invalid.")
        current = (
            connection.execute(
                text(
                    "SELECT * FROM verification_policies WHERE project_id=:p AND policy_key=:k AND status='active' FOR UPDATE"
                ),
                {"p": project_id, "k": policy_key},
            )
            .mappings()
            .first()
        )
        version = int(current["version"]) + 1 if current else 1
        if current:
            connection.execute(
                text(
                    "UPDATE verification_policies SET status='superseded' WHERE id=:id"
                ),
                {"id": current["id"]},
            )
        row = (
            connection.execute(
                text(
                    """
                INSERT INTO verification_policies (
                  id,project_id,policy_key,version,surface_patterns,claim_types,risk_tiers,
                  cadence_seconds,sample_rate,verification_method,independence_policy,
                  escalation_target,estimated_cost_usd_micros,created_by
                ) VALUES (
                  :id,:p,:key,:version,CAST(:surfaces AS JSONB),CAST(:claims AS JSONB),
                  CAST(:risks AS JSONB),:cadence,:sample,CAST(:method AS JSONB),
                  CAST(:independence AS JSONB),:target,:cost,:actor
                ) RETURNING *
                """
                ),
                {
                    "id": make_id("vpolicy"),
                    "p": project_id,
                    "key": policy_key,
                    "version": version,
                    "surfaces": json.dumps(surface_patterns or []),
                    "claims": json.dumps(claim_types),
                    "risks": json.dumps(risk_tiers or []),
                    "cadence": cadence_seconds,
                    "sample": sample_rate,
                    "method": json.dumps(verification_method or {}),
                    "independence": json.dumps(independence_policy or {}),
                    "target": _required_text(escalation_target, "escalation_target"),
                    "cost": estimated_cost_usd_micros,
                    "actor": actor,
                },
            )
            .mappings()
            .one()
        )
        emit_event(
            connection,
            project_id,
            "assurance.verification_policy_published",
            actor,
            "verification_policy",
            row["id"],
            {"policy_key": policy_key, "version": version},
        )
        scheduled = self.schedule_due_verifications(
            connection,
            project_id=project_id,
            actor=actor,
            verification_policy_id=row["id"],
        )
        return {**_row(row), "scheduled_runs": scheduled["scheduled"], "schedule_blocks": scheduled["blocked"]}

    def schedule_due_verifications(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        claim_id: Optional[str] = None,
        verification_policy_id: Optional[str] = None,
    ) -> dict[str, Any]:
        conditions = [
            "c.project_id=:p",
            "c.status='authoritative'",
            "p.status='active'",
        ]
        params: dict[str, Any] = {"p": project_id}
        if claim_id:
            conditions.append("c.id=:claim")
            params["claim"] = claim_id
        if verification_policy_id:
            conditions.append("p.id=:policy")
            params["policy"] = verification_policy_id
        candidates = connection.execute(
            text(
                f"""
                SELECT c.id AS claim_id,c.claim_type,c.version AS claim_version,
                       c.producer_ref,c.updated_at AS claim_updated_at,
                       ac.id AS case_id,ac.subject_ref,ac.risk_tier,
                       p.*
                FROM assurance_claims c
                JOIN assurance_cases ac ON ac.id=c.assurance_case_id
                JOIN verification_policies p ON p.project_id=c.project_id
                WHERE {' AND '.join(conditions)}
                ORDER BY p.policy_key,c.id
                """
            ),
            params,
        ).mappings().all()
        now = now_utc()
        scheduled: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        for raw in candidates:
            candidate = dict(raw)
            if candidate["claim_type"] not in _json(candidate["claim_types"], []):
                continue
            risk_tiers = _json(candidate["risk_tiers"], [])
            if risk_tiers and int(candidate["risk_tier"]) not in risk_tiers:
                continue
            patterns = _json(candidate["surface_patterns"], [])
            if patterns and not any(
                fnmatchcase(candidate["subject_ref"], str(pattern))
                for pattern in patterns
            ):
                continue
            sample = int(
                hashlib.sha256(
                    f"{candidate['id']}:{candidate['claim_id']}".encode("utf-8")
                ).hexdigest()[:8],
                16,
            ) / 0xFFFFFFFF
            if sample > float(candidate["sample_rate"]):
                continue
            previous = (
                connection.execute(
                    text(
                        """
                        SELECT * FROM verification_runs
                        WHERE project_id=:p AND claim_id=:claim
                          AND verification_policy_id=:policy
                        ORDER BY created_at DESC LIMIT 1
                        """
                    ),
                    {
                        "p": project_id,
                        "claim": candidate["claim_id"],
                        "policy": candidate["id"],
                    },
                )
                .mappings()
                .first()
            )
            if previous and previous["status"] in {"scheduled", "leased", "running"}:
                continue
            anchor = candidate["claim_updated_at"]
            if previous and previous["completed_at"]:
                due_at = previous["completed_at"] + timedelta(
                    seconds=int(candidate["cadence_seconds"])
                )
                if due_at > now:
                    continue
                anchor = due_at
            dedupe = (
                f"standing-verifier:{candidate['id']}:{candidate['claim_id']}:"
                f"{int(anchor.timestamp())}"
            )
            try:
                run = self.schedule_verification(
                    connection,
                    project_id=project_id,
                    assurance_case_id=candidate["case_id"],
                    claim_id=candidate["claim_id"],
                    verification_policy_id=candidate["id"],
                    producer_path={"producer_ref": candidate["producer_ref"]},
                    verifier_path={
                        "role": "adversarial_verifier",
                        "policy_key": candidate["policy_key"],
                    },
                    dedupe_key=dedupe,
                    actor=actor,
                )
                scheduled.append(run)
            except AssuranceConflictError as error:
                decision_id = create_blocking_decision(
                    connection,
                    project_id,
                    None,
                    "Standing adversarial verification could not be scheduled",
                    {
                        "action": "restore_verifier_preconditions",
                        "claim_id": candidate["claim_id"],
                        "verification_policy_id": candidate["id"],
                    },
                    {"reason": str(error)},
                    "adversarial_verification_schedule_blocked",
                    risk_tier=max(2, int(candidate["risk_tier"])),
                )
                blocked.append(
                    {
                        "claim_id": candidate["claim_id"],
                        "verification_policy_id": candidate["id"],
                        "reason": str(error),
                        "decision_id": decision_id,
                    }
                )
                emit_event(
                    connection,
                    project_id,
                    "assurance.verification_schedule_blocked",
                    actor,
                    "assurance_claim",
                    candidate["claim_id"],
                    {"decision_id": decision_id, "reason": str(error)},
                )
        return {
            "project_id": project_id,
            "scheduled": scheduled,
            "blocked": blocked,
        }

    def schedule_verification(
        self,
        connection: Connection,
        *,
        project_id: str,
        assurance_case_id: str,
        claim_id: str,
        verification_policy_id: str,
        producer_path: dict[str, Any],
        verifier_path: dict[str, Any],
        dedupe_key: str,
        actor: str,
    ) -> dict[str, Any]:
        case = self._case(connection, project_id, assurance_case_id)
        claim = self._claim(connection, project_id, claim_id)
        if claim["assurance_case_id"] != assurance_case_id:
            raise AssuranceConflictError("Claim belongs to another Assurance Case.")
        existing = (
            connection.execute(
                text(
                    "SELECT * FROM verification_runs WHERE project_id=:p AND dedupe_key=:d"
                ),
                {"p": project_id, "d": dedupe_key},
            )
            .mappings()
            .first()
        )
        if existing:
            return {**_row(existing), "duplicate": True}
        policy = (
            connection.execute(
                text(
                    "SELECT * FROM verification_policies WHERE id=:id AND project_id=:p AND status='active'"
                ),
                {"id": verification_policy_id, "p": project_id},
            )
            .mappings()
            .first()
        )
        if not policy:
            raise AssuranceNotFoundError("Active verification policy not found.")
        if claim["claim_type"] not in _json(policy["claim_types"], []):
            raise AssuranceConflictError(
                "Verification policy does not cover this claim type."
            )
        risk_tiers = _json(policy["risk_tiers"], [])
        if risk_tiers and int(case["risk_tier"]) not in risk_tiers:
            raise AssuranceConflictError(
                "Verification policy does not cover this risk tier."
            )
        independence = _json(policy["independence_policy"], {})
        if (
            independence.get("require_distinct_path", True)
            and producer_path == verifier_path
        ):
            raise AssuranceConflictError(
                "Verifier path must be independent from the producer path."
            )
        reservation_id = None
        cost = int(policy["estimated_cost_usd_micros"] or 0)
        if cost:
            reservation = self.budget.reserve(
                connection,
                project_id,
                "verification",
                cost,
                f"Adversarial verification for claim {claim_id}",
                max(5, int(policy["cadence_seconds"]) // 60 + 5),
                risk_tier=int(case["risk_tier"]),
            )
            if reservation["status"] != "reserved":
                raise AssuranceConflictError(
                    reservation.get("message") or "Verification budget denied."
                )
            reservation_id = reservation["reservation_id"]
        run_id = make_id("vrun")
        job = enqueue_job(
            connection,
            project_id,
            "assurance.verify",
            {
                "verification_run_id": run_id,
                "assurance_case_id": assurance_case_id,
                "claim_id": claim_id,
                "verification_method": _json(policy["verification_method"], {}),
                "producer_path": producer_path,
                "verifier_path": verifier_path,
            },
            priority=25 if int(case["risk_tier"]) >= 2 else 75,
            dedupe_key=f"verification-job:{dedupe_key}",
            workload_class="verification",
            data_boundary="internal",
        )
        row = (
            connection.execute(
                text(
                    """
                INSERT INTO verification_runs (
                  id,project_id,assurance_case_id,claim_id,verification_policy_id,
                  job_id,budget_reservation_id,producer_path,verifier_path,
                  evidence_bundle,dedupe_key
                ) VALUES (
                  :id,:p,:case,:claim,:policy,:job,:reservation,
                  CAST(:producer AS JSONB),CAST(:verifier AS JSONB),
                  CAST(:evidence AS JSONB),:dedupe
                ) RETURNING *
                """
                ),
                {
                    "id": run_id,
                    "p": project_id,
                    "case": assurance_case_id,
                    "claim": claim_id,
                    "policy": verification_policy_id,
                    "job": job["id"],
                    "reservation": reservation_id,
                    "producer": json.dumps(producer_path or {}),
                    "verifier": json.dumps(verifier_path or {}),
                    "evidence": json.dumps([]),
                    "dedupe": dedupe_key,
                },
            )
            .mappings()
            .one()
        )
        emit_event(
            connection,
            project_id,
            "assurance.verification_scheduled",
            actor,
            "verification_run",
            run_id,
            {"job_id": job["id"], "claim_id": claim_id},
        )
        return {**_row(row), "duplicate": False}

    def complete_verification(
        self,
        connection: Connection,
        *,
        project_id: str,
        verification_run_id: str,
        worker_id: str,
        job_lease_token: str,
        outcome: str,
        evidence_bundle: list[Any],
        receipt_refs: list[str],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        if outcome not in {"passed", "refuted", "inconclusive"}:
            raise ValueError("outcome must be passed, refuted, or inconclusive.")
        run = (
            connection.execute(
                text(
                    "SELECT * FROM verification_runs WHERE id=:id AND project_id=:p FOR UPDATE"
                ),
                {"id": verification_run_id, "p": project_id},
            )
            .mappings()
            .first()
        )
        if not run:
            raise AssuranceNotFoundError("Verification run not found.")
        if run["status"] in {"passed", "refuted", "inconclusive"}:
            return _row(run)
        self.jobs.require_active_lease(
            connection, worker_id, run["job_id"], job_lease_token
        )
        if not evidence_bundle:
            outcome = "inconclusive"
            result = {**(result or {}), "reason": "verifier supplied no evidence"}
        if run["budget_reservation_id"] and not receipt_refs:
            raise AssuranceEvidenceError(
                "Spendy verification completion requires receipt references."
            )
        if receipt_refs:
            valid_receipts = connection.execute(
                text(
                    "SELECT id FROM receipts WHERE project_id=:p AND id = ANY(:ids)"
                ),
                {"p": project_id, "ids": receipt_refs},
            ).scalars().all()
            if len(set(valid_receipts)) != len(set(receipt_refs)):
                raise AssuranceEvidenceError(
                    "Verifier receipt references must belong to the same project."
                )
        policy = (
            connection.execute(
                text(
                    "SELECT * FROM verification_policies WHERE id=:id AND project_id=:p"
                ),
                {"id": run["verification_policy_id"], "p": project_id},
            )
            .mappings()
            .one()
        )
        estimated_cost = int(policy["estimated_cost_usd_micros"] or 0)
        if run["budget_reservation_id"]:
            actual_cost = result.get("actual_cost_usd_micros")
            self.budget.consume(
                connection,
                project_id,
                run["budget_reservation_id"],
                receipt_refs[0],
                "verification",
                f"assurance.verify:{policy['policy_key']}",
                estimated_cost,
                int(actual_cost) if actual_cost is not None else None,
                worker_id,
            )
        updated = (
            connection.execute(
                text(
                    """
                UPDATE verification_runs SET status=:status,evidence_bundle=CAST(:evidence AS JSONB),
                  receipt_refs=CAST(:receipts AS JSONB),result=CAST(:result AS JSONB),
                  started_at=COALESCE(started_at,now()),completed_at=now()
                WHERE id=:id RETURNING *
                """
                ),
                {
                    "status": outcome,
                    "evidence": json.dumps(evidence_bundle),
                    "receipts": json.dumps(receipt_refs or []),
                    "result": json.dumps(result or {}),
                    "id": verification_run_id,
                },
            )
            .mappings()
            .one()
        )
        claim_status = {
            "passed": "authoritative",
            "refuted": "refuted",
            "inconclusive": "inconclusive",
        }[outcome]
        connection.execute(
            text(
                "UPDATE assurance_claims SET status=:status,updated_at=now() WHERE id=:id"
            ),
            {"status": claim_status, "id": run["claim_id"]},
        )
        decision_id = None
        if outcome == "refuted":
            connection.execute(
                text(
                    "UPDATE assurance_cases SET status='refuted',version=version+1,updated_at=now() WHERE id=:id"
                ),
                {"id": run["assurance_case_id"]},
            )
            decision_id = create_blocking_decision(
                connection,
                project_id,
                None,
                "Standing verifier refuted an authoritative claim",
                {
                    "action": "reopen_and_investigate",
                    "claim_id": run["claim_id"],
                    "escalation_target": policy["escalation_target"],
                },
                {
                    "verification_run_id": verification_run_id,
                    "evidence_bundle": evidence_bundle,
                },
                "adversarial_verification_refuted",
                risk_tier=3,
            )
        emit_event(
            connection,
            project_id,
            f"assurance.verification_{outcome}",
            worker_id,
            "verification_run",
            verification_run_id,
            {"claim_id": run["claim_id"], "decision_id": decision_id},
        )
        self.jobs.complete(
            connection,
            worker_id,
            run["job_id"],
            job_lease_token,
            {"verification_run_id": verification_run_id, "outcome": outcome},
        )
        return {**_row(updated), "decision_id": decision_id}

    def list_verification_runs(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = (
            connection.execute(
                text(
                    """
                    SELECT v.*,
                      CASE WHEN v.completed_at IS NOT NULL
                        THEN v.completed_at + make_interval(secs => p.cadence_seconds)
                        ELSE NULL END AS next_run_at,
                      p.policy_key,p.escalation_target
                    FROM verification_runs v
                    JOIN verification_policies p ON p.id=v.verification_policy_id
                    WHERE v.project_id=:p ORDER BY v.created_at DESC
                    """
                ),
                {"p": project_id},
            )
            .mappings()
            .all()
        )
        return [_row(row) for row in rows]

    def overview(self, connection: Connection, project_id: str) -> dict[str, Any]:
        return {
            "project_id": project_id,
            "metrics": self.list_metrics(connection, project_id),
            "cases": self.list_cases(connection, project_id),
            "surfaces": self.list_surfaces(connection, project_id),
            "verification_runs": self.list_verification_runs(connection, project_id),
            "settled_conclusions": [
                item
                for item in self.list_conclusions(connection, project_id)
                if item["status"] == "current"
            ],
        }

    @staticmethod
    def _case(
        connection: Connection,
        project_id: str,
        case_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any]:
        lock = " FOR UPDATE" if for_update else ""
        row = (
            connection.execute(
                text(
                    f"SELECT * FROM assurance_cases WHERE id=:id AND project_id=:p{lock}"
                ),
                {"id": case_id, "p": project_id},
            )
            .mappings()
            .first()
        )
        if not row:
            raise AssuranceNotFoundError("Assurance Case not found.")
        return _row(row)

    @staticmethod
    def _claim(
        connection: Connection,
        project_id: str,
        claim_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any]:
        lock = " FOR UPDATE" if for_update else ""
        row = (
            connection.execute(
                text(
                    f"SELECT * FROM assurance_claims WHERE id=:id AND project_id=:p{lock}"
                ),
                {"id": claim_id, "p": project_id},
            )
            .mappings()
            .first()
        )
        if not row:
            raise AssuranceNotFoundError("Assurance claim not found.")
        return _row(row)
