from __future__ import annotations

import json
from typing import Any, Callable, Optional

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event, make_id, now_utc


DEFAULT_DEPLOYMENT_ROLLOUT_STAGES = ["canary_10pct", "expand_50pct", "full_100pct"]
DEFAULT_DEPLOYMENT_ROLLOUT_THRESHOLDS = {"max_error_rate": 0.05}


def build_rollout_state(
    stages: list[str], thresholds: dict[str, Any]
) -> dict[str, Any]:
    return {
        "stages": stages,
        "current_index": 0,
        "status": "in_progress",
        "thresholds": thresholds,
        "history": [],
    }


def rollout_regression_reason(
    observation: dict[str, Any], thresholds: dict[str, Any]
) -> Optional[str]:
    health = str(observation.get("health", "")).strip().lower()
    if health in {"degraded", "unhealthy", "failing", "fail"}:
        return f"health={health}"
    if observation.get("regression") is True:
        return "regression_flag"
    error_rate = observation.get("error_rate")
    max_error_rate = thresholds.get("max_error_rate")
    if error_rate is not None and max_error_rate is not None:
        try:
            if float(error_rate) > float(max_error_rate):
                return f"error_rate {error_rate} > {max_error_rate}"
        except (TypeError, ValueError):
            pass
    return None


