from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event, make_id, now_utc
from .deployment_rollout_engine import DeploymentRolloutEngine
from .pipeline_adapter import GitHubActionsPipeline, make_pipeline_adapter
from .resource_plane import ResourcePlaneService


TERMINAL_PROMOTION_STATES = {"blocked", "completed", "rolled_back", "cancelled"}


class PromotionOrchestrator:
    def __init__(
        self,
        settings: Settings,
        *,
        resource_plane: Optional[ResourcePlaneService] = None,
        rollout_engine: Optional[DeploymentRolloutEngine] = None,
        pipeline: Optional[GitHubActionsPipeline] = None,
    ):
        self.settings = settings
        self.resource_plane = resource_plane or ResourcePlaneService(settings)
        self.rollout_engine = rollout_engine or DeploymentRolloutEngine(settings)
        self.pipeline = pipeline or make_pipeline_adapter(settings)

    def _pipeline_for(self, repository: Any) -> Any:
        """The repository's own pipeline: a local project's is BOSS's sandbox (it already ran),
        never GitHub Actions — a local project's promotion makes no GitHub call."""
        from . import local_repo

        full = str(repository or "")
        if local_repo.is_local(full) and "/" in full:
            from .local_repo_host import LocalGitAdapter, LocalSandboxPipeline

            owner, _, repo = full.partition("/")
            return LocalSandboxPipeline(self.settings,
                                        LocalGitAdapter(self.settings, owner=owner, repo=repo))
        return self.pipeline

    @staticmethod
    def _serialize(row: Any) -> dict[str, Any]:
        return dict(row)

    def _get_promotion(
        self,
        connection: Connection,
        project_id: str,
        promotion_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any]:
        lock = "FOR UPDATE" if for_update else ""
        row = connection.execute(
            text(
                f"""
                SELECT id, project_id, app_name, repo, commit_ref,
                       staging_target_id, production_target_id,
                       staging_deployment_run_id, production_deployment_run_id,
                       production_rollout_id, pipeline_provider,
                       pipeline_descriptor, pipeline_verdict, gate_result,
                       status, failure_reason, created_by, created_at,
                       updated_at, completed_at
                FROM deployment_promotions
                WHERE id = :promotion_id AND project_id = :project_id
                {lock}
                """
            ),
            {"promotion_id": promotion_id, "project_id": project_id},
        ).mappings().first()
        if not row:
            raise ValueError("Deployment promotion not found.")
        return self._serialize(row)

    def _block(
        self,
        connection: Connection,
        *,
        project_id: str,
        promotion_id: str,
        actor: str,
        reason: str,
        descriptor: Optional[dict[str, Any]] = None,
        verdict: Optional[dict[str, Any]] = None,
        gate_result: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        timestamp = now_utc()
        connection.execute(
            text(
                """
                UPDATE deployment_promotions
                SET status = 'blocked', failure_reason = :reason,
                    pipeline_descriptor = COALESCE(CAST(:descriptor AS JSONB), pipeline_descriptor),
                    pipeline_verdict = COALESCE(CAST(:verdict AS JSONB), pipeline_verdict),
                    gate_result = COALESCE(CAST(:gate_result AS JSONB), gate_result),
                    completed_at = :timestamp, updated_at = :timestamp
                WHERE id = :promotion_id AND project_id = :project_id
                """
            ),
            {
                "reason": reason,
                "descriptor": json.dumps(descriptor) if descriptor is not None else None,
                "verdict": json.dumps(verdict) if verdict is not None else None,
                "gate_result": (
                    json.dumps(gate_result) if gate_result is not None else None
                ),
                "timestamp": timestamp,
                "promotion_id": promotion_id,
                "project_id": project_id,
            },
        )
        emit_event(
            connection,
            project_id,
            "deployment.promotion_blocked",
            actor,
            "deployment_promotion",
            promotion_id,
            {"reason": reason, "gate_result": gate_result or {}},
        )
        return self.get_promotion_detail(connection, project_id, promotion_id)

    def promote(
        self,
        connection: Connection,
        *,
        project_id: str,
        staging_target_id: str,
        production_target_id: str,
        commit_ref: str,
        actor: str,
        staging_reservation_id: Optional[str] = None,
        production_reservation_id: Optional[str] = None,
        staging_expected_cost_usd_micros: int = 0,
        production_expected_cost_usd_micros: int = 0,
        release_packet: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        normalized_ref = commit_ref.strip()
        if not normalized_ref:
            raise ValueError("commit_ref is required.")
        staging = self.resource_plane.get_deployment_target(
            connection, project_id, staging_target_id
        )
        production = self.resource_plane.get_deployment_target(
            connection, project_id, production_target_id
        )
        if staging["environment"] != "staging":
            raise ValueError("staging_target_id must reference a staging target.")
        if production["environment"] != "production":
            raise ValueError("production_target_id must reference a production target.")
        if staging["app_name"] != production["app_name"] or staging["repo"] != production["repo"]:
            raise ValueError("Promotion targets must belong to the same app and repository.")
        pipeline = self._pipeline_for(staging["repo"])

        promotion_id = make_id("prom")
        connection.execute(
            text(
                """
                INSERT INTO deployment_promotions (
                    id, project_id, app_name, repo, commit_ref,
                    staging_target_id, production_target_id, pipeline_provider,
                    status, created_by
                )
                VALUES (
                    :id, :project_id, :app_name, :repo, :commit_ref,
                    :staging_target_id, :production_target_id, :pipeline_provider,
                    'evaluating_pipeline', :actor
                )
                """
            ),
            {
                "id": promotion_id,
                "project_id": project_id,
                "app_name": staging["app_name"],
                "repo": staging["repo"],
                "commit_ref": normalized_ref,
                "staging_target_id": staging_target_id,
                "production_target_id": production_target_id,
                "pipeline_provider": pipeline.provider,
                "actor": actor,
            },
        )

        production_policy = self.resource_plane.deployment_rollout_policy_for_target(
            connection, project_id, production
        )
        if not production_policy:
            return self._block(
                connection,
                project_id=project_id,
                promotion_id=promotion_id,
                actor=actor,
                reason="production_target_requires_explicit_environment_policy",
            )
        production_policy_reason = (
            self.resource_plane.deployment_rollout_policy_block_reason(
                production,
                production_policy,
                self.resource_plane.deployment_rollback_plan(production),
                production_reservation_id,
            )
        )
        if production_policy_reason:
            return self._block(
                connection,
                project_id=project_id,
                promotion_id=promotion_id,
                actor=actor,
                reason=production_policy_reason,
            )

        descriptor = pipeline.discover(staging["repo"])
        descriptor_data = descriptor.to_dict()
        if not descriptor.configured:
            return self._block(
                connection,
                project_id=project_id,
                promotion_id=promotion_id,
                actor=actor,
                reason="pipeline_missing_generate_config_pr_first",
                descriptor=descriptor_data,
            )
        verdict = pipeline.wait_for_verdict(
            normalized_ref,
            timeout_seconds=self.settings.sandbox_ci_timeout_seconds,
            poll_interval_seconds=self.settings.sandbox_ci_poll_interval_seconds,
        )
        verdict_data = verdict.to_dict()
        if verdict.status != "passed":
            return self._block(
                connection,
                project_id=project_id,
                promotion_id=promotion_id,
                actor=actor,
                reason=f"pipeline_verdict_{verdict.status}",
                descriptor=descriptor_data,
                verdict=verdict_data,
            )

        staging_run = self.resource_plane.plan_deployment_run(
            connection,
            project_id=project_id,
            deployment_target_id=staging_target_id,
            commit_ref=normalized_ref,
            release_packet={
                **(release_packet or {}),
                "promotion_id": promotion_id,
                "pipeline_verdict": verdict_data,
            },
            expected_cost_usd_micros=staging_expected_cost_usd_micros,
            reservation_id=staging_reservation_id,
            actor=actor,
        )
        if staging_run["status"] == "failed":
            return self._block(
                connection,
                project_id=project_id,
                promotion_id=promotion_id,
                actor=actor,
                reason=staging_run.get("failure_reason") or "staging_plan_failed",
                descriptor=descriptor_data,
                verdict=verdict_data,
            )
        connection.execute(
            text(
                """
                UPDATE deployment_promotions
                SET staging_deployment_run_id = :staging_run_id,
                    pipeline_descriptor = CAST(:descriptor AS JSONB),
                    pipeline_verdict = CAST(:verdict AS JSONB),
                    status = 'staging_planned', updated_at = now()
                WHERE id = :promotion_id AND project_id = :project_id
                """
            ),
            {
                "staging_run_id": staging_run["id"],
                "descriptor": json.dumps(descriptor_data),
                "verdict": json.dumps(verdict_data),
                "promotion_id": promotion_id,
                "project_id": project_id,
            },
        )
        # Store production inputs for the post-staging gate without exposing secrets.
        connection.execute(
            text(
                """
                UPDATE deployment_promotions
                SET gate_result = CAST(:gate_result AS JSONB)
                WHERE id = :promotion_id
                """
            ),
            {
                "promotion_id": promotion_id,
                "gate_result": json.dumps(
                    {
                        "status": "waiting",
                        "production_reservation_id": production_reservation_id,
                        "production_expected_cost_usd_micros": production_expected_cost_usd_micros,
                        "release_packet": release_packet or {},
                    }
                ),
            },
        )
        emit_event(
            connection,
            project_id,
            "deployment.staging_planned",
            actor,
            "deployment_promotion",
            promotion_id,
            {
                "staging_deployment_run_id": staging_run["id"],
                "pipeline_verdict": verdict_data,
            },
        )
        return self.get_promotion_detail(connection, project_id, promotion_id)

    def advance_after_staging_gate(
        self,
        connection: Connection,
        *,
        project_id: str,
        promotion_id: str,
        actor: str,
    ) -> dict[str, Any]:
        promotion = self._get_promotion(
            connection, project_id, promotion_id, for_update=True
        )
        if promotion["status"] in TERMINAL_PROMOTION_STATES or promotion[
            "status"
        ] == "production_planned":
            return self.get_promotion_detail(connection, project_id, promotion_id)
        if promotion["status"] != "staging_planned" or not promotion[
            "staging_deployment_run_id"
        ]:
            raise ValueError("Promotion is not waiting on a staging gate.")
        health = self.resource_plane.run_deployment_health_checks(
            connection,
            project_id=project_id,
            deployment_run_id=promotion["staging_deployment_run_id"],
            actor=actor,
        )
        staging_run = health["deployment_run"]
        gate_result = {
            "status": "passed" if staging_run["status"] == "healthy" else "failed",
            "deployment_run_id": staging_run["id"],
            "health_artifact_id": health["health_artifact_id"],
            "rollback_artifact_id": health["rollback_artifact_id"],
            "decision_id": health["decision_id"],
            "health_check_results": staging_run["health_check_results"],
        }
        prior_gate = dict(promotion.get("gate_result") or {})
        if gate_result["status"] != "passed":
            return self._block(
                connection,
                project_id=project_id,
                promotion_id=promotion_id,
                actor=actor,
                reason="staging_health_gate_failed",
                gate_result={**prior_gate, **gate_result},
            )
        emit_event(
            connection,
            project_id,
            "deployment.staging_ok",
            actor,
            "deployment_promotion",
            promotion_id,
            gate_result,
        )

        production_run = self.resource_plane.plan_deployment_run(
            connection,
            project_id=project_id,
            deployment_target_id=promotion["production_target_id"],
            commit_ref=promotion["commit_ref"],
            release_packet={
                **dict(prior_gate.get("release_packet") or {}),
                "promotion_id": promotion_id,
                "staging_gate": gate_result,
            },
            expected_cost_usd_micros=int(
                prior_gate.get("production_expected_cost_usd_micros") or 0
            ),
            reservation_id=prior_gate.get("production_reservation_id"),
            actor=actor,
        )
        if production_run["status"] == "failed":
            return self._block(
                connection,
                project_id=project_id,
                promotion_id=promotion_id,
                actor=actor,
                reason=production_run.get("failure_reason") or "production_plan_failed",
                gate_result={**prior_gate, **gate_result},
            )
        rollout = self.rollout_engine.start_rollout(
            connection,
            project_id=project_id,
            deployment_run_id=production_run["id"],
            actor=actor,
            promotion_id=promotion_id,
        )
        connection.execute(
            text(
                """
                UPDATE deployment_promotions
                SET production_deployment_run_id = :production_run_id,
                    production_rollout_id = :rollout_id,
                    gate_result = CAST(:gate_result AS JSONB),
                    status = 'production_planned', updated_at = now()
                WHERE id = :promotion_id AND project_id = :project_id
                """
            ),
            {
                "production_run_id": production_run["id"],
                "rollout_id": rollout["id"],
                "gate_result": json.dumps({**prior_gate, **gate_result}),
                "promotion_id": promotion_id,
                "project_id": project_id,
            },
        )
        emit_event(
            connection,
            project_id,
            "deployment.promoted",
            actor,
            "deployment_promotion",
            promotion_id,
            {
                "staging_deployment_run_id": staging_run["id"],
                "production_deployment_run_id": production_run["id"],
                "production_rollout_id": rollout["id"],
            },
        )
        return self.get_promotion_detail(connection, project_id, promotion_id)

    def list_promotions(
        self, connection: Connection, project_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id FROM deployment_promotions
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).scalars().all()
        return [
            self.get_promotion_detail(connection, project_id, promotion_id)
            for promotion_id in rows
        ]

    def get_promotion_detail(
        self, connection: Connection, project_id: str, promotion_id: str
    ) -> dict[str, Any]:
        promotion = self._get_promotion(connection, project_id, promotion_id)
        staging_run = (
            self.resource_plane.get_deployment_run(
                connection, project_id, promotion["staging_deployment_run_id"]
            )
            if promotion.get("staging_deployment_run_id")
            else None
        )
        production_run = (
            self.resource_plane.get_deployment_run(
                connection, project_id, promotion["production_deployment_run_id"]
            )
            if promotion.get("production_deployment_run_id")
            else None
        )
        rollout = (
            self.rollout_engine._get_rollout(
                connection, project_id, promotion["production_rollout_id"]
            )
            if promotion.get("production_rollout_id")
            else None
        )
        entity_ids = [
            value
            for value in (
                promotion_id,
                promotion.get("staging_deployment_run_id"),
                promotion.get("production_deployment_run_id"),
                promotion.get("production_rollout_id"),
            )
            if value
        ]
        events = connection.execute(
            text(
                """
                SELECT id, event_type, actor, entity_type, entity_id, data, created_at
                FROM events
                WHERE project_id = :project_id AND entity_id = ANY(:entity_ids)
                ORDER BY created_at ASC
                """
            ),
            {"project_id": project_id, "entity_ids": entity_ids},
        ).mappings().all()
        timeline = [
            {
                "id": f"pipeline:{promotion_id}",
                "event_type": "pipeline.verdict",
                "actor": "pipeline_adapter",
                "entity_type": "deployment_promotion",
                "entity_id": promotion_id,
                "data": promotion.get("pipeline_verdict") or {},
                "created_at": promotion["created_at"],
            },
            *[dict(event) for event in events],
        ]
        return {
            **promotion,
            "staging_deployment_run": staging_run,
            "production_deployment_run": production_run,
            "production_rollout": rollout,
            "timeline": timeline,
        }
