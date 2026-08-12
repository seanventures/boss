"""Elastic worker/infra fabric: capacity requests, instances, provider usage.

Agents, nodes, and projects request capacity against worker pools. The fabric:

- plans scale via existing Budget-Aware Autoscaler / worker_pools
- materializes local/container instances immediately (process-level capacity)
- proposes cloud VM capacity through ProviderOps ChangeSets (receipted, approved)
- tracks every instance lifecycle
- logs provider-by-provider usage (API, compute, tokens, billing sync)

Hard rules: no silent provision; real cloud create uses ChangeSets + receipts;
fail closed when infra real provision is denied; never invent managed fleet capacity.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event, make_id
from .receipts import ReceiptService
from .resource_plane import ResourcePlaneService


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


ACTIVE_INSTANCE_STATUSES = ("requested", "provisioning", "running", "simulated", "draining")


class ElasticFabricService:
    """Project-scoped elastic capacity + per-provider usage ledger."""

    def __init__(
        self,
        settings: Settings,
        resource_plane: Optional[ResourcePlaneService] = None,
    ):
        self.settings = settings
        self.resource_plane = resource_plane or ResourcePlaneService(settings)
        self.receipts = ReceiptService()

    # ------------------------------------------------------------------
    # Capacity + pools
    # ------------------------------------------------------------------

    def request_capacity(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        desired_workers: int,
        pool_key: Optional[str] = None,
        worker_pool_id: Optional[str] = None,
        kind: Optional[str] = None,
        provider_account_id: Optional[str] = None,
        capabilities: Optional[list[str]] = None,
        auto_reconcile: bool = True,
        cloud_spillover_enabled: bool = False,
        reason: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Request elastic capacity for a project pool (agents / nodes / jobs)."""
        if desired_workers < 0:
            raise ValueError("desired_workers must be non-negative.")
        pool = self._resolve_pool(
            connection,
            project_id=project_id,
            actor=actor,
            pool_key=pool_key,
            worker_pool_id=worker_pool_id,
            kind=kind,
            provider_account_id=provider_account_id,
            capabilities=capabilities,
        )
        if desired_workers > int(pool["max_workers"]):
            raise ValueError(
                f"desired_workers {desired_workers} exceeds pool max_workers "
                f"{pool['max_workers']}."
            )

        before = int(pool["desired_workers"])
        self.resource_plane._set_worker_pool_desired(
            connection, project_id, pool["id"], desired_workers, actor
        )
        pool = self.resource_plane._get_worker_pool(connection, project_id, pool["id"])

        scope = self._project_scope(connection, project_id)

        receipt, _ = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            f"elastic-capacity:{project_id}:{pool['id']}:{desired_workers}:{before}",
            "elastic.capacity_request",
            {
                "worker_pool_id": pool["id"],
                "desired_workers": desired_workers,
                "previous_desired": before,
            },
            {
                "operation": "request_capacity",
                "reason": reason,
                "actor": actor,
                "data_boundary": "internal",
            },
        )
        self.receipts.mark_succeeded(
            connection,
            receipt["id"],
            f"pool:{pool['pool_key']}:desired:{desired_workers}",
            {"desired_workers": desired_workers, "previous_desired": before},
        )

        emit_event(
            connection,
            project_id,
            "elastic.capacity_requested",
            actor,
            "worker_pool",
            pool["id"],
            {
                "desired_workers": desired_workers,
                "previous_desired": before,
                "pool_key": pool["pool_key"],
                "kind": pool["kind"],
                "reason": reason,
                "receipt_id": receipt["id"],
            },
        )

        # Explicit capacity requests must NOT run the queue-driven autoscaler first —
        # it would drain desired_workers when the job queue is idle. Reconcile against
        # the requested desired count; operators can still POST /v1/autoscaler/plans
        # separately for queue pressure scaling.
        reconcile = None
        if auto_reconcile:
            reconcile = self.reconcile_pool(
                connection,
                project_id=project_id,
                actor=actor,
                worker_pool_id=pool["id"],
                cloud_spillover_enabled=cloud_spillover_enabled
                or pool["kind"] in {"cloud", "vm"},
                reason=reason or "capacity_request",
                metadata=metadata,
            )
            pool = (reconcile or {}).get("worker_pool") or pool

        return {
            "project_id": project_id,
            "worker_pool": pool,
            "desired_workers": desired_workers,
            "previous_desired": before,
            "receipt_id": receipt["id"],
            "scale_plan": None,
            "reconcile": reconcile,
        }

    def reconcile_pool(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        worker_pool_id: Optional[str] = None,
        pool_key: Optional[str] = None,
        cloud_spillover_enabled: bool = False,
        reason: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Bring active instances in line with pool.desired_workers."""
        pool = self._resolve_pool(
            connection,
            project_id=project_id,
            actor=actor,
            worker_pool_id=worker_pool_id,
            pool_key=pool_key,
        )
        desired = int(pool["desired_workers"])
        active = self.list_instances(
            connection,
            project_id,
            worker_pool_id=pool["id"],
            active_only=True,
        )
        current = len(active)
        created: list[dict[str, Any]] = []
        drained: list[dict[str, Any]] = []
        proposed_changeset_id = None
        actions: list[dict[str, Any]] = []

        if current < desired:
            gap = desired - current
            pool_meta = pool.get("metadata") or {}
            if not isinstance(pool_meta, dict):
                pool_meta = {}
            # worker_pools.kind is local|cloud; elastic_kind may refine (container|vm).
            kind = str(pool_meta.get("elastic_kind") or pool["kind"]).lower()
            if kind in {"local", "container"}:
                instance_kind = "container" if kind == "container" else "local"
                for i in range(gap):
                    inst = self._create_instance(
                        connection,
                        project_id=project_id,
                        pool=pool,
                        actor=actor,
                        kind=instance_kind,
                        status="running",
                        metadata={
                            **(metadata or {}),
                            "reconcile_reason": reason,
                            "synthetic": True,
                            "slot": current + i + 1,
                        },
                    )
                    created.append(inst)
                    self.record_usage(
                        connection,
                        project_id=project_id,
                        provider=pool.get("provider") or "local",
                        provider_account_id=pool.get("provider_account_id"),
                        category="infra",
                        operation="elastic.instance.start",
                        quantity=1,
                        unit="instance",
                        worker_instance_id=inst["id"],
                        resource_ref=inst["instance_key"],
                        metadata={"kind": inst["kind"], "pool_key": pool["pool_key"]},
                        actor=actor,
                    )
                actions.append(
                    {"action": "start_local_or_container", "count": gap}
                )
            elif kind in {"cloud", "vm"}:
                kind = "vm"  # normalize for instance records
                if not cloud_spillover_enabled:
                    actions.append(
                        {
                            "action": "await_cloud_spillover_flag",
                            "gap": gap,
                            "message": (
                                "Cloud/VM scale requires cloud_spillover_enabled "
                                "or an approved autoscaler ChangeSet."
                            ),
                        }
                    )
                elif not pool.get("provider_account_id"):
                    raise ValueError("cloud/vm pools require provider_account_id.")
                else:
                    # Propose N create ops via ChangeSet (ProviderOps path).
                    ops = []
                    for i in range(gap):
                        ops.append(
                            self._vm_create_operation(pool, index=current + i + 1)
                        )
                    if not self.settings.infra_real_provision_allowed:
                        # Simulated instances — durable, no live provider write.
                        for i in range(gap):
                            inst = self._create_instance(
                                connection,
                                project_id=project_id,
                                pool=pool,
                                actor=actor,
                                kind="vm",
                                status="simulated",
                                metadata={
                                    **(metadata or {}),
                                    "simulated": True,
                                    "reason": "infra_real_provision_not_allowed",
                                    "slot": current + i + 1,
                                },
                            )
                            created.append(inst)
                            self.record_usage(
                                connection,
                                project_id=project_id,
                                provider=pool.get("provider") or "unknown",
                                provider_account_id=pool.get("provider_account_id"),
                                category="infra",
                                operation="elastic.instance.simulate",
                                quantity=1,
                                unit="instance",
                                worker_instance_id=inst["id"],
                                resource_ref=inst["instance_key"],
                                metadata={"simulated": True},
                                actor=actor,
                            )
                        actions.append(
                            {
                                "action": "simulate_vm_instances",
                                "count": gap,
                                "reason": "BOSS_INFRA_REAL_PROVISION_ALLOWED is false",
                            }
                        )
                    else:
                        reservation = self.resource_plane.budget.reserve(
                            connection,
                            project_id,
                            "elastic_fabric",
                            max(int(pool["cost_per_worker_hour_usd_micros"]) * gap, 1),
                            f"Elastic fabric scale-up of {gap} VM workers.",
                            60,
                        )
                        if reservation["status"] != "reserved":
                            actions.append(
                                {
                                    "action": "budget_blocked",
                                    "reservation": reservation,
                                }
                            )
                        else:
                            meta = pool.get("metadata") or {}
                            if not isinstance(meta, dict):
                                meta = {}
                            changeset = self.resource_plane.create_changeset(
                                connection,
                                project_id=project_id,
                                provider_account_id=pool["provider_account_id"],
                                title=f"Elastic fabric scale {pool['pool_key']} +{gap}",
                                summary=(
                                    f"Provision {gap} worker VM(s) for pool "
                                    f"{pool['pool_key']} via elastic fabric."
                                ),
                                expected_cost_usd_micros=int(
                                    pool["cost_per_worker_hour_usd_micros"]
                                )
                                * gap,
                                reservation_id=reservation["reservation_id"],
                                rollback_plan=[
                                    "Drain worker instances.",
                                    "Destroy provider resources via approved ChangeSet.",
                                ],
                                risk_tier=3,
                                approval_required=True,
                                actor=actor,
                                operations=ops,
                            )
                            proposed_changeset_id = changeset["id"]
                            for i in range(gap):
                                inst = self._create_instance(
                                    connection,
                                    project_id=project_id,
                                    pool=pool,
                                    actor=actor,
                                    kind="vm",
                                    status="provisioning",
                                    changeset_id=proposed_changeset_id,
                                    metadata={
                                        **(metadata or {}),
                                        "slot": current + i + 1,
                                        "awaiting_changeset": True,
                                    },
                                )
                                created.append(inst)
                            actions.append(
                                {
                                    "action": "propose_vm_changeset",
                                    "count": gap,
                                    "changeset_id": proposed_changeset_id,
                                }
                            )
            else:
                raise ValueError(f"Unsupported pool kind for reconcile: {kind}")

        elif current > desired:
            # Drain newest first
            extras = sorted(
                active,
                key=lambda row: row.get("created_at") or "",
                reverse=True,
            )[: current - desired]
            for inst in extras:
                drained.append(
                    self.terminate_instance(
                        connection,
                        project_id=project_id,
                        instance_id=inst["id"],
                        actor=actor,
                        reason=reason or "reconcile_scale_down",
                        destroy_provider_resource=False,
                    )
                )
            actions.append({"action": "drain_instances", "count": len(extras)})

        # Refresh pool current_workers from active instances
        active_after = self.list_instances(
            connection, project_id, worker_pool_id=pool["id"], active_only=True
        )
        self._set_pool_current(
            connection, project_id, pool["id"], len(active_after), actor
        )
        pool = self.resource_plane._get_worker_pool(connection, project_id, pool["id"])

        emit_event(
            connection,
            project_id,
            "elastic.pool_reconciled",
            actor,
            "worker_pool",
            pool["id"],
            {
                "desired": desired,
                "active_before": current,
                "active_after": len(active_after),
                "created": len(created),
                "drained": len(drained),
                "changeset_id": proposed_changeset_id,
                "reason": reason,
            },
        )
        return {
            "project_id": project_id,
            "worker_pool": pool,
            "desired_workers": desired,
            "active_instances": len(active_after),
            "created": created,
            "drained": drained,
            "actions": actions,
            "changeset_id": proposed_changeset_id,
        }

    # ------------------------------------------------------------------
    # Instances
    # ------------------------------------------------------------------

    def list_instances(
        self,
        connection: Connection,
        project_id: str,
        *,
        worker_pool_id: Optional[str] = None,
        status: Optional[str] = None,
        active_only: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["project_id = :project_id"]
        params: dict[str, Any] = {"project_id": project_id, "limit": limit}
        if worker_pool_id:
            clauses.append("worker_pool_id = :worker_pool_id")
            params["worker_pool_id"] = worker_pool_id
        if status:
            clauses.append("status = :status")
            params["status"] = status
        if active_only:
            # Expand statuses inline (portable across drivers).
            placeholders = []
            for idx, st in enumerate(ACTIVE_INSTANCE_STATUSES):
                key = f"ast_{idx}"
                placeholders.append(f":{key}")
                params[key] = st
            clauses.append(f"status IN ({', '.join(placeholders)})")
        rows = connection.execute(
            text(
                f"""
                SELECT id, project_id, worker_pool_id, instance_key, kind, provider,
                       provider_account_id, external_id, status, endpoint, region,
                       size, capability_tags, scale_plan_id, changeset_id,
                       resource_receipt_id, cost_event_id, last_heartbeat_at,
                       provisioned_at, terminated_at, created_by, metadata,
                       created_at, updated_at
                FROM worker_instances
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
        return [self._serialize_instance(row) for row in rows]

    def heartbeat_instance(
        self,
        connection: Connection,
        *,
        project_id: str,
        instance_id: str,
        actor: str = "worker",
        status: Optional[str] = None,
        endpoint: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        row = self._get_instance(connection, project_id, instance_id)
        new_status = status or (
            "running" if row["status"] in {"requested", "provisioning", "simulated"} else row["status"]
        )
        meta = dict(row.get("metadata") or {})
        if metadata:
            meta.update(metadata)
        connection.execute(
            text(
                """
                UPDATE worker_instances
                SET status = :status,
                    endpoint = COALESCE(:endpoint, endpoint),
                    last_heartbeat_at = now(),
                    provisioned_at = COALESCE(provisioned_at, now()),
                    metadata = CAST(:metadata AS JSONB),
                    updated_at = now()
                WHERE id = :id AND project_id = :project_id
                """
            ),
            {
                "id": instance_id,
                "project_id": project_id,
                "status": new_status,
                "endpoint": endpoint,
                "metadata": json.dumps(meta),
            },
        )
        self.record_usage(
            connection,
            project_id=project_id,
            provider=row.get("provider") or "local",
            provider_account_id=row.get("provider_account_id"),
            category="infra",
            operation="elastic.instance.heartbeat",
            quantity=1,
            unit="heartbeat",
            worker_instance_id=instance_id,
            resource_ref=row["instance_key"],
            actor=actor,
        )
        return self._get_instance(connection, project_id, instance_id)

    def terminate_instance(
        self,
        connection: Connection,
        *,
        project_id: str,
        instance_id: str,
        actor: str,
        reason: Optional[str] = None,
        destroy_provider_resource: bool = False,
    ) -> dict[str, Any]:
        row = self._get_instance(connection, project_id, instance_id)
        meta = dict(row.get("metadata") or {})
        meta["terminate_reason"] = reason
        connection.execute(
            text(
                """
                UPDATE worker_instances
                SET status = 'terminated',
                    terminated_at = now(),
                    metadata = CAST(:metadata AS JSONB),
                    updated_at = now()
                WHERE id = :id AND project_id = :project_id
                """
            ),
            {
                "id": instance_id,
                "project_id": project_id,
                "metadata": json.dumps(meta),
            },
        )
        changeset_id = None
        if (
            destroy_provider_resource
            and row.get("external_id")
            and row.get("provider") == "digitalocean"
            and row.get("provider_account_id")
            and self.settings.infra_real_provision_allowed
        ):
            changeset = self.resource_plane.create_changeset(
                connection,
                project_id=project_id,
                provider_account_id=row["provider_account_id"],
                title=f"Destroy elastic worker {row['instance_key']}",
                summary=f"Terminate provider resource {row['external_id']}.",
                expected_cost_usd_micros=0,
                reservation_id=None,
                rollback_plan=["N/A — destroy operation"],
                risk_tier=3,
                approval_required=True,
                actor=actor,
                operations=[
                    {
                        "skill_id": "digitalocean.droplet.destroy",
                        "action": "destroy",
                        "resource_type": "droplet",
                        "risk_tier": 3,
                        "destructive": True,
                        "production_affecting": False,
                        "desired_state": {
                            "droplet_id": row["external_id"],
                            "name": row["instance_key"],
                        },
                    }
                ],
            )
            changeset_id = changeset["id"]

        self.record_usage(
            connection,
            project_id=project_id,
            provider=row.get("provider") or "local",
            provider_account_id=row.get("provider_account_id"),
            category="infra",
            operation="elastic.instance.terminate",
            quantity=1,
            unit="instance",
            worker_instance_id=instance_id,
            resource_ref=row["instance_key"],
            actor=actor,
            metadata={"reason": reason, "changeset_id": changeset_id},
        )
        emit_event(
            connection,
            project_id,
            "elastic.instance_terminated",
            actor,
            "worker_instance",
            instance_id,
            {"reason": reason, "changeset_id": changeset_id},
        )
        out = self._get_instance(connection, project_id, instance_id)
        out["destroy_changeset_id"] = changeset_id
        return out

    def register_provider_instance(
        self,
        connection: Connection,
        *,
        project_id: str,
        worker_pool_id: str,
        actor: str,
        external_id: str,
        provider: str,
        provider_account_id: Optional[str] = None,
        kind: str = "vm",
        region: Optional[str] = None,
        size: Optional[str] = None,
        endpoint: Optional[str] = None,
        changeset_id: Optional[str] = None,
        receipt_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Register a live provider resource as a worker instance (post-ChangeSet apply)."""
        pool = self.resource_plane._get_worker_pool(
            connection, project_id, worker_pool_id
        )
        inst = self._create_instance(
            connection,
            project_id=project_id,
            pool=pool,
            actor=actor,
            kind=kind,
            status="running",
            external_id=external_id,
            provider=provider,
            provider_account_id=provider_account_id or pool.get("provider_account_id"),
            region=region,
            size=size,
            endpoint=endpoint,
            changeset_id=changeset_id,
            resource_receipt_id=receipt_id,
            metadata=metadata,
        )
        self.record_usage(
            connection,
            project_id=project_id,
            provider=provider,
            provider_account_id=provider_account_id or pool.get("provider_account_id"),
            category="infra",
            operation=f"{provider}.instance.registered",
            quantity=1,
            unit="instance",
            cost_usd_micros=int(pool.get("cost_per_worker_hour_usd_micros") or 0),
            worker_instance_id=inst["id"],
            resource_ref=external_id,
            receipt_id=receipt_id,
            actor=actor,
        )
        active = self.list_instances(
            connection, project_id, worker_pool_id=worker_pool_id, active_only=True
        )
        self._set_pool_current(
            connection, project_id, worker_pool_id, len(active), actor
        )
        return inst

    # ------------------------------------------------------------------
    # Provider usage ledger
    # ------------------------------------------------------------------

    def record_usage(
        self,
        connection: Connection,
        *,
        project_id: str,
        provider: str,
        operation: str,
        category: str = "api_call",
        quantity: float = 1.0,
        unit: str = "count",
        provider_account_id: Optional[str] = None,
        cost_usd_micros: Optional[int] = None,
        resource_ref: Optional[str] = None,
        worker_instance_id: Optional[str] = None,
        receipt_id: Optional[str] = None,
        source: str = "control_plane",
        metadata: Optional[dict[str, Any]] = None,
        actor: str = "system",
        occurred_at: Optional[datetime] = None,
    ) -> dict[str, Any]:
        event_id = make_id("pue")
        when = occurred_at or _utcnow()
        connection.execute(
            text(
                """
                INSERT INTO provider_usage_events (
                    id, project_id, provider, provider_account_id, category,
                    operation, quantity, unit, cost_usd_micros, resource_ref,
                    worker_instance_id, receipt_id, source, metadata, occurred_at
                )
                VALUES (
                    :id, :project_id, :provider, :provider_account_id, :category,
                    :operation, :quantity, :unit, :cost_usd_micros, :resource_ref,
                    :worker_instance_id, :receipt_id, :source,
                    CAST(:metadata AS JSONB), :occurred_at
                )
                """
            ),
            {
                "id": event_id,
                "project_id": project_id,
                "provider": (provider or "unknown").strip().lower(),
                "provider_account_id": provider_account_id,
                "category": (category or "other").strip().lower(),
                "operation": (operation or "unknown").strip(),
                "quantity": float(quantity),
                "unit": (unit or "count").strip(),
                "cost_usd_micros": cost_usd_micros,
                "resource_ref": resource_ref,
                "worker_instance_id": worker_instance_id,
                "receipt_id": receipt_id,
                "source": source,
                "metadata": json.dumps({**(metadata or {}), "actor": actor}),
                "occurred_at": when,
            },
        )
        return self._get_usage_event(connection, project_id, event_id)

    def list_usage(
        self,
        connection: Connection,
        project_id: str,
        *,
        provider: Optional[str] = None,
        provider_account_id: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["project_id = :project_id"]
        params: dict[str, Any] = {"project_id": project_id, "limit": limit}
        if provider:
            clauses.append("provider = :provider")
            params["provider"] = provider.lower()
        if provider_account_id:
            clauses.append("provider_account_id = :provider_account_id")
            params["provider_account_id"] = provider_account_id
        if category:
            clauses.append("category = :category")
            params["category"] = category.lower()
        rows = connection.execute(
            text(
                f"""
                SELECT id, project_id, provider, provider_account_id, category,
                       operation, quantity, unit, cost_usd_micros, resource_ref,
                       worker_instance_id, receipt_id, source, metadata,
                       occurred_at, created_at
                FROM provider_usage_events
                WHERE {' AND '.join(clauses)}
                ORDER BY occurred_at DESC
                LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
        return [self._serialize_usage(row) for row in rows]

    def usage_summary(
        self,
        connection: Connection,
        project_id: str,
        *,
        provider: Optional[str] = None,
    ) -> dict[str, Any]:
        """Aggregate usage by provider and category (monitor view)."""
        clauses = ["project_id = :project_id"]
        params: dict[str, Any] = {"project_id": project_id}
        if provider:
            clauses.append("provider = :provider")
            params["provider"] = provider.lower()
        rows = connection.execute(
            text(
                f"""
                SELECT provider, category, unit,
                       COUNT(*)::int AS event_count,
                       COALESCE(SUM(quantity), 0) AS quantity_sum,
                       COALESCE(SUM(cost_usd_micros), 0)::bigint AS cost_usd_micros_sum
                FROM provider_usage_events
                WHERE {' AND '.join(clauses)}
                GROUP BY provider, category, unit
                ORDER BY provider ASC, category ASC
                """
            ),
            params,
        ).mappings().all()
        by_provider: dict[str, Any] = {}
        for row in rows:
            prov = row["provider"]
            bucket = by_provider.setdefault(
                prov,
                {
                    "provider": prov,
                    "categories": [],
                    "event_count": 0,
                    "cost_usd_micros": 0,
                },
            )
            bucket["categories"].append(
                {
                    "category": row["category"],
                    "unit": row["unit"],
                    "event_count": row["event_count"],
                    "quantity_sum": float(row["quantity_sum"] or 0),
                    "cost_usd_micros": int(row["cost_usd_micros_sum"] or 0),
                }
            )
            bucket["event_count"] += int(row["event_count"] or 0)
            bucket["cost_usd_micros"] += int(row["cost_usd_micros_sum"] or 0)

        # Also surface cost_events (billing sync) per provider for FinOps join.
        cost_rows = connection.execute(
            text(
                """
                SELECT provider,
                       COUNT(*)::int AS cost_event_count,
                       COALESCE(SUM(amount_usd_micros), 0)::bigint AS amount_usd_micros
                FROM cost_events
                WHERE project_id = :project_id
                GROUP BY provider
                ORDER BY provider ASC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        billing = {
            row["provider"]: {
                "cost_event_count": row["cost_event_count"],
                "amount_usd_micros": int(row["amount_usd_micros"] or 0),
            }
            for row in cost_rows
        }
        for prov, bill in billing.items():
            by_provider.setdefault(
                prov,
                {
                    "provider": prov,
                    "categories": [],
                    "event_count": 0,
                    "cost_usd_micros": 0,
                },
            )["billing"] = bill

        instances = connection.execute(
            text(
                """
                SELECT provider, status, COUNT(*)::int AS n
                FROM worker_instances
                WHERE project_id = :project_id
                GROUP BY provider, status
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        instance_rollups: dict[str, Any] = {}
        for row in instances:
            key = row["provider"] or "local"
            inst = instance_rollups.setdefault(key, {"provider": key, "by_status": {}})
            inst["by_status"][row["status"]] = row["n"]

        return {
            "project_id": project_id,
            "providers": list(by_provider.values()),
            "instances": list(instance_rollups.values()),
            "generated_at": _utcnow().isoformat(),
        }

    def sync_provider_usage(
        self,
        connection: Connection,
        *,
        project_id: str,
        provider_account_id: str,
        actor: str,
    ) -> dict[str, Any]:
        """Pull billing/usage signals for one provider account into the ledger.

        Uses existing inventory+billing sync (ProviderOps) then mirrors recent
        cost_events into provider_usage_events for a unified monitor view.
        """
        account = self.resource_plane.get_provider_account(
            connection, project_id, provider_account_id
        )
        # Inventory + optional billing via existing path
        sync_result = self.resource_plane.sync_resources(
            connection,
            project_id=project_id,
            provider_account_id=provider_account_id,
            actor=actor,
        )
        # Mirror recent cost events as usage rows (deduped by source_ref)
        costs = connection.execute(
            text(
                """
                SELECT id, provider, category, amount_usd_micros, source_ref,
                       metadata, created_at, period_start, period_end
                FROM cost_events
                WHERE project_id = :project_id
                  AND provider_account_id = :provider_account_id
                ORDER BY created_at DESC
                LIMIT 50
                """
            ),
            {
                "project_id": project_id,
                "provider_account_id": provider_account_id,
            },
        ).mappings().all()
        mirrored = 0
        for cost in costs:
            source_ref = cost.get("source_ref") or cost["id"]
            exists = connection.execute(
                text(
                    """
                    SELECT 1 FROM provider_usage_events
                    WHERE project_id = :project_id
                      AND resource_ref = :resource_ref
                      AND source = 'billing_sync'
                    LIMIT 1
                    """
                ),
                {"project_id": project_id, "resource_ref": str(source_ref)},
            ).first()
            if exists:
                continue
            self.record_usage(
                connection,
                project_id=project_id,
                provider=cost["provider"],
                provider_account_id=provider_account_id,
                category=str(cost.get("category") or "infra"),
                operation="provider.billing.sync",
                quantity=1,
                unit="cost_event",
                cost_usd_micros=int(cost.get("amount_usd_micros") or 0),
                resource_ref=str(source_ref),
                source="billing_sync",
                metadata={"cost_event_id": cost["id"]},
                actor=actor,
                occurred_at=cost.get("created_at"),
            )
            mirrored += 1

        # Always log the sync API call itself
        self.record_usage(
            connection,
            project_id=project_id,
            provider=account["provider"],
            provider_account_id=provider_account_id,
            category="api_call",
            operation="provider.resources.sync",
            quantity=1,
            unit="call",
            source="control_plane",
            actor=actor,
            metadata={"mirrored_cost_events": mirrored},
        )
        emit_event(
            connection,
            project_id,
            "elastic.provider_usage_synced",
            actor,
            "provider_account",
            provider_account_id,
            {"provider": account["provider"], "mirrored": mirrored},
        )
        return {
            "project_id": project_id,
            "provider_account_id": provider_account_id,
            "provider": account["provider"],
            "inventory_sync": sync_result,
            "mirrored_cost_events": mirrored,
            "summary": self.usage_summary(
                connection, project_id, provider=account["provider"]
            ),
        }

    def fabric_status(
        self, connection: Connection, project_id: str
    ) -> dict[str, Any]:
        pools = self.resource_plane.list_worker_pools(connection, project_id)
        instances = self.list_instances(connection, project_id, limit=500)
        active = [i for i in instances if i["status"] in ACTIVE_INSTANCE_STATUSES]
        usage = self.usage_summary(connection, project_id)
        return {
            "project_id": project_id,
            "pools": pools,
            "instance_counts": {
                "total": len(instances),
                "active": len(active),
                "by_status": self._count_by(instances, "status"),
                "by_kind": self._count_by(instances, "kind"),
                "by_provider": self._count_by(instances, "provider"),
            },
            "usage": usage,
            "infra_real_provision_allowed": bool(
                self.settings.infra_real_provision_allowed
            ),
            "message": (
                "Elastic fabric uses ProviderOps connections per project. "
                "Local/container capacity starts immediately; cloud VMs go through "
                "budgeted ChangeSets (or simulated instances when real provision is off)."
            ),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_pool(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        pool_key: Optional[str] = None,
        worker_pool_id: Optional[str] = None,
        kind: Optional[str] = None,
        provider_account_id: Optional[str] = None,
        capabilities: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        if worker_pool_id:
            return self.resource_plane._get_worker_pool(
                connection, project_id, worker_pool_id
            )
        if pool_key:
            try:
                return self.resource_plane._get_worker_pool_by_key(
                    connection, project_id, pool_key
                )
            except Exception:
                # No pool with this key yet — create below.
                pass
            normalized_kind = (kind or "local").lower()
            if normalized_kind not in {"local", "cloud", "container", "vm"}:
                raise ValueError("kind must be local, container, cloud, or vm.")
            # Map container→local for worker_pools.kind constraint (local|cloud only)
            pool_kind = "cloud" if normalized_kind in {"cloud", "vm"} else "local"
            meta = {"elastic_kind": normalized_kind}
            return self.resource_plane.upsert_worker_pool(
                connection,
                project_id=project_id,
                pool_key=pool_key,
                kind=pool_kind,
                actor=actor,
                provider_account_id=provider_account_id,
                desired_workers=0,
                min_workers=0,
                max_workers=32 if pool_kind == "cloud" else 16,
                capabilities=capabilities,
                metadata=meta,
            )
        return self.resource_plane._ensure_default_worker_pool(
            connection, project_id, actor
        )

    def _create_instance(
        self,
        connection: Connection,
        *,
        project_id: str,
        pool: dict[str, Any],
        actor: str,
        kind: str,
        status: str,
        external_id: Optional[str] = None,
        provider: Optional[str] = None,
        provider_account_id: Optional[str] = None,
        region: Optional[str] = None,
        size: Optional[str] = None,
        endpoint: Optional[str] = None,
        changeset_id: Optional[str] = None,
        resource_receipt_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        instance_id = make_id("winst")
        instance_key = f"{pool['pool_key']}-{instance_id[-8:]}"
        meta = dict(metadata or {})
        pool_meta = pool.get("metadata") or {}
        if not isinstance(pool_meta, dict):
            pool_meta = {}
        region = region or pool_meta.get("region")
        size = size or pool_meta.get("size")
        connection.execute(
            text(
                """
                INSERT INTO worker_instances (
                    id, project_id, worker_pool_id, instance_key, kind, provider,
                    provider_account_id, external_id, status, endpoint, region, size,
                    capability_tags, changeset_id, resource_receipt_id,
                    provisioned_at, created_by, metadata
                )
                VALUES (
                    :id, :project_id, :worker_pool_id, :instance_key, :kind, :provider,
                    :provider_account_id, :external_id, :status, :endpoint, :region,
                    :size, CAST(:capability_tags AS JSONB), :changeset_id,
                    :resource_receipt_id,
                    CASE WHEN :status IN ('running', 'simulated') THEN now() ELSE NULL END,
                    :actor, CAST(:metadata AS JSONB)
                )
                """
            ),
            {
                "id": instance_id,
                "project_id": project_id,
                "worker_pool_id": pool["id"],
                "instance_key": instance_key,
                "kind": kind,
                "provider": provider or pool.get("provider"),
                "provider_account_id": provider_account_id
                or pool.get("provider_account_id"),
                "external_id": external_id,
                "status": status,
                "endpoint": endpoint,
                "region": region,
                "size": size,
                "capability_tags": json.dumps(pool.get("capabilities") or []),
                "changeset_id": changeset_id,
                "resource_receipt_id": resource_receipt_id,
                "actor": actor,
                "metadata": json.dumps(meta),
            },
        )
        emit_event(
            connection,
            project_id,
            "elastic.instance_created",
            actor,
            "worker_instance",
            instance_id,
            {
                "instance_key": instance_key,
                "kind": kind,
                "status": status,
                "worker_pool_id": pool["id"],
                "external_id": external_id,
            },
        )
        return self._get_instance(connection, project_id, instance_id)

    def _vm_create_operation(self, pool: dict[str, Any], *, index: int) -> dict[str, Any]:
        provider = pool.get("provider")
        meta = pool.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        if provider == "digitalocean":
            return {
                "skill_id": "digitalocean.droplet.create",
                "action": "create",
                "resource_type": "droplet",
                "risk_tier": 3,
                "destructive": False,
                "production_affecting": False,
                "desired_state": {
                    "worker_pool_id": pool["id"],
                    "pool_key": pool["pool_key"],
                    "name": f"boss-worker-{pool['pool_key']}-{index}",
                    "region": meta.get("region") or "nyc3",
                    "size": meta.get("size") or "s-1vcpu-1gb",
                    "tags": ["boss", "elastic-worker", f"pool:{pool['pool_key']}"],
                    "elastic_fabric": True,
                },
            }
        raise ValueError(
            f"Provider {provider!r} does not support elastic VM creation yet."
        )

    def _set_pool_current(
        self,
        connection: Connection,
        project_id: str,
        pool_id: str,
        current: int,
        actor: str,
    ) -> None:
        connection.execute(
            text(
                """
                UPDATE worker_pools
                SET current_workers = :current,
                    updated_at = now()
                WHERE id = :id AND project_id = :project_id
                """
            ),
            {"id": pool_id, "project_id": project_id, "current": current},
        )

    def _project_scope(
        self, connection: Connection, project_id: str
    ) -> dict[str, str]:
        row = connection.execute(
            text(
                """
                SELECT p.workspace_id, w.org_id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if not row:
            return {
                "org_id": self.settings.default_org_id,
                "workspace_id": self.settings.default_workspace_id,
            }
        return {"org_id": row["org_id"], "workspace_id": row["workspace_id"]}

    def _get_instance(
        self, connection: Connection, project_id: str, instance_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, worker_pool_id, instance_key, kind, provider,
                       provider_account_id, external_id, status, endpoint, region,
                       size, capability_tags, scale_plan_id, changeset_id,
                       resource_receipt_id, cost_event_id, last_heartbeat_at,
                       provisioned_at, terminated_at, created_by, metadata,
                       created_at, updated_at
                FROM worker_instances
                WHERE project_id = :project_id AND id = :id
                """
            ),
            {"project_id": project_id, "id": instance_id},
        ).mappings().first()
        if not row:
            raise ValueError(f"Worker instance '{instance_id}' not found.")
        return self._serialize_instance(row)

    def _get_usage_event(
        self, connection: Connection, project_id: str, event_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, provider, provider_account_id, category,
                       operation, quantity, unit, cost_usd_micros, resource_ref,
                       worker_instance_id, receipt_id, source, metadata,
                       occurred_at, created_at
                FROM provider_usage_events
                WHERE project_id = :project_id AND id = :id
                """
            ),
            {"project_id": project_id, "id": event_id},
        ).mappings().first()
        if not row:
            raise ValueError(f"Usage event '{event_id}' not found.")
        return self._serialize_usage(row)

    @staticmethod
    def _serialize_instance(row: Any) -> dict[str, Any]:
        data = dict(row)
        for key in (
            "created_at",
            "updated_at",
            "last_heartbeat_at",
            "provisioned_at",
            "terminated_at",
        ):
            if data.get(key) is not None and hasattr(data[key], "isoformat"):
                data[key] = data[key].isoformat()
        if not isinstance(data.get("capability_tags"), list):
            data["capability_tags"] = data.get("capability_tags") or []
        if not isinstance(data.get("metadata"), dict):
            data["metadata"] = data.get("metadata") or {}
        return data

    @staticmethod
    def _serialize_usage(row: Any) -> dict[str, Any]:
        data = dict(row)
        for key in ("occurred_at", "created_at"):
            if data.get(key) is not None and hasattr(data[key], "isoformat"):
                data[key] = data[key].isoformat()
        data["quantity"] = float(data.get("quantity") or 0)
        if data.get("cost_usd_micros") is not None:
            data["cost_usd_micros"] = int(data["cost_usd_micros"])
        if not isinstance(data.get("metadata"), dict):
            data["metadata"] = data.get("metadata") or {}
        return data

    @staticmethod
    def _count_by(items: list[dict[str, Any]], key: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in items:
            k = str(item.get(key) or "unknown")
            out[k] = out.get(k, 0) + 1
        return out
