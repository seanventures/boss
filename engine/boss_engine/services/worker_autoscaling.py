"""Distributed job/worker autoscaling control plane on elastic fabric.

Deterministic policy:
  queue_depth → needed_workers = ceil(queue_depth / max_queue_depth_per_worker)
  desired = clamp(needed, min_workers, max_workers)
  scale-up held when queue_depth < scale_up_threshold
  scale actions held during cooldown_seconds after last apply

Apply path requests capacity via ElasticFabricService (respects
BOSS_INFRA_REAL_PROVISION_ALLOWED: local/container immediate; cloud/VM
simulated when real provision is off). Every apply emits a Receipt and
history row. Never silently provisions.
"""

from __future__ import annotations

import json
import math
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event, make_id
from .elastic_fabric import ElasticFabricService
from .receipts import ReceiptService
from .resource_plane import ResourcePlaneService


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


# Recovery DDL without FKs — live schema apply can abort before these tables exist.
_AUTOSCALING_POLICIES_DDL = """
CREATE TABLE IF NOT EXISTS worker_autoscaling_policies (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  pool_key TEXT NOT NULL DEFAULT 'default',
  enabled BOOLEAN NOT NULL DEFAULT true,
  min_workers INTEGER NOT NULL DEFAULT 0,
  max_workers INTEGER NOT NULL DEFAULT 8,
  scale_up_threshold INTEGER NOT NULL DEFAULT 1,
  cooldown_seconds INTEGER NOT NULL DEFAULT 30,
  max_queue_depth_per_worker INTEGER NOT NULL DEFAULT 5,
  kind TEXT NOT NULL DEFAULT 'local',
  provider_account_id TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  last_applied_at TIMESTAMPTZ,
  last_scale_direction TEXT,
  created_by TEXT NOT NULL DEFAULT 'system',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, pool_key)
)
"""
_AUTOSCALING_POLICIES_INDEX = """
CREATE INDEX IF NOT EXISTS idx_worker_autoscaling_policies_project
  ON worker_autoscaling_policies(project_id, enabled, pool_key)
"""
_AUTOSCALING_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS worker_autoscaling_history (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  pool_key TEXT NOT NULL,
  action TEXT NOT NULL,
  status TEXT NOT NULL,
  queue_depth INTEGER NOT NULL DEFAULT 0,
  leased_jobs INTEGER NOT NULL DEFAULT 0,
  running_jobs INTEGER NOT NULL DEFAULT 0,
  desired_workers INTEGER NOT NULL DEFAULT 0,
  actual_workers INTEGER NOT NULL DEFAULT 0,
  previous_desired INTEGER NOT NULL DEFAULT 0,
  receipt_id TEXT,
  elastic_result JSONB NOT NULL DEFAULT '{}'::jsonb,
  reason TEXT,
  actor TEXT NOT NULL DEFAULT 'system',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
_AUTOSCALING_HISTORY_INDEX = """
CREATE INDEX IF NOT EXISTS idx_worker_autoscaling_history_project
  ON worker_autoscaling_history(project_id, pool_key, created_at DESC)
"""


def is_missing_relation(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", None)
    name = type(orig).__name__ if orig is not None else type(exc).__name__
    blob = str(orig or exc).lower()
    return name == "UndefinedTable" or "does not exist" in blob


@contextmanager
def _savepoint(connection: Connection) -> Iterator[None]:
    begin_nested = getattr(connection, "begin_nested", None)
    nested = None
    if callable(begin_nested):
        try:
            nested = begin_nested()
        except Exception:
            nested = None
    try:
        yield
        if nested is not None:
            nested.commit()
    except Exception:
        if nested is not None:
            try:
                nested.rollback()
            except Exception:
                pass
        raise


DEFAULT_POLICY = {
    "pool_key": "default",
    "enabled": True,
    "min_workers": 0,
    "max_workers": 8,
    "scale_up_threshold": 1,
    "cooldown_seconds": 30,
    "max_queue_depth_per_worker": 5,
    "kind": "local",
    "provider_account_id": None,
    "metadata": {},
}


class WorkerAutoscalingService:
    """Project-scoped multi-pool worker autoscaling over elastic fabric."""

    def __init__(
        self,
        settings: Settings,
        *,
        elastic_fabric: Optional[ElasticFabricService] = None,
        resource_plane: Optional[ResourcePlaneService] = None,
    ):
        self.settings = settings
        self.resource_plane = resource_plane or ResourcePlaneService(settings)
        self.elastic = elastic_fabric or ElasticFabricService(
            settings, resource_plane=self.resource_plane
        )
        self.receipts = ReceiptService()

    def ensure_tables(self, connection: Connection) -> bool:
        """CREATE TABLE IF NOT EXISTS for policies + history. False if unusable."""
        try:
            with _savepoint(connection):
                connection.execute(text(_AUTOSCALING_POLICIES_DDL))
                connection.execute(text(_AUTOSCALING_POLICIES_INDEX))
                connection.execute(text(_AUTOSCALING_HISTORY_DDL))
                connection.execute(text(_AUTOSCALING_HISTORY_INDEX))
            return True
        except Exception as exc:
            blob = str(exc).lower()
            if is_missing_relation(exc) or "permission" in blob or "read-only" in blob:
                return False
            raise

    # ------------------------------------------------------------------
    # Policies
    # ------------------------------------------------------------------

    def list_policies(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        if not self.ensure_tables(connection):
            return []
        try:
            with _savepoint(connection):
                rows = connection.execute(
                    text(
                        """
                        SELECT id, project_id, pool_key, enabled, min_workers, max_workers,
                               scale_up_threshold, cooldown_seconds,
                               max_queue_depth_per_worker, kind, provider_account_id,
                               metadata, last_applied_at, last_scale_direction,
                               created_by, created_at, updated_at
                        FROM worker_autoscaling_policies
                        WHERE project_id = :project_id
                        ORDER BY pool_key ASC
                        """
                    ),
                    {"project_id": project_id},
                ).mappings().all()
            return [self._serialize_policy(row) for row in rows]
        except Exception as exc:
            if is_missing_relation(exc):
                return []
            raise

    def upsert_policy(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        pool_key: str = "default",
        enabled: bool = True,
        min_workers: int = 0,
        max_workers: int = 8,
        scale_up_threshold: int = 1,
        cooldown_seconds: int = 30,
        max_queue_depth_per_worker: int = 5,
        kind: str = "local",
        provider_account_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        pool_key = (pool_key or "default").strip()
        if not pool_key:
            raise ValueError("pool_key is required.")
        if min_workers < 0 or max_workers < 0:
            raise ValueError("worker counts must be non-negative.")
        if min_workers > max_workers:
            raise ValueError("min_workers cannot exceed max_workers.")
        if scale_up_threshold < 0:
            raise ValueError("scale_up_threshold must be non-negative.")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be non-negative.")
        if max_queue_depth_per_worker < 1:
            raise ValueError("max_queue_depth_per_worker must be at least 1.")
        kind_norm = (kind or "local").strip().lower()
        if kind_norm not in {"local", "container", "cloud", "vm"}:
            raise ValueError("kind must be local, container, cloud, or vm.")
        if not self.ensure_tables(connection):
            raise ValueError(
                "worker_autoscaling_policies is not applied on this runtime."
            )

        policy_id = make_id("wasp")
        connection.execute(
            text(
                """
                INSERT INTO worker_autoscaling_policies (
                    id, project_id, pool_key, enabled, min_workers, max_workers,
                    scale_up_threshold, cooldown_seconds, max_queue_depth_per_worker,
                    kind, provider_account_id, metadata, created_by
                )
                VALUES (
                    :id, :project_id, :pool_key, :enabled, :min_workers, :max_workers,
                    :scale_up_threshold, :cooldown_seconds, :max_queue_depth_per_worker,
                    :kind, :provider_account_id, CAST(:metadata AS JSONB), :actor
                )
                ON CONFLICT (project_id, pool_key) DO UPDATE
                SET enabled = EXCLUDED.enabled,
                    min_workers = EXCLUDED.min_workers,
                    max_workers = EXCLUDED.max_workers,
                    scale_up_threshold = EXCLUDED.scale_up_threshold,
                    cooldown_seconds = EXCLUDED.cooldown_seconds,
                    max_queue_depth_per_worker = EXCLUDED.max_queue_depth_per_worker,
                    kind = EXCLUDED.kind,
                    provider_account_id = EXCLUDED.provider_account_id,
                    metadata = EXCLUDED.metadata,
                    updated_at = now()
                """
            ),
            {
                "id": policy_id,
                "project_id": project_id,
                "pool_key": pool_key,
                "enabled": enabled,
                "min_workers": min_workers,
                "max_workers": max_workers,
                "scale_up_threshold": scale_up_threshold,
                "cooldown_seconds": cooldown_seconds,
                "max_queue_depth_per_worker": max_queue_depth_per_worker,
                "kind": kind_norm,
                "provider_account_id": provider_account_id,
                "metadata": json.dumps(metadata or {}),
                "actor": actor,
            },
        )
        policy = self._get_policy(connection, project_id, pool_key)
        emit_event(
            connection,
            project_id,
            "worker_autoscaling.policy_upserted",
            actor,
            "worker_autoscaling_policy",
            policy["id"],
            {
                "pool_key": pool_key,
                "min_workers": min_workers,
                "max_workers": max_workers,
                "scale_up_threshold": scale_up_threshold,
                "cooldown_seconds": cooldown_seconds,
                "enabled": enabled,
            },
        )
        return policy

    def _get_policy(
        self, connection: Connection, project_id: str, pool_key: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, pool_key, enabled, min_workers, max_workers,
                       scale_up_threshold, cooldown_seconds,
                       max_queue_depth_per_worker, kind, provider_account_id,
                       metadata, last_applied_at, last_scale_direction,
                       created_by, created_at, updated_at
                FROM worker_autoscaling_policies
                WHERE project_id = :project_id AND pool_key = :pool_key
                """
            ),
            {"project_id": project_id, "pool_key": pool_key},
        ).mappings().first()
        if not row:
            raise ValueError(f"No autoscaling policy for pool_key={pool_key!r}.")
        return self._serialize_policy(row)

    def _ensure_policy(
        self,
        connection: Connection,
        project_id: str,
        pool_key: str,
        actor: str,
    ) -> dict[str, Any]:
        try:
            return self._get_policy(connection, project_id, pool_key)
        except ValueError:
            return self.upsert_policy(
                connection,
                project_id=project_id,
                actor=actor,
                pool_key=pool_key,
                **{
                    k: v
                    for k, v in DEFAULT_POLICY.items()
                    if k not in {"pool_key", "metadata"}
                },
                metadata={},
            )

    # ------------------------------------------------------------------
    # Queue + desired calculation (deterministic)
    # ------------------------------------------------------------------

    def queue_snapshot(
        self, connection: Connection, project_id: str
    ) -> dict[str, int]:
        """Project-wide job queue pressure (same definition as Budget-Aware Autoscaler)."""
        empty = {
            "queue_depth": 0,
            "running_jobs": 0,
            "leased_jobs": 0,
            "retryable_jobs": 0,
        }
        try:
            with _savepoint(connection):
                row = connection.execute(
                    text(
                        """
                        SELECT
                          COUNT(*) FILTER (
                            WHERE status IN ('queued', 'failed_retryable')
                              AND run_after <= now()
                          ) AS queue_depth,
                          COUNT(*) FILTER (WHERE status = 'running') AS running_jobs,
                          COUNT(*) FILTER (WHERE status = 'leased') AS leased_jobs,
                          COUNT(*) FILTER (WHERE status = 'failed_retryable') AS retryable_jobs
                        FROM jobs
                        WHERE project_id = :project_id
                        """
                    ),
                    {"project_id": project_id},
                ).mappings().one()
            return {
                "queue_depth": int(row["queue_depth"] or 0),
                "running_jobs": int(row["running_jobs"] or 0),
                "leased_jobs": int(row["leased_jobs"] or 0),
                "retryable_jobs": int(row["retryable_jobs"] or 0),
            }
        except Exception as exc:
            if is_missing_relation(exc):
                return empty
            raise

    @staticmethod
    def compute_desired_workers(
        *,
        queue_depth: int,
        min_workers: int,
        max_workers: int,
        max_queue_depth_per_worker: int,
        scale_up_threshold: int,
        current_desired: int,
        cooldown_seconds: int,
        last_applied_at: Optional[datetime],
        now: Optional[datetime] = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Pure deterministic policy — unit-testable without DB.

        Returns:
          desired_workers, needed_workers, action, hold_reason
        """
        now = now or _utcnow()
        per_worker = max(int(max_queue_depth_per_worker), 1)
        queue_depth = max(int(queue_depth), 0)
        min_workers = max(int(min_workers), 0)
        max_workers = max(int(max_workers), min_workers)
        current_desired = max(int(current_desired), 0)

        if not enabled:
            return {
                "needed_workers": 0,
                "raw_target": current_desired,
                "desired_workers": current_desired,
                "action": "disabled",
                "hold_reason": "policy_disabled",
            }

        if queue_depth <= 0:
            needed = 0
        else:
            needed = int(math.ceil(queue_depth / per_worker))

        raw_target = max(min_workers, min(needed, max_workers))
        desired = raw_target
        hold_reason: Optional[str] = None
        action = "no_change"

        if raw_target > current_desired:
            # Scale-up gates
            if queue_depth < int(scale_up_threshold):
                desired = current_desired
                hold_reason = "below_scale_up_threshold"
                action = "held_threshold"
            elif last_applied_at is not None and cooldown_seconds > 0:
                last = last_applied_at
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                elapsed = (now - last).total_seconds()
                if elapsed < cooldown_seconds:
                    desired = current_desired
                    hold_reason = "cooldown"
                    action = "held_cooldown"
            if desired > current_desired:
                action = "scale_up"
        elif raw_target < current_desired:
            # Scale-down also respects cooldown to avoid thrash
            if last_applied_at is not None and cooldown_seconds > 0:
                last = last_applied_at
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                elapsed = (now - last).total_seconds()
                if elapsed < cooldown_seconds:
                    desired = current_desired
                    hold_reason = "cooldown"
                    action = "held_cooldown"
                else:
                    action = "scale_down"
            else:
                action = "scale_down"
        else:
            action = "no_change"

        return {
            "needed_workers": needed,
            "raw_target": raw_target,
            "desired_workers": desired,
            "action": action,
            "hold_reason": hold_reason,
        }

    def evaluate(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str = "system",
        pool_key: Optional[str] = None,
        persist_history: bool = True,
    ) -> dict[str, Any]:
        """Compute desired workers for one pool or all policies (multi-pool)."""
        queue = self.queue_snapshot(connection, project_id)
        if pool_key:
            policies = [self._ensure_policy(connection, project_id, pool_key, actor)]
        else:
            policies = self.list_policies(connection, project_id)
            if not policies:
                policies = [
                    self._ensure_policy(connection, project_id, "default", actor)
                ]

        evaluations: list[dict[str, Any]] = []
        for policy in policies:
            pool_eval = self._evaluate_one(
                connection,
                project_id=project_id,
                actor=actor,
                policy=policy,
                queue=queue,
                persist_history=persist_history,
            )
            evaluations.append(pool_eval)

        return {
            "project_id": project_id,
            "queue": queue,
            "evaluations": evaluations,
            "infra_real_provision_allowed": bool(
                self.settings.infra_real_provision_allowed
            ),
        }

    def _evaluate_one(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        policy: dict[str, Any],
        queue: dict[str, int],
        persist_history: bool,
    ) -> dict[str, Any]:
        pool = self._resolve_or_create_pool(
            connection,
            project_id=project_id,
            actor=actor,
            policy=policy,
        )
        current_desired = int(pool.get("desired_workers") or 0)
        actual = int(pool.get("current_workers") or 0)
        # Prefer live elastic instance count when available
        try:
            instances = self.elastic.list_instances(
                connection,
                project_id,
                worker_pool_id=pool["id"],
                active_only=True,
            )
            actual = len(instances)
        except Exception:
            pass

        last_applied = policy.get("last_applied_at")
        last_dt: Optional[datetime] = None
        if isinstance(last_applied, datetime):
            last_dt = last_applied
        elif isinstance(last_applied, str) and last_applied:
            try:
                last_dt = datetime.fromisoformat(last_applied.replace("Z", "+00:00"))
            except ValueError:
                last_dt = None

        calc = self.compute_desired_workers(
            queue_depth=queue["queue_depth"],
            min_workers=int(policy["min_workers"]),
            max_workers=int(policy["max_workers"]),
            max_queue_depth_per_worker=int(policy["max_queue_depth_per_worker"]),
            scale_up_threshold=int(policy["scale_up_threshold"]),
            current_desired=current_desired,
            cooldown_seconds=int(policy["cooldown_seconds"]),
            last_applied_at=last_dt,
            enabled=bool(policy.get("enabled", True)),
        )

        result = {
            "pool_key": policy["pool_key"],
            "policy_id": policy["id"],
            "worker_pool_id": pool["id"],
            "queue_depth": queue["queue_depth"],
            "leased_jobs": queue["leased_jobs"],
            "running_jobs": queue["running_jobs"],
            "current_desired": current_desired,
            "actual_workers": actual,
            "needed_workers": calc["needed_workers"],
            "raw_target": calc["raw_target"],
            "desired_workers": calc["desired_workers"],
            "action": calc["action"],
            "hold_reason": calc["hold_reason"],
            "delta": calc["desired_workers"] - current_desired,
            "policy": policy,
            "worker_pool": pool,
        }

        if persist_history:
            self._record_history(
                connection,
                project_id=project_id,
                pool_key=policy["pool_key"],
                action="evaluate",
                status=calc["action"],
                queue=queue,
                desired_workers=calc["desired_workers"],
                actual_workers=actual,
                previous_desired=current_desired,
                reason=calc["hold_reason"] or calc["action"],
                actor=actor,
                elastic_result=None,
                receipt_id=None,
            )
            emit_event(
                connection,
                project_id,
                "worker_autoscaling.evaluated",
                actor,
                "worker_autoscaling_policy",
                policy["id"],
                {
                    "pool_key": policy["pool_key"],
                    "desired_workers": calc["desired_workers"],
                    "current_desired": current_desired,
                    "action": calc["action"],
                    "queue_depth": queue["queue_depth"],
                },
            )
        return result

    # ------------------------------------------------------------------
    # Apply via elastic fabric
    # ------------------------------------------------------------------

    def apply(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str = "system",
        pool_key: Optional[str] = None,
        force: bool = False,
        auto_reconcile: bool = True,
        cloud_spillover_enabled: bool = False,
        reason: Optional[str] = None,
    ) -> dict[str, Any]:
        """Evaluate then request elastic capacity when desired changes.

        force=True bypasses cooldown/threshold holds and applies raw_target
        (still clamped to min/max). Real provision still gated by elastic fabric.
        """
        evaluation = self.evaluate(
            connection,
            project_id=project_id,
            actor=actor,
            pool_key=pool_key,
            persist_history=False,
        )
        applied: list[dict[str, Any]] = []
        for pool_eval in evaluation["evaluations"]:
            applied.append(
                self._apply_one(
                    connection,
                    project_id=project_id,
                    actor=actor,
                    pool_eval=pool_eval,
                    force=force,
                    auto_reconcile=auto_reconcile,
                    cloud_spillover_enabled=cloud_spillover_enabled,
                    reason=reason,
                )
            )

        return {
            "project_id": project_id,
            "queue": evaluation["queue"],
            "results": applied,
            "infra_real_provision_allowed": bool(
                self.settings.infra_real_provision_allowed
            ),
        }

    def _apply_one(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        pool_eval: dict[str, Any],
        force: bool,
        auto_reconcile: bool,
        cloud_spillover_enabled: bool,
        reason: Optional[str],
    ) -> dict[str, Any]:
        policy = pool_eval["policy"]
        action = pool_eval["action"]
        target = int(pool_eval["desired_workers"])
        previous = int(pool_eval["current_desired"])

        if force and action in {"held_cooldown", "held_threshold", "disabled"}:
            # Recompute without holds
            calc = self.compute_desired_workers(
                queue_depth=pool_eval["queue_depth"],
                min_workers=int(policy["min_workers"]),
                max_workers=int(policy["max_workers"]),
                max_queue_depth_per_worker=int(policy["max_queue_depth_per_worker"]),
                scale_up_threshold=0 if force else int(policy["scale_up_threshold"]),
                current_desired=previous,
                cooldown_seconds=0 if force else int(policy["cooldown_seconds"]),
                last_applied_at=None,
                enabled=True if force else bool(policy.get("enabled", True)),
            )
            target = int(calc["desired_workers"])
            action = calc["action"] if calc["action"] != "no_change" or target != previous else (
                "scale_up" if target > previous else (
                    "scale_down" if target < previous else "no_change"
                )
            )

        if target == previous or action in {
            "no_change",
            "held_cooldown",
            "held_threshold",
            "disabled",
        }:
            status = action if action != "no_change" else "no_change"
            history = self._record_history(
                connection,
                project_id=project_id,
                pool_key=policy["pool_key"],
                action="apply",
                status=status,
                queue={
                    "queue_depth": pool_eval["queue_depth"],
                    "leased_jobs": pool_eval["leased_jobs"],
                    "running_jobs": pool_eval["running_jobs"],
                    "retryable_jobs": 0,
                },
                desired_workers=target,
                actual_workers=pool_eval["actual_workers"],
                previous_desired=previous,
                reason=pool_eval.get("hold_reason") or status,
                actor=actor,
                elastic_result=None,
                receipt_id=None,
            )
            return {
                "pool_key": policy["pool_key"],
                "status": status,
                "desired_workers": target,
                "previous_desired": previous,
                "actual_workers": pool_eval["actual_workers"],
                "action": action,
                "hold_reason": pool_eval.get("hold_reason"),
                "receipt_id": None,
                "elastic": None,
                "history_id": history["id"],
            }

        # Cap elastic pool max_workers so request_capacity does not reject
        self._align_pool_limits(
            connection,
            project_id=project_id,
            pool_id=pool_eval["worker_pool_id"],
            min_workers=int(policy["min_workers"]),
            max_workers=int(policy["max_workers"]),
        )

        elastic_result = self.elastic.request_capacity(
            connection,
            project_id=project_id,
            actor=actor,
            desired_workers=target,
            pool_key=policy["pool_key"],
            kind=policy.get("kind") or "local",
            provider_account_id=policy.get("provider_account_id"),
            auto_reconcile=auto_reconcile,
            cloud_spillover_enabled=cloud_spillover_enabled
            or (policy.get("kind") in {"cloud", "vm"}),
            reason=reason
            or f"worker_autoscaling:{action}:queue={pool_eval['queue_depth']}",
            metadata={
                "source": "worker_autoscaling",
                "policy_id": policy["id"],
                "action": action,
                "queue_depth": pool_eval["queue_depth"],
            },
        )

        receipt_id = elastic_result.get("receipt_id")
        direction = "up" if target > previous else "down"
        connection.execute(
            text(
                """
                UPDATE worker_autoscaling_policies
                SET last_applied_at = now(),
                    last_scale_direction = :direction,
                    updated_at = now()
                WHERE project_id = :project_id AND pool_key = :pool_key
                """
            ),
            {
                "project_id": project_id,
                "pool_key": policy["pool_key"],
                "direction": direction,
            },
        )

        # Extra receipt for the autoscaling decision itself (deduped per apply)
        scope = self.elastic._project_scope(connection, project_id)
        as_receipt, _ = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            (
                f"worker-autoscaling:apply:{project_id}:{policy['pool_key']}:"
                f"{previous}->{target}:{pool_eval['queue_depth']}"
            ),
            "worker_autoscaling.apply",
            {
                "pool_key": policy["pool_key"],
                "previous_desired": previous,
                "desired_workers": target,
                "queue_depth": pool_eval["queue_depth"],
                "action": action,
            },
            {
                "operation": "apply",
                "actor": actor,
                "elastic_receipt_id": receipt_id,
                "data_boundary": "internal",
            },
        )
        self.receipts.mark_succeeded(
            connection,
            as_receipt["id"],
            f"pool:{policy['pool_key']}:desired:{target}",
            {
                "previous_desired": previous,
                "desired_workers": target,
                "elastic_receipt_id": receipt_id,
            },
        )

        actual_after = pool_eval["actual_workers"]
        recon = elastic_result.get("reconcile") or {}
        if recon.get("active_instances") is not None:
            actual_after = int(recon["active_instances"])

        history = self._record_history(
            connection,
            project_id=project_id,
            pool_key=policy["pool_key"],
            action="apply",
            status="applied",
            queue={
                "queue_depth": pool_eval["queue_depth"],
                "leased_jobs": pool_eval["leased_jobs"],
                "running_jobs": pool_eval["running_jobs"],
                "retryable_jobs": 0,
            },
            desired_workers=target,
            actual_workers=actual_after,
            previous_desired=previous,
            reason=reason or action,
            actor=actor,
            elastic_result={
                "receipt_id": receipt_id,
                "desired_workers": elastic_result.get("desired_workers"),
                "reconcile_active": recon.get("active_instances"),
                "actions": recon.get("actions"),
            },
            receipt_id=as_receipt["id"],
        )

        emit_event(
            connection,
            project_id,
            "worker_autoscaling.applied",
            actor,
            "worker_autoscaling_policy",
            policy["id"],
            {
                "pool_key": policy["pool_key"],
                "previous_desired": previous,
                "desired_workers": target,
                "action": action,
                "receipt_id": as_receipt["id"],
                "elastic_receipt_id": receipt_id,
                "queue_depth": pool_eval["queue_depth"],
            },
        )

        return {
            "pool_key": policy["pool_key"],
            "status": "applied",
            "desired_workers": target,
            "previous_desired": previous,
            "actual_workers": actual_after,
            "action": action,
            "hold_reason": None,
            "receipt_id": as_receipt["id"],
            "elastic_receipt_id": receipt_id,
            "elastic": elastic_result,
            "history_id": history["id"],
        }

    # ------------------------------------------------------------------
    # Status + history
    # ------------------------------------------------------------------

    def status(
        self, connection: Connection, project_id: str
    ) -> dict[str, Any]:
        ready = self.ensure_tables(connection)
        queue = self.queue_snapshot(connection, project_id)
        policies = self.list_policies(connection, project_id)
        try:
            fabric = self.elastic.fabric_status(connection, project_id)
        except Exception as exc:
            if not is_missing_relation(exc):
                raise
            fabric = {"pools": [], "instance_counts": {}}

        pools_out: list[dict[str, Any]] = []
        for policy in policies or []:
            try:
                pool = self.resource_plane._get_worker_pool_by_key(
                    connection, project_id, policy["pool_key"]
                )
            except Exception:
                pool = None
            actual = int((pool or {}).get("current_workers") or 0)
            desired = int((pool or {}).get("desired_workers") or 0)
            if pool:
                try:
                    actual = len(
                        self.elastic.list_instances(
                            connection,
                            project_id,
                            worker_pool_id=pool["id"],
                            active_only=True,
                        )
                    )
                except Exception:
                    pass
            calc = self.compute_desired_workers(
                queue_depth=queue["queue_depth"],
                min_workers=int(policy["min_workers"]),
                max_workers=int(policy["max_workers"]),
                max_queue_depth_per_worker=int(policy["max_queue_depth_per_worker"]),
                scale_up_threshold=int(policy["scale_up_threshold"]),
                current_desired=desired,
                cooldown_seconds=int(policy["cooldown_seconds"]),
                last_applied_at=self._parse_dt(policy.get("last_applied_at")),
                enabled=bool(policy.get("enabled", True)),
            )
            pools_out.append(
                {
                    "pool_key": policy["pool_key"],
                    "policy_id": policy["id"],
                    "enabled": policy["enabled"],
                    "kind": policy["kind"],
                    "min_workers": policy["min_workers"],
                    "max_workers": policy["max_workers"],
                    "scale_up_threshold": policy["scale_up_threshold"],
                    "cooldown_seconds": policy["cooldown_seconds"],
                    "desired_workers": desired,
                    "actual_workers": actual,
                    "policy_desired": calc["desired_workers"],
                    "action": calc["action"],
                    "hold_reason": calc["hold_reason"],
                    "last_applied_at": policy.get("last_applied_at"),
                    "last_scale_direction": policy.get("last_scale_direction"),
                    "worker_pool_id": (pool or {}).get("id"),
                }
            )

        # Include fabric pools that lack a policy yet (read-only status)
        known = {p["pool_key"] for p in pools_out}
        for pool in fabric.get("pools") or []:
            if pool["pool_key"] in known:
                continue
            pools_out.append(
                {
                    "pool_key": pool["pool_key"],
                    "policy_id": None,
                    "enabled": False,
                    "kind": pool.get("kind"),
                    "min_workers": pool.get("min_workers"),
                    "max_workers": pool.get("max_workers"),
                    "scale_up_threshold": None,
                    "cooldown_seconds": None,
                    "desired_workers": pool.get("desired_workers"),
                    "actual_workers": pool.get("current_workers"),
                    "policy_desired": None,
                    "action": "no_policy",
                    "hold_reason": "no_policy",
                    "last_applied_at": None,
                    "last_scale_direction": None,
                    "worker_pool_id": pool.get("id"),
                }
            )

        return {
            "project_id": project_id,
            "queue": queue,
            "leased_jobs": queue["leased_jobs"],
            "queue_depth": queue["queue_depth"],
            "running_jobs": queue["running_jobs"],
            "pools": pools_out,
            "instance_counts": fabric.get("instance_counts"),
            "infra_real_provision_allowed": bool(
                self.settings.infra_real_provision_allowed
            ),
            "schema_ready": ready,
            "configured": bool(policies),
            "message": (
                "Worker autoscaling evaluates queue depth with deterministic policy "
                "(min/max, scale_up_threshold, cooldown) and applies via elastic fabric. "
                "Real cloud provision requires BOSS_INFRA_REAL_PROVISION_ALLOWED."
            ),
        }

    def history(
        self,
        connection: Connection,
        project_id: str,
        *,
        pool_key: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if not self.ensure_tables(connection):
            return []
        limit = max(1, min(int(limit), 200))
        clauses = ["project_id = :project_id"]
        params: dict[str, Any] = {"project_id": project_id, "limit": limit}
        if pool_key:
            clauses.append("pool_key = :pool_key")
            params["pool_key"] = pool_key
        try:
            with _savepoint(connection):
                rows = connection.execute(
                    text(
                        f"""
                        SELECT id, project_id, pool_key, action, status, queue_depth,
                               leased_jobs, running_jobs, desired_workers, actual_workers,
                               previous_desired, receipt_id, elastic_result, reason,
                               actor, created_at
                        FROM worker_autoscaling_history
                        WHERE {' AND '.join(clauses)}
                        ORDER BY created_at DESC
                        LIMIT :limit
                        """
                    ),
                    params,
                ).mappings().all()
            return [self._serialize_history(row) for row in rows]
        except Exception as exc:
            if is_missing_relation(exc):
                return []
            raise

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_or_create_pool(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        return self.elastic._resolve_pool(
            connection,
            project_id=project_id,
            actor=actor,
            pool_key=policy["pool_key"],
            kind=policy.get("kind") or "local",
            provider_account_id=policy.get("provider_account_id"),
        )

    def _align_pool_limits(
        self,
        connection: Connection,
        *,
        project_id: str,
        pool_id: str,
        min_workers: int,
        max_workers: int,
    ) -> None:
        connection.execute(
            text(
                """
                UPDATE worker_pools
                SET min_workers = :min_workers,
                    max_workers = GREATEST(max_workers, :max_workers),
                    updated_at = now()
                WHERE id = :id AND project_id = :project_id
                """
            ),
            {
                "id": pool_id,
                "project_id": project_id,
                "min_workers": min_workers,
                "max_workers": max_workers,
            },
        )

    def _record_history(
        self,
        connection: Connection,
        *,
        project_id: str,
        pool_key: str,
        action: str,
        status: str,
        queue: dict[str, int],
        desired_workers: int,
        actual_workers: int,
        previous_desired: int,
        reason: Optional[str],
        actor: str,
        elastic_result: Optional[dict[str, Any]],
        receipt_id: Optional[str],
    ) -> dict[str, Any]:
        history_id = make_id("wash")
        connection.execute(
            text(
                """
                INSERT INTO worker_autoscaling_history (
                    id, project_id, pool_key, action, status, queue_depth,
                    leased_jobs, running_jobs, desired_workers, actual_workers,
                    previous_desired, receipt_id, elastic_result, reason, actor
                )
                VALUES (
                    :id, :project_id, :pool_key, :action, :status, :queue_depth,
                    :leased_jobs, :running_jobs, :desired_workers, :actual_workers,
                    :previous_desired, :receipt_id, CAST(:elastic_result AS JSONB),
                    :reason, :actor
                )
                """
            ),
            {
                "id": history_id,
                "project_id": project_id,
                "pool_key": pool_key,
                "action": action,
                "status": status,
                "queue_depth": int(queue.get("queue_depth") or 0),
                "leased_jobs": int(queue.get("leased_jobs") or 0),
                "running_jobs": int(queue.get("running_jobs") or 0),
                "desired_workers": desired_workers,
                "actual_workers": actual_workers,
                "previous_desired": previous_desired,
                "receipt_id": receipt_id,
                "elastic_result": json.dumps(elastic_result or {}),
                "reason": reason,
                "actor": actor,
            },
        )
        return self._get_history(connection, project_id, history_id)

    def _get_history(
        self, connection: Connection, project_id: str, history_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, pool_key, action, status, queue_depth,
                       leased_jobs, running_jobs, desired_workers, actual_workers,
                       previous_desired, receipt_id, elastic_result, reason,
                       actor, created_at
                FROM worker_autoscaling_history
                WHERE id = :id AND project_id = :project_id
                """
            ),
            {"id": history_id, "project_id": project_id},
        ).mappings().one()
        return self._serialize_history(row)

    @staticmethod
    def _parse_dt(value: Any) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None

    @staticmethod
    def _serialize_policy(row: Any) -> dict[str, Any]:
        meta = row["metadata"] or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "pool_key": row["pool_key"],
            "enabled": bool(row["enabled"]),
            "min_workers": int(row["min_workers"]),
            "max_workers": int(row["max_workers"]),
            "scale_up_threshold": int(row["scale_up_threshold"]),
            "cooldown_seconds": int(row["cooldown_seconds"]),
            "max_queue_depth_per_worker": int(row["max_queue_depth_per_worker"]),
            "kind": row["kind"],
            "provider_account_id": row["provider_account_id"],
            "metadata": meta if isinstance(meta, dict) else {},
            "last_applied_at": _iso(row["last_applied_at"]),
            "last_scale_direction": row["last_scale_direction"],
            "created_by": row["created_by"],
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
        }

    @staticmethod
    def _serialize_history(row: Any) -> dict[str, Any]:
        elastic = row["elastic_result"] or {}
        if isinstance(elastic, str):
            elastic = json.loads(elastic)
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "pool_key": row["pool_key"],
            "action": row["action"],
            "status": row["status"],
            "queue_depth": int(row["queue_depth"] or 0),
            "leased_jobs": int(row["leased_jobs"] or 0),
            "running_jobs": int(row["running_jobs"] or 0),
            "desired_workers": int(row["desired_workers"] or 0),
            "actual_workers": int(row["actual_workers"] or 0),
            "previous_desired": int(row["previous_desired"] or 0),
            "receipt_id": row["receipt_id"],
            "elastic_result": elastic if isinstance(elastic, dict) else {},
            "reason": row["reason"],
            "actor": row["actor"],
            "created_at": _iso(row["created_at"]),
        }
