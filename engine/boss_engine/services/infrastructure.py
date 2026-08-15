from __future__ import annotations

import json
import re
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .budget import BudgetGovernor
from .core import (
    create_artifact,
    create_blocking_decision,
    emit_event,
    expires_at,
    json_hash,
    make_id,
    normalize_data_boundary,
    now_utc,
)
from .digitalocean_adapter import DigitalOceanAdapter
from .github_adapter import IntegrationConfigurationError
from .receipts import ReceiptService


class InfraResourceNotFoundError(RuntimeError):
    pass


class InfrastructureService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.budget = BudgetGovernor(settings)
        self.receipts = ReceiptService()
        self.digitalocean = DigitalOceanAdapter(settings)

    def request_droplet_provision(
        self,
        connection: Connection,
        *,
        project_id: str,
        task_id: Optional[str],
        reservation_id: str,
        title: str,
        region: str,
        size: str,
        ttl_minutes: int,
        tags: list[str],
        actor: str,
        data_boundary: Optional[str] = None,
    ) -> dict[str, Any]:
        normalized_title = title.strip()
        normalized_region = region.strip()
        normalized_size = size.strip()
        normalized_tags = self._normalize_tags(tags)
        self._validate_ttl(ttl_minutes)
        boundary = self._resolve_boundary(connection, task_id, data_boundary)
        artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            task_id,
            "infra_provision_request",
            f"Infrastructure provisioning request: {normalized_title}",
            {
                "title": normalized_title,
                "region": normalized_region,
                "size": normalized_size,
                "ttl_minutes": ttl_minutes,
                "tags": normalized_tags,
                "reservation_id": reservation_id,
                "data_boundary": boundary,
            },
            data_boundary=boundary,
        )
        decision_id = self._create_provision_review_decision(
            connection,
            project_id=project_id,
            task_id=task_id,
            reservation_id=reservation_id,
            artifact_id=artifact_id,
            title=normalized_title,
            region=normalized_region,
            size=normalized_size,
            ttl_minutes=ttl_minutes,
            tags=normalized_tags,
            data_boundary=boundary,
        )
        emit_event(
            connection,
            project_id,
            "infra.provision_review_requested",
            actor,
            "decision",
            decision_id,
            {
                "artifact_id": artifact_id,
                "reservation_id": reservation_id,
                "region": normalized_region,
                "size": normalized_size,
                "ttl_minutes": ttl_minutes,
                "data_boundary": boundary,
            },
        )
        return {
            "status": "pending_approval",
            "artifact_id": artifact_id,
            "decision_id": decision_id,
            "data_boundary": boundary,
        }

    def create_droplet(
        self,
        connection: Connection,
        *,
        project_id: str,
        task_id: Optional[str],
        decision_id: str,
        reservation_id: str,
        dedupe_key: str,
        title: str,
        region: str,
        size: str,
        ttl_minutes: int,
        tags: list[str],
        created_by_job_id: Optional[str] = None,
        data_boundary: Optional[str] = None,
    ) -> dict[str, Any]:
        self._assert_decision_ready(connection, project_id, decision_id)
        normalized_title = title.strip()
        normalized_region = region.strip()
        normalized_size = size.strip()
        normalized_tags = self._normalize_tags(tags)
        self._validate_ttl(ttl_minutes)
        boundary = self._resolve_boundary(connection, task_id, data_boundary)
        reservation = self.budget.assert_active_reservation(
            connection,
            project_id,
            reservation_id,
        )
        if not reservation:
            decision_id_block = create_blocking_decision(
                connection,
                project_id,
                task_id,
                "Missing active reservation for infrastructure provisioning",
                {"action": "create_budget_reservation"},
                {"decision_id": decision_id, "reservation_id": reservation_id},
                "missing_reservation",
                risk_tier=3,
            )
            emit_event(
                connection,
                project_id,
                "infra.provision_blocked",
                "infra_worker",
                "decision",
                decision_id_block,
                {"reason": "missing_reservation", "reservation_id": reservation_id},
            )
            return {"status": "blocked", "decision_id": decision_id_block}
        if boundary in {"restricted", "local-only"}:
            self.budget.release(
                connection,
                project_id,
                reservation_id,
                "Released reservation because data-boundary policy blocked the infra effect.",
            )
            decision_id_block = create_blocking_decision(
                connection,
                project_id,
                task_id,
                "Data boundary blocks external infrastructure provisioning",
                {
                    "action": "reclassify_or_configure_approved_boundary_adapter",
                    "data_boundary": boundary,
                },
                {"decision_id": decision_id},
                "data_boundary_blocks_external_effect",
                risk_tier=3,
            )
            emit_event(
                connection,
                project_id,
                "policy.denied",
                "infra_worker",
                "infra_effect",
                "infra.digitalocean.droplet.create",
                {
                    "reason": "data_boundary_not_allowed_for_infra",
                    "data_boundary": boundary,
                    "decision_id": decision_id_block,
                },
            )
            return {
                "status": "blocked",
                "decision_id": decision_id_block,
                "data_boundary": boundary,
                "message": (
                    f"{boundary} data cannot be sent to external infrastructure "
                    "providers without an approved boundary adapter."
                ),
            }

        scope = self._scope(connection, project_id)
        receipt, created = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key,
            "infra.digitalocean.droplet.create",
            {
                "project_id": project_id,
                "decision_id": decision_id,
                "region": normalized_region,
                "size": normalized_size,
                "data_boundary": boundary,
            },
            {
                "title": normalized_title,
                "region": normalized_region,
                "size": normalized_size,
                "ttl_minutes": ttl_minutes,
                "tags": normalized_tags,
                "reservation_id": reservation_id,
                "data_boundary": boundary,
            },
            created_by_job_id=created_by_job_id,
            data_boundary=boundary,
        )
        if not created and receipt["status"] == "succeeded":
            return self._reused_create_result(
                connection,
                project_id,
                receipt["id"],
                boundary,
            )

        if not bool(getattr(self.settings, "infra_real_provision_allowed", False)):
            message = (
                "BOSS_INFRA_REAL_PROVISION_ALLOWED is false. "
                "Real DigitalOcean writes stay fail-closed."
            )
            self.receipts.mark_failed(connection, receipt["id"], {"message": message})
            self.budget.release(
                connection,
                project_id,
                reservation_id,
                "Released reservation because real infra provision is not allowed.",
            )
            resource_receipt_id = self._record_resource_receipt(
                connection,
                project_id=project_id,
                receipt_id=receipt["id"],
                dedupe_key=f"{dedupe_key}:resource",
                provider="digitalocean",
                action="create",
                resource_type="droplet",
                resource_ref=normalized_title,
                status="failed",
                error={"message": message, "reason": "infra_real_provision_not_allowed"},
                created_by_job_id=created_by_job_id,
            )
            decision_id_block = create_blocking_decision(
                connection,
                project_id,
                task_id,
                "Real infrastructure provision is disabled",
                {"action": "set_BOSS_INFRA_REAL_PROVISION_ALLOWED"},
                {"decision_id": decision_id, "receipt_id": receipt["id"]},
                "infra_real_provision_not_allowed",
                risk_tier=3,
            )
            emit_event(
                connection,
                project_id,
                "infra.provision_blocked",
                "infra_worker",
                "decision",
                decision_id_block,
                {
                    "reason": "infra_real_provision_not_allowed",
                    "receipt_id": receipt["id"],
                    "resource_receipt_id": resource_receipt_id,
                },
            )
            return {
                "status": "blocked",
                "decision_id": decision_id_block,
                "receipt_id": receipt["id"],
                "resource_receipt_id": resource_receipt_id,
                "message": message,
            }

        resource_name = self._resource_name(receipt["id"])
        provider_tags = [
            *self.settings.digitalocean_tag_values,
            f"boss-resource-{receipt['id'][-8:]}",
            f"boss-exp-{expires_at(ttl_minutes).strftime('%Y%m%d%H%M')}",
        ]
        try:
            droplet = self.digitalocean.create_droplet(
                name=resource_name,
                region=normalized_region,
                size=normalized_size,
                tags=list(provider_tags),
                user_tags=normalized_tags,
            )
        except IntegrationConfigurationError as error:
            self.receipts.mark_failed(connection, receipt["id"], {"message": str(error)})
            self.budget.release(
                connection,
                project_id,
                reservation_id,
                "Released reservation because infra provisioning is disabled or unconfigured.",
            )
            decision_id_block = create_blocking_decision(
                connection,
                project_id,
                task_id,
                "Infrastructure provisioning is blocked by policy or missing configuration",
                {"action": "configure_or_enable_infra_provisioning"},
                {"decision_id": decision_id},
                "infra_provision_blocked",
                risk_tier=3,
            )
            emit_event(
                connection,
                project_id,
                "infra.provision_blocked",
                "infra_worker",
                "decision",
                decision_id_block,
                {"message": str(error), "receipt_id": receipt["id"]},
            )
            return {
                "status": "blocked",
                "decision_id": decision_id_block,
                "receipt_id": receipt["id"],
                "message": str(error),
            }
        except Exception as error:
            self.receipts.mark_failed(connection, receipt["id"], {"message": str(error)})
            self.budget.release(
                connection,
                project_id,
                reservation_id,
                "Released reservation because infra provisioning failed before acknowledgement.",
            )
            emit_event(
                connection,
                project_id,
                "infra.provision_failed",
                "infra_worker",
                "receipt",
                receipt["id"],
                {"message": str(error)},
            )
            return {
                "status": "failed",
                "receipt_id": receipt["id"],
                "message": str(error),
            }

        artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            task_id,
            "infra_provision_result",
            f"Infrastructure provisioned: {normalized_title}",
            {
                "title": normalized_title,
                "provider": "digitalocean",
                "resource_type": "droplet",
                "resource_name": resource_name,
                "droplet_id": droplet.droplet_id,
                "api_url": droplet.api_url,
                "region": droplet.region,
                "size": droplet.size,
                "status": droplet.status,
                "ttl_minutes": ttl_minutes,
                "tags": normalized_tags,
                "decision_id": decision_id,
                "reservation_id": reservation_id,
                "receipt_id": receipt["id"],
                "data_boundary": boundary,
            },
            created_by_job_id=created_by_job_id,
            data_boundary=boundary,
        )
        resource_id = make_id("infra")
        expires_at_value = expires_at(ttl_minutes)
        connection.execute(
            text(
                """
                INSERT INTO infra_resources (
                    id, project_id, task_id, decision_id, provider, resource_type,
                    resource_name, external_resource_id, region, size, image, tags,
                    status, ttl_minutes, expires_at, provision_receipt_id,
                    provision_artifact_id, data_boundary, metadata, created_by_job_id
                )
                VALUES (
                    :id, :project_id, :task_id, :decision_id, 'digitalocean', 'droplet',
                    :resource_name, :external_resource_id, :region, :size, :image,
                    CAST(:tags AS JSONB), 'active', :ttl_minutes, :expires_at,
                    :provision_receipt_id, :provision_artifact_id, :data_boundary,
                    CAST(:metadata AS JSONB), :created_by_job_id
                )
                """
            ),
            {
                "id": resource_id,
                "project_id": project_id,
                "task_id": task_id,
                "decision_id": decision_id,
                "resource_name": resource_name,
                "external_resource_id": droplet.droplet_id,
                "region": droplet.region,
                "size": droplet.size,
                "image": self.settings.digitalocean_default_image,
                "tags": json.dumps(normalized_tags),
                "ttl_minutes": ttl_minutes,
                "expires_at": expires_at_value,
                "provision_receipt_id": receipt["id"],
                "provision_artifact_id": artifact_id,
                "data_boundary": boundary,
                "metadata": json.dumps({"provider_url": droplet.api_url}),
                "created_by_job_id": created_by_job_id,
            },
        )
        self.receipts.mark_succeeded(
            connection,
            receipt["id"],
            droplet.api_url,
            {
                "resource_id": resource_id,
                "droplet_id": droplet.droplet_id,
                "resource_name": resource_name,
                "expires_at": expires_at_value.isoformat(),
            },
        )
        self.budget.settle(
            connection,
            project_id,
            reservation_id,
            receipt["id"],
            "infra",
            "infra.do.create_droplet_real",
            int(reservation["amount_usd_micros"]),
            None,
        )
        emit_event(
            connection,
            project_id,
            "infra.provisioned",
            "infra_worker",
            "infra_resource",
            resource_id,
            {
                "receipt_id": receipt["id"],
                "artifact_id": artifact_id,
                "droplet_id": droplet.droplet_id,
                "expires_at": expires_at_value.isoformat(),
            },
        )
        return {
            "status": "created",
            "resource_id": resource_id,
            "receipt_id": receipt["id"],
            "artifact_id": artifact_id,
            "decision_id": decision_id,
            "provider_ref": droplet.api_url,
            "provider_resource_id": droplet.droplet_id,
            "resource_name": resource_name,
            "expires_at": expires_at_value.isoformat(),
            "data_boundary": boundary,
        }

    def queue_droplet_termination(
        self,
        connection: Connection,
        *,
        project_id: str,
        resource_id: str,
        dedupe_key: str,
        actor: str,
        priority: int,
        max_attempts: int,
        reason: Optional[str] = None,
    ) -> dict[str, Any]:
        resource = self.get_resource(connection, project_id, resource_id)
        if resource["status"] == "terminated":
            raise ValueError("Infrastructure resource is already terminated.")
        if resource["status"] == "termination_queued":
            raise ValueError("Infrastructure resource termination is already queued.")
        run_after_value = now_utc()
        connection.execute(
            text(
                """
                UPDATE infra_resources
                SET status = 'termination_queued',
                    termination_reason = :reason,
                    updated_at = now()
                WHERE id = :resource_id
                """
            ),
            {"resource_id": resource_id, "reason": reason},
        )
        job_id = make_id("job")
        connection.execute(
            text(
                """
                INSERT INTO jobs (
                    id, project_id, task_id, kind, priority, status, payload,
                    data_boundary, max_attempts, attempt, run_after
                )
                VALUES (
                    :id, :project_id, :task_id, 'infra.droplet.delete', :priority, 'queued',
                    CAST(:payload AS JSONB), :data_boundary, :max_attempts, 0, :run_after
                )
                """
            ),
            {
                "id": job_id,
                "project_id": project_id,
                "task_id": resource.get("task_id"),
                "priority": priority,
                "payload": json.dumps(
                    {
                        "project_id": project_id,
                        "resource_id": resource_id,
                        "dedupe_key": dedupe_key,
                        "reason": reason,
                    }
                ),
                "data_boundary": resource["data_boundary"],
                "max_attempts": max_attempts,
                "run_after": run_after_value,
            },
        )
        emit_event(
            connection,
            project_id,
            "job.created",
            "control_plane",
            "job",
            job_id,
            {
                "kind": "infra.droplet.delete",
                "task_id": resource.get("task_id"),
                "data_boundary": resource["data_boundary"],
            },
        )
        emit_event(
            connection,
            project_id,
            "infra.termination_queued",
            actor,
            "infra_resource",
            resource_id,
            {"job_id": job_id, "reason": reason},
        )
        return {
            "id": job_id,
            "project_id": project_id,
            "task_id": resource.get("task_id"),
            "kind": "infra.droplet.delete",
            "status": "queued",
            "data_boundary": resource["data_boundary"],
            "priority": priority,
            "max_attempts": max_attempts,
            "attempt": 0,
            "run_after": run_after_value.isoformat(),
        }

    def terminate_droplet(
        self,
        connection: Connection,
        *,
        project_id: str,
        resource_id: str,
        dedupe_key: str,
        reason: Optional[str] = None,
        created_by_job_id: Optional[str] = None,
    ) -> dict[str, Any]:
        resource = self.get_resource(connection, project_id, resource_id)
        if resource["status"] == "terminated":
            return {
                "status": "reused",
                "resource_id": resource_id,
                "receipt_id": resource.get("termination_receipt_id"),
                "artifact_id": resource.get("termination_artifact_id"),
            }
        scope = self._scope(connection, project_id)
        receipt, created = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key,
            "infra.digitalocean.droplet.delete",
            {
                "project_id": project_id,
                "resource_id": resource_id,
                "provider": resource["provider"],
                "data_boundary": resource["data_boundary"],
            },
            {
                "resource_id": resource_id,
                "external_resource_id": resource["external_resource_id"],
                "reason": reason,
                "data_boundary": resource["data_boundary"],
            },
            created_by_job_id=created_by_job_id,
            data_boundary=resource["data_boundary"],
        )
        if not created and receipt["status"] == "succeeded":
            return {
                "status": "reused",
                "resource_id": resource_id,
                "receipt_id": receipt["id"],
                "artifact_id": resource.get("termination_artifact_id"),
                "provider_ref": receipt.get("provider_ref"),
            }
        try:
            result = self.digitalocean.delete_droplet(resource["external_resource_id"])
        except IntegrationConfigurationError as error:
            self.receipts.mark_failed(connection, receipt["id"], {"message": str(error)})
            connection.execute(
                text(
                    """
                    UPDATE infra_resources
                    SET status = 'active',
                        updated_at = now()
                    WHERE id = :resource_id
                    """
                ),
                {"resource_id": resource_id},
            )
            decision_id = create_blocking_decision(
                connection,
                project_id,
                resource.get("task_id"),
                "Infrastructure termination is blocked by policy or missing configuration",
                {"action": "configure_or_enable_infra_provisioning"},
                {"resource_id": resource_id},
                "infra_termination_blocked",
                risk_tier=2,
            )
            emit_event(
                connection,
                project_id,
                "infra.termination_blocked",
                "infra_worker",
                "infra_resource",
                resource_id,
                {"decision_id": decision_id, "message": str(error)},
            )
            return {
                "status": "blocked",
                "decision_id": decision_id,
                "receipt_id": receipt["id"],
                "message": str(error),
            }
        except Exception as error:
            self.receipts.mark_failed(connection, receipt["id"], {"message": str(error)})
            connection.execute(
                text(
                    """
                    UPDATE infra_resources
                    SET updated_at = now()
                    WHERE id = :resource_id
                    """
                ),
                {"resource_id": resource_id},
            )
            emit_event(
                connection,
                project_id,
                "infra.termination_failed",
                "infra_worker",
                "receipt",
                receipt["id"],
                {"resource_id": resource_id, "message": str(error)},
            )
            return {
                "status": "failed",
                "receipt_id": receipt["id"],
                "message": str(error),
            }

        artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            resource.get("task_id"),
            "infra_termination_result",
            f"Infrastructure terminated: {resource['resource_name']}",
            {
                "resource_id": resource_id,
                "provider": resource["provider"],
                "external_resource_id": resource["external_resource_id"],
                "provider_ref": result.api_url,
                "reason": reason,
                "receipt_id": receipt["id"],
                "data_boundary": resource["data_boundary"],
            },
            created_by_job_id=created_by_job_id,
            data_boundary=resource["data_boundary"],
        )
        connection.execute(
            text(
                """
                UPDATE infra_resources
                SET status = 'terminated',
                    terminated_at = now(),
                    termination_reason = :reason,
                    termination_receipt_id = :termination_receipt_id,
                    termination_artifact_id = :termination_artifact_id,
                    updated_by_job_id = :updated_by_job_id,
                    updated_at = now()
                WHERE id = :resource_id
                """
            ),
            {
                "resource_id": resource_id,
                "reason": reason,
                "termination_receipt_id": receipt["id"],
                "termination_artifact_id": artifact_id,
                "updated_by_job_id": created_by_job_id,
            },
        )
        self.receipts.mark_succeeded(
            connection,
            receipt["id"],
            result.api_url,
            {"resource_id": resource_id, "external_resource_id": result.droplet_id},
        )
        emit_event(
            connection,
            project_id,
            "infra.terminated",
            "infra_worker",
            "infra_resource",
            resource_id,
            {"receipt_id": receipt["id"], "artifact_id": artifact_id, "reason": reason},
        )
        return {
            "status": "terminated",
            "resource_id": resource_id,
            "receipt_id": receipt["id"],
            "artifact_id": artifact_id,
            "provider_ref": result.api_url,
        }

    def queue_droplet_provision(
        self,
        connection: Connection,
        *,
        project_id: str,
        task_id: Optional[str],
        decision_id: str,
        reservation_id: str,
        dedupe_key: str,
        title: str,
        region: str,
        size: str,
        ttl_minutes: int,
        tags: list[str],
        actor: str,
        priority: int,
        max_attempts: int,
        data_boundary: Optional[str] = None,
    ) -> dict[str, Any]:
        self._assert_decision_ready(connection, project_id, decision_id)
        normalized_title = title.strip()
        normalized_region = region.strip()
        normalized_size = size.strip()
        normalized_tags = self._normalize_tags(tags)
        self._validate_ttl(ttl_minutes)
        boundary = self._resolve_boundary(connection, task_id, data_boundary)
        run_after_value = now_utc()
        job_id = make_id("job")
        connection.execute(
            text(
                """
                INSERT INTO jobs (
                    id, project_id, task_id, kind, priority, status, payload,
                    data_boundary, max_attempts, attempt, run_after
                )
                VALUES (
                    :id, :project_id, :task_id, 'infra.droplet.create', :priority, 'queued',
                    CAST(:payload AS JSONB), :data_boundary, :max_attempts, 0, :run_after
                )
                """
            ),
            {
                "id": job_id,
                "project_id": project_id,
                "task_id": task_id,
                "priority": priority,
                "payload": json.dumps(
                    {
                        "project_id": project_id,
                        "task_id": task_id,
                        "decision_id": decision_id,
                        "reservation_id": reservation_id,
                        "dedupe_key": dedupe_key,
                        "title": normalized_title,
                        "region": normalized_region,
                        "size": normalized_size,
                        "ttl_minutes": ttl_minutes,
                        "tags": normalized_tags,
                        "requires_reservation": True,
                        "data_boundary": boundary,
                    }
                ),
                "data_boundary": boundary,
                "max_attempts": max_attempts,
                "run_after": run_after_value,
            },
        )
        emit_event(
            connection,
            project_id,
            "job.created",
            "control_plane",
            "job",
            job_id,
            {
                "kind": "infra.droplet.create",
                "task_id": task_id,
                "data_boundary": boundary,
            },
        )
        emit_event(
            connection,
            project_id,
            "infra.provision_job_queued",
            actor,
            "job",
            job_id,
            {"decision_id": decision_id, "reservation_id": reservation_id},
        )
        self.budget.attach_job(connection, project_id, reservation_id, job_id)
        return {
            "id": job_id,
            "project_id": project_id,
            "task_id": task_id,
            "kind": "infra.droplet.create",
            "status": "queued",
            "data_boundary": boundary,
            "priority": priority,
            "max_attempts": max_attempts,
            "attempt": 0,
            "run_after": run_after_value.isoformat(),
        }

    def queue_expired_resource_terminations(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        rows = connection.execute(
            text(
                """
                SELECT id
                FROM infra_resources
                WHERE project_id = :project_id
                  AND status = 'active'
                  AND expires_at <= now()
                ORDER BY expires_at ASC, created_at ASC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        job_ids: list[str] = []
        resource_ids: list[str] = []
        for row in rows:
            resource_id = str(row["id"])
            resource_ids.append(resource_id)
            queued = self.queue_droplet_termination(
                connection,
                project_id=project_id,
                resource_id=resource_id,
                dedupe_key=f"janitor-terminate-{resource_id}",
                actor=actor,
                priority=10,
                max_attempts=1,
                reason="ttl_expired",
            )
            job_ids.append(queued["id"])
        emit_event(
            connection,
            project_id,
            "infra.janitor_run",
            actor,
            "project",
            project_id,
            {"queued_job_ids": job_ids, "resource_ids": resource_ids},
        )
        return {
            "status": "queued" if job_ids else "noop",
            "project_id": project_id,
            "queued_job_ids": job_ids,
            "resource_ids": resource_ids,
        }

    def list_resources(
        self, connection: Connection, project_id: str, limit: int
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, task_id, decision_id, provider, resource_type,
                       resource_name, external_resource_id, region, size, image, tags,
                       status, ttl_minutes, expires_at, terminated_at, termination_reason,
                       provision_receipt_id, provision_artifact_id,
                       termination_receipt_id, termination_artifact_id,
                       data_boundary, metadata, created_by_job_id, updated_by_job_id,
                       created_at, updated_at
                FROM infra_resources
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        return [dict(row) for row in rows]

    def get_resource(
        self, connection: Connection, project_id: str, resource_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, task_id, decision_id, provider, resource_type,
                       resource_name, external_resource_id, region, size, image, tags,
                       status, ttl_minutes, expires_at, terminated_at, termination_reason,
                       provision_receipt_id, provision_artifact_id,
                       termination_receipt_id, termination_artifact_id,
                       data_boundary, metadata, created_by_job_id, updated_by_job_id,
                       created_at, updated_at
                FROM infra_resources
                WHERE project_id = :project_id AND id = :resource_id
                """
            ),
            {"project_id": project_id, "resource_id": resource_id},
        ).mappings().first()
        if not row:
            raise InfraResourceNotFoundError("Infrastructure resource not found.")
        return dict(row)

    def _create_provision_review_decision(
        self,
        connection: Connection,
        *,
        project_id: str,
        task_id: Optional[str],
        reservation_id: str,
        artifact_id: str,
        title: str,
        region: str,
        size: str,
        ttl_minutes: int,
        tags: list[str],
        data_boundary: str,
    ) -> str:
        decision_id = make_id("dec")
        deadline_at = expires_at(24 * 60)
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, task_id, question, recommendation, options, risk_tier,
                    cost_summary, deadline_at, default_action, status
                )
                VALUES (
                    :id, :project_id, :task_id, :question, CAST(:recommendation AS JSONB),
                    CAST(:options AS JSONB), 3, CAST(:cost_summary AS JSONB), :deadline_at,
                    'hold', 'pending'
                )
                """
            ),
            {
                "id": decision_id,
                "project_id": project_id,
                "task_id": task_id,
                "question": f"Approve infrastructure provisioning: {title}",
                "recommendation": json.dumps(
                    {
                        "action": "review_infra_provisioning",
                        "artifact_id": artifact_id,
                        "reservation_id": reservation_id,
                        "title": title,
                        "region": region,
                        "size": size,
                        "ttl_minutes": ttl_minutes,
                        "tags": tags,
                        "data_boundary": data_boundary,
                    }
                ),
                "options": json.dumps(
                    [
                        {"action": "approve", "label": "Provision"},
                        {"action": "reject", "label": "Hold"},
                    ]
                ),
                "cost_summary": json.dumps(
                    {
                        "artifact_id": artifact_id,
                        "reservation_id": reservation_id,
                        "region": region,
                        "size": size,
                        "ttl_minutes": ttl_minutes,
                    }
                ),
                "deadline_at": deadline_at,
            },
        )
        emit_event(
            connection,
            project_id,
            "decision.created",
            "system",
            "decision",
            decision_id,
            {
                "reason": "infra_provision_review_requested",
                "default_action": "hold",
                "deadline_at": deadline_at.isoformat(),
            },
        )
        return decision_id

    def create_provision_gate_block(
        self,
        connection: Connection,
        *,
        project_id: str,
        task_id: Optional[str],
        decision_id: str,
        title: str,
        region: str,
        size: str,
        ttl_minutes: int,
        reservation_id: str,
        data_boundary: str,
        actor: str,
    ) -> str:
        artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            task_id,
            "infra_provision_gate",
            f"Infrastructure gate for {title}",
            {
                "status": "blocked_manual_provision_required",
                "reason": "Automatic infrastructure provisioning is disabled in this phase.",
                "approved_review_decision_id": decision_id,
                "approved_by": actor,
                "region": region,
                "size": size,
                "ttl_minutes": ttl_minutes,
                "reservation_id": reservation_id,
                "next_action": "Enable the infra provisioning policy or provision the resource manually.",
                "data_boundary": data_boundary,
            },
            data_boundary=data_boundary,
        )
        emit_event(
            connection,
            project_id,
            "decision.follow_on_blocked",
            actor,
            "artifact",
            artifact_id,
            {
                "decision_id": decision_id,
                "blocked_action": "infra.droplet.create",
                "reason": "manual_infra_provision_required",
            },
        )
        return artifact_id

    def _reused_create_result(
        self,
        connection: Connection,
        project_id: str,
        receipt_id: str,
        data_boundary: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, resource_name, external_resource_id, expires_at,
                       provision_artifact_id
                FROM infra_resources
                WHERE project_id = :project_id AND provision_receipt_id = :receipt_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"project_id": project_id, "receipt_id": receipt_id},
        ).mappings().first()
        if not row:
            return {"status": "reused", "receipt_id": receipt_id, "data_boundary": data_boundary}
        provider_ref = connection.execute(
            text("SELECT provider_ref FROM receipts WHERE id = :receipt_id"),
            {"receipt_id": receipt_id},
        ).scalar_one_or_none()
        return {
            "status": "reused",
            "resource_id": row["id"],
            "receipt_id": receipt_id,
            "artifact_id": row.get("provision_artifact_id"),
            "provider_ref": provider_ref,
            "provider_resource_id": row.get("external_resource_id"),
            "resource_name": row.get("resource_name"),
            "expires_at": row["expires_at"].isoformat() if row.get("expires_at") else None,
            "data_boundary": data_boundary,
        }

    def _normalize_tags(self, tags: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in tags:
            cleaned = raw.strip().lower()
            if not cleaned:
                continue
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", cleaned):
                raise ValueError(
                    "tags must be lowercase slug tokens containing letters, numbers, or dashes."
                )
            normalized.append(cleaned)
        return list(dict.fromkeys(normalized))

    def _validate_ttl(self, ttl_minutes: int) -> None:
        if ttl_minutes < 5 or ttl_minutes > 10_080:
            raise ValueError("ttl_minutes must be between 5 and 10080.")

    def _resolve_boundary(
        self,
        connection: Connection,
        task_id: Optional[str],
        explicit_boundary: Optional[str],
    ) -> str:
        if explicit_boundary:
            return normalize_data_boundary(explicit_boundary)
        if task_id:
            row = connection.execute(
                text("SELECT data_boundary FROM tasks WHERE id = :task_id"),
                {"task_id": task_id},
            ).mappings().first()
            if row:
                return normalize_data_boundary(row["data_boundary"])
        return "internal"

    def _scope(self, connection: Connection, project_id: str) -> dict[str, str]:
        row = connection.execute(
            text(
                """
                SELECT o.id AS org_id, w.id AS workspace_id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                JOIN orgs o ON o.id = w.org_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().one()
        return {"org_id": row["org_id"], "workspace_id": row["workspace_id"]}

    def _resource_name(self, receipt_id: str) -> str:
        return f"boss-{receipt_id[-10:]}".lower()

    def _record_resource_receipt(
        self,
        connection: Connection,
        *,
        project_id: str,
        receipt_id: str,
        dedupe_key: str,
        provider: str,
        action: str,
        resource_type: str,
        resource_ref: Optional[str],
        status: str,
        error: Optional[dict[str, Any]] = None,
        result: Optional[dict[str, Any]] = None,
        provider_ref: Optional[str] = None,
        created_by_job_id: Optional[str] = None,
    ) -> str:
        resource_receipt_id = make_id("rrcpt")
        payload = {
            "provider": provider,
            "action": action,
            "resource_type": resource_type,
            "resource_ref": resource_ref,
        }
        connection.execute(
            text(
                """
                INSERT INTO resource_receipts (
                    id, project_id, changeset_id, operation_id, provider_account_id,
                    receipt_id, dedupe_key, provider, action, resource_type,
                    resource_ref, status, provider_ref, request_hash, result, error,
                    created_by_job_id
                )
                VALUES (
                    :id, :project_id, NULL, NULL, NULL,
                    :receipt_id, :dedupe_key, :provider, :action, :resource_type,
                    :resource_ref, :status, :provider_ref, :request_hash,
                    CAST(:result AS JSONB), CAST(:error AS JSONB), :created_by_job_id
                )
                """
            ),
            {
                "id": resource_receipt_id,
                "project_id": project_id,
                "receipt_id": receipt_id,
                "dedupe_key": dedupe_key,
                "provider": provider,
                "action": action,
                "resource_type": resource_type,
                "resource_ref": resource_ref,
                "status": status,
                "provider_ref": provider_ref,
                "request_hash": json_hash(payload),
                "result": json.dumps(result),
                "error": json.dumps(error),
                "created_by_job_id": created_by_job_id,
            },
        )
        return resource_receipt_id

    def _assert_decision_ready(
        self, connection: Connection, project_id: str, decision_id: str
    ) -> None:
        row = connection.execute(
            text(
                """
                SELECT status
                FROM decisions
                WHERE id = :decision_id AND project_id = :project_id
                """
            ),
            {"decision_id": decision_id, "project_id": project_id},
        ).mappings().first()
        if not row:
            raise ValueError("decision_id does not reference a project decision.")
        if row["status"] not in {"approved", "executed"}:
            raise ValueError("decision_id must be approved before provisioning.")