def advance_rollout_state(
    state: dict[str, Any],
    observation: dict[str, Any],
    *,
    observed_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Advance the shared ship-narrow/widen state machine without persistence.

    Provider deployments and BOSS self-improvement keep distinct durable records,
    receipts, and events, but use this one deterministic transition function.
    """
    next_state = dict(state)
    stages = [str(item) for item in next_state.get("stages") or []]
    if not stages:
        raise ValueError("Rollout state has no stages.")
    index = int(next_state.get("current_index", 0))
    if index < 0 or index >= len(stages):
        raise ValueError("Rollout state current_index is outside its stage list.")
    current_stage = stages[index]
    thresholds = dict(next_state.get("thresholds") or {})
    history = list(next_state.get("history") or [])
    regression = rollout_regression_reason(observation, thresholds)
    if regression is not None:
        history.append(
            {
                "stage": current_stage,
                "observation": observation,
                "result": "rolled_back",
                "reason": regression,
                "at": observed_at,
            }
        )
        next_state.update({"history": history, "status": "rolled_back"})
        return next_state, {
            "status": "rolled_back",
            "from_stage": current_stage,
            "to_stage": current_stage,
            "reason": regression,
        }

    history.append(
        {
            "stage": current_stage,
            "observation": observation,
            "result": "advanced",
            "at": observed_at,
        }
    )
    next_index = index + 1
    completed = next_index >= len(stages)
    next_state.update(
        {
            "current_index": len(stages) if completed else next_index,
            "history": history,
            "status": "completed" if completed else "in_progress",
        }
    )
    return next_state, {
        "status": "completed" if completed else "in_progress",
        "from_stage": current_stage,
        "to_stage": current_stage if completed else stages[next_index],
        "reason": None,
    }


HealthSource = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


class DeploymentRolloutEngine:
    """Health-gated rollout state for arbitrary ProviderOps DeploymentRuns.

    The engine advances orchestration state only. A rollback stops widening and
    marks the DeploymentRun ``rollback_pending``; provider actuation remains behind
    the Resource Plane ChangeSet, reservation, approval, and ResourceReceipt path.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        health_source: Optional[HealthSource] = None,
    ):
        self.settings = settings
        self.health_source = health_source or self._verify_observation

    def _verify_observation(
        self, observation: dict[str, Any], deployment_run: dict[str, Any]
    ) -> dict[str, Any]:
        verified = dict(observation)
        healthcheck_url = str(verified.get("healthcheck_url") or "").strip()
        if healthcheck_url:
            try:
                response = httpx.get(
                    healthcheck_url,
                    follow_redirects=True,
                    timeout=self.settings.sandbox_deploy_healthcheck_timeout_seconds,
                )
                verified.update(
                    {
                        "health": "healthy" if response.is_success else "unhealthy",
                        "http_status": response.status_code,
                        "verified_by": "http_healthcheck",
                    }
                )
            except Exception as error:
                verified.update(
                    {
                        "health": "unhealthy",
                        "verified_by": "http_healthcheck",
                        "error": str(error),
                    }
                )
        elif not str(verified.get("health") or "").strip():
            raise ValueError(
                "A rollout observation needs health or a healthcheck_url."
            )
        verified.setdefault("deployment_run_id", deployment_run["id"])
        verified.setdefault("verified_by", "provided_health_observation")
        return verified

    @staticmethod
    def _normalize_stages(stages: Optional[list[str]]) -> list[str]:
        values = stages or list(DEFAULT_DEPLOYMENT_ROLLOUT_STAGES)
        normalized = [str(item).strip() for item in values if str(item).strip()]
        if not normalized or len(set(normalized)) != len(normalized):
            raise ValueError("Rollout stages must be non-empty and unique.")
        return normalized

    @staticmethod
    def _get_deployment_run(
        connection: Connection, project_id: str, deployment_run_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, deployment_target_id, changeset_id,
                       app_name, environment,
                       repo, target_kind, target_ref, commit_ref, status,
                       rollback_plan, created_by
                FROM deployment_runs
                WHERE id = :deployment_run_id AND project_id = :project_id
                FOR UPDATE
                """
            ),
            {"deployment_run_id": deployment_run_id, "project_id": project_id},
        ).mappings().first()
        if not row:
            raise ValueError("Deployment run not found.")
        return dict(row)

    @staticmethod
    def _get_rollout(
        connection: Connection,
        project_id: str,
        rollout_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any]:
        lock = "FOR UPDATE" if for_update else ""
        row = connection.execute(
            text(
                f"""
                SELECT id, project_id, deployment_run_id, promotion_id, task_id,
                       decision_id, deployment_receipt_id, deployment_artifact_id,
                       rollout_artifact_id, title, environment, merged_sha,
                       pull_request_url, branch_name, status, current_stage,
                       health_verification_status, rollback_decision_id, stages,
                       health_observations, rollback_reason, created_by_job_id,
                       started_at, finished_at, created_at, updated_at
                FROM deployment_rollouts
                WHERE id = :rollout_id AND project_id = :project_id
                {lock}
                """
            ),
            {"rollout_id": rollout_id, "project_id": project_id},
        ).mappings().first()
        if not row:
            raise ValueError("Deployment rollout not found.")
        return dict(row)

    def start_rollout(
        self,
        connection: Connection,
        *,
        project_id: str,
        deployment_run_id: str,
        actor: str,
        stages: Optional[list[str]] = None,
        thresholds: Optional[dict[str, Any]] = None,
        promotion_id: Optional[str] = None,
    ) -> dict[str, Any]:
        deployment_run = self._get_deployment_run(
            connection, project_id, deployment_run_id
        )
        existing = connection.execute(
            text(
                """
                SELECT id FROM deployment_rollouts
                WHERE project_id = :project_id
                  AND deployment_run_id = :deployment_run_id
                """
            ),
            {"project_id": project_id, "deployment_run_id": deployment_run_id},
        ).scalar_one_or_none()
        if existing:
            return self._get_rollout(connection, project_id, existing)

        stage_names = self._normalize_stages(stages)
        rollout_thresholds = {
            **DEFAULT_DEPLOYMENT_ROLLOUT_THRESHOLDS,
            **(thresholds or {}),
        }
        timestamp = now_utc()
        stage_state = [
            {
                "name": name,
                "status": "in_progress" if index == 0 else "pending",
                **({"started_at": timestamp.isoformat()} if index == 0 else {}),
            }
            for index, name in enumerate(stage_names)
        ]
        rollout_id = make_id("dproll")
        commit_ref = str(deployment_run.get("commit_ref") or "unknown")
        repo = str(deployment_run["repo"])
        connection.execute(
            text(
                """
                INSERT INTO deployment_rollouts (
                    id, project_id, deployment_run_id, promotion_id, title,
                    environment, merged_sha, pull_request_url, status,
                    current_stage, health_verification_status, stages,
                    health_observations, started_at
                )
                VALUES (
                    :id, :project_id, :deployment_run_id, :promotion_id, :title,
                    :environment, :merged_sha, :pull_request_url, 'in_progress',
                    :current_stage, 'pending', CAST(:stages AS JSONB),
                    '[]'::jsonb, :started_at
                )
                """
            ),
            {
                "id": rollout_id,
                "project_id": project_id,
                "deployment_run_id": deployment_run_id,
                "promotion_id": promotion_id,
                "title": (
                    f"Staged rollout for {deployment_run['app_name']} "
                    f"{deployment_run['environment']}"
                ),
                "environment": deployment_run["environment"],
                "merged_sha": commit_ref,
                "pull_request_url": f"https://github.com/{repo}/commit/{commit_ref}",
                "current_stage": stage_names[0],
                "stages": json.dumps(stage_state),
                "started_at": timestamp,
            },
        )
        # Thresholds are durable alongside stage state without requiring another table.
        connection.execute(
            text(
                """
                UPDATE deployment_rollouts
                SET health_observations = CAST(:metadata AS JSONB)
                WHERE id = :rollout_id
                """
            ),
            {
                "rollout_id": rollout_id,
                "metadata": json.dumps(
                    [{"kind": "rollout_config", "thresholds": rollout_thresholds}]
                ),
            },
        )
        emit_event(
            connection,
            project_id,
            "deployment.rollout_started",
            actor,
            "deployment_rollout",
            rollout_id,
            {
                "deployment_run_id": deployment_run_id,
                "promotion_id": promotion_id,
                "stages": stage_names,
                "thresholds": rollout_thresholds,
            },
        )
        return self._get_rollout(connection, project_id, rollout_id)

    def advance_rollout(
        self,
        connection: Connection,
        *,
        project_id: str,
        deployment_run_id: str,
        actor: str,
        observation: dict[str, Any],
    ) -> dict[str, Any]:
        rollout_id = connection.execute(
            text(
                """
                SELECT id FROM deployment_rollouts
                WHERE project_id = :project_id
                  AND deployment_run_id = :deployment_run_id
                """
            ),
            {"project_id": project_id, "deployment_run_id": deployment_run_id},
        ).scalar_one_or_none()
        if not rollout_id:
            raise ValueError("Deployment rollout has not been started.")
        rollout = self._get_rollout(
            connection, project_id, rollout_id, for_update=True
        )
        if rollout["status"] != "in_progress":
            return rollout
        deployment_run = self._get_deployment_run(
            connection, project_id, deployment_run_id
        )
        if deployment_run.get("changeset_id"):
            changeset_status = connection.execute(
                text(
                    """
                    SELECT status FROM changesets
                    WHERE id = :changeset_id AND project_id = :project_id
                    """
                ),
                {
                    "changeset_id": deployment_run["changeset_id"],
                    "project_id": project_id,
                },
            ).scalar_one_or_none()
            if changeset_status != "applied":
                raise ValueError(
                    "Deployment ChangeSet must be applied before rollout health can advance."
                )
        verified = self.health_source(dict(observation), deployment_run)
        observations = list(rollout.get("health_observations") or [])
        config = next(
            (item for item in observations if item.get("kind") == "rollout_config"),
            {"thresholds": DEFAULT_DEPLOYMENT_ROLLOUT_THRESHOLDS},
        )
        thresholds = dict(config.get("thresholds") or {})
        timestamp = now_utc()
        verified.update(
            {"stage": rollout["current_stage"], "observed_at": timestamp.isoformat()}
        )
        observations.append(verified)
        stages = [dict(item) for item in rollout.get("stages") or []]
        current_index = next(
            (
                index
                for index, item in enumerate(stages)
                if item.get("name") == rollout["current_stage"]
            ),
            0,
        )
        transition_state = build_rollout_state(
            [str(item["name"]) for item in stages], thresholds
        )
        transition_state["current_index"] = current_index
        _, transition = advance_rollout_state(
            transition_state,
            verified,
            observed_at=timestamp.isoformat(),
        )
        regression = transition["reason"]
        if regression is not None:
            return self.rollback(
                connection,
                project_id=project_id,
                deployment_run_id=deployment_run_id,
                actor=actor,
                reason=f"Auto-rollback at {rollout['current_stage']}: {regression}",
                observations=observations,
            )

        stages[current_index].update(
            {"status": "healthy", "completed_at": timestamp.isoformat()}
        )
        completed = transition["status"] == "completed"
        next_stage = str(transition["to_stage"])
        if completed:
            status = "completed"
            stages[current_index]["status"] = "completed"
        else:
            next_index = current_index + 1
            status = "in_progress"
            stages[next_index].update(
                {"status": "in_progress", "started_at": timestamp.isoformat()}
            )
        connection.execute(
            text(
                """
                UPDATE deployment_rollouts
                SET status = :status,
                    current_stage = :current_stage,
                    health_verification_status = 'passed',
                    stages = CAST(:stages AS JSONB),
                    health_observations = CAST(:observations AS JSONB),
                    finished_at = CASE WHEN :completed THEN :timestamp ELSE finished_at END,
                    updated_at = :timestamp
                WHERE id = :rollout_id
                """
            ),
            {
                "status": status,
                "current_stage": next_stage,
                "stages": json.dumps(stages),
                "observations": json.dumps(observations),
                "completed": completed,
                "timestamp": timestamp,
                "rollout_id": rollout_id,
            },
        )
        if completed:
            connection.execute(
                text(
                    """
                    UPDATE deployment_runs
                    SET status = 'healthy', finished_at = COALESCE(finished_at, :timestamp),
                        updated_at = :timestamp
                    WHERE id = :deployment_run_id AND project_id = :project_id
                    """
                ),
                {
                    "timestamp": timestamp,
                    "deployment_run_id": deployment_run_id,
                    "project_id": project_id,
                },
            )
            if rollout.get("promotion_id"):
                connection.execute(
                    text(
                        """
                        UPDATE deployment_promotions
                        SET status = 'completed', completed_at = :timestamp,
                            updated_at = :timestamp
                        WHERE id = :promotion_id AND project_id = :project_id
                        """
                    ),
                    {
                        "timestamp": timestamp,
                        "promotion_id": rollout["promotion_id"],
                        "project_id": project_id,
                    },
                )
        emit_event(
            connection,
            project_id,
            "deployment.rollout_completed" if completed else "deployment.rollout_advanced",
            actor,
            "deployment_rollout",
            rollout_id,
            {
                "deployment_run_id": deployment_run_id,
                "from_stage": rollout["current_stage"],
                "to_stage": next_stage,
                "observation": verified,
            },
        )
        return self._get_rollout(connection, project_id, rollout_id)

    def rollback(
        self,
        connection: Connection,
        *,
        project_id: str,
        deployment_run_id: str,
        actor: str,
        reason: str,
        observations: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        rollout_id = connection.execute(
            text(
                """
                SELECT id FROM deployment_rollouts
                WHERE project_id = :project_id
                  AND deployment_run_id = :deployment_run_id
                """
            ),
            {"project_id": project_id, "deployment_run_id": deployment_run_id},
        ).scalar_one_or_none()
        if not rollout_id:
            raise ValueError("Deployment rollout has not been started.")
        rollout = self._get_rollout(
            connection, project_id, rollout_id, for_update=True
        )
        if rollout["status"] == "rolled_back":
            return rollout
        timestamp = now_utc()
        stages = [dict(item) for item in rollout.get("stages") or []]
        for item in stages:
            if item.get("name") == rollout["current_stage"]:
                item.update(
                    {
                        "status": "rolled_back",
                        "completed_at": timestamp.isoformat(),
                        "reason": reason,
                    }
                )
        connection.execute(
            text(
                """
                UPDATE deployment_rollouts
                SET status = 'rolled_back', current_stage = 'rolled_back',
                    health_verification_status = 'failed',
                    rollback_reason = :reason, stages = CAST(:stages AS JSONB),
                    health_observations = CAST(:observations AS JSONB),
                    finished_at = :timestamp, updated_at = :timestamp
                WHERE id = :rollout_id
                """
            ),
            {
                "reason": reason,
                "stages": json.dumps(stages),
                "observations": json.dumps(
                    observations or rollout.get("health_observations") or []
                ),
                "timestamp": timestamp,
                "rollout_id": rollout_id,
            },
        )
        connection.execute(
            text(
                """
                UPDATE deployment_runs
                SET status = 'rollback_pending', failure_reason = :reason,
                    updated_at = :timestamp
                WHERE id = :deployment_run_id AND project_id = :project_id
                """
            ),
            {
                "reason": reason,
                "timestamp": timestamp,
                "deployment_run_id": deployment_run_id,
                "project_id": project_id,
            },
        )
        if rollout.get("promotion_id"):
            connection.execute(
                text(
                    """
                    UPDATE deployment_promotions
                    SET status = 'rolled_back', failure_reason = :reason,
                        completed_at = :timestamp, updated_at = :timestamp
                    WHERE id = :promotion_id AND project_id = :project_id
                    """
                ),
                {
                    "reason": reason,
                    "timestamp": timestamp,
                    "promotion_id": rollout["promotion_id"],
                    "project_id": project_id,
                },
            )
        emit_event(
            connection,
            project_id,
            "deployment.rolled_back",
            actor,
            "deployment_rollout",
            rollout_id,
            {
                "deployment_run_id": deployment_run_id,
                "reason": reason,
                "provider_effect": "rollback_pending_changeset",
            },
        )
        return self._get_rollout(connection, project_id, rollout_id)
