"""Provider SLO definitions and evaluation (BOSS-241 depth).

Beyond basic ``provider_health`` polling: declare SLOs (metric, target, window,
comparator), evaluate them from latest health samples / cost events / simple
availability, and persist evaluation history.

Fail-closed: when no source data exists in the evaluation window, status is
``unknown`` — never silent success (``ok``).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id, now_utc

VALID_COMPARATORS = frozenset({"gte", "lte", "gt", "lt", "eq"})
VALID_METRIC_KEYS = frozenset(
    {
        "availability",  # fraction of samples with api_available=true
        "healthy_ratio",  # fraction of samples with status=healthy
        "api_available",  # latest sample: 1.0 if api_available else 0.0
        "cost_usd_micros",  # sum of cost_events.amount_usd_micros in window
        "sample_count",  # number of health samples in window
    }
)
VALID_STATUSES = frozenset({"ok", "breached", "unknown"})


class ProviderSloService:
    """CRUD + evaluation for project-scoped provider SLOs."""

    def __init__(self, settings: Any = None) -> None:
        self.settings = settings

    def upsert_slo(
        self,
        connection: Connection,
        *,
        project_id: str,
        provider: str,
        metric_key: str,
        target: float,
        window_seconds: int = 3600,
        comparator: str = "gte",
        provider_account_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        actor: str = "system",
        slo_id: Optional[str] = None,
    ) -> dict[str, Any]:
        provider = (provider or "").strip().lower()
        metric_key = (metric_key or "").strip().lower()
        comparator = (comparator or "gte").strip().lower()
        if not project_id:
            raise ValueError("project_id is required")
        if not provider:
            raise ValueError("provider is required")
        if metric_key not in VALID_METRIC_KEYS:
            raise ValueError(
                f"metric_key must be one of: {', '.join(sorted(VALID_METRIC_KEYS))}"
            )
        if comparator not in VALID_COMPARATORS:
            raise ValueError(
                f"comparator must be one of: {', '.join(sorted(VALID_COMPARATORS))}"
            )
        if window_seconds < 60 or window_seconds > 30 * 24 * 3600:
            raise ValueError("window_seconds must be between 60 and 2592000 (30d)")
        try:
            target_f = float(target)
        except (TypeError, ValueError) as exc:
            raise ValueError("target must be a number") from exc

        meta = metadata if isinstance(metadata, dict) else {}
        existing = None
        if slo_id:
            existing = self._get_definition(connection, slo_id, project_id=project_id)
            if existing is None:
                raise ValueError(f"provider SLO not found: {slo_id}")
        else:
            existing = self._find_definition(
                connection,
                project_id=project_id,
                provider=provider,
                provider_account_id=provider_account_id,
                metric_key=metric_key,
                window_seconds=window_seconds,
            )

        if existing:
            connection.execute(
                text(
                    """
                    UPDATE provider_slo_definitions
                    SET provider = :provider,
                        provider_account_id = :provider_account_id,
                        metric_key = :metric_key,
                        target = :target,
                        window_seconds = :window_seconds,
                        comparator = :comparator,
                        metadata = CAST(:metadata AS JSONB),
                        updated_at = now()
                    WHERE id = :id
                    """
                ),
                {
                    "id": existing["id"],
                    "provider": provider,
                    "provider_account_id": provider_account_id,
                    "metric_key": metric_key,
                    "target": target_f,
                    "window_seconds": int(window_seconds),
                    "comparator": comparator,
                    "metadata": json.dumps(meta),
                },
            )
            row = self._get_definition(connection, existing["id"])
            emit_event(
                connection,
                project_id,
                "provider_slo.updated",
                actor,
                "provider_slo_definition",
                existing["id"],
                {
                    "provider": provider,
                    "metric_key": metric_key,
                    "target": target_f,
                    "comparator": comparator,
                    "window_seconds": int(window_seconds),
                },
            )
            return row  # type: ignore[return-value]

        new_id = make_id("pslo")
        connection.execute(
            text(
                """
                INSERT INTO provider_slo_definitions (
                    id, project_id, provider, provider_account_id, metric_key,
                    target, window_seconds, comparator, status, metadata, created_by
                )
                VALUES (
                    :id, :project_id, :provider, :provider_account_id, :metric_key,
                    :target, :window_seconds, :comparator, 'unknown',
                    CAST(:metadata AS JSONB), :created_by
                )
                """
            ),
            {
                "id": new_id,
                "project_id": project_id,
                "provider": provider,
                "provider_account_id": provider_account_id,
                "metric_key": metric_key,
                "target": target_f,
                "window_seconds": int(window_seconds),
                "comparator": comparator,
                "metadata": json.dumps(meta),
                "created_by": actor,
            },
        )
        row = self._get_definition(connection, new_id)
        emit_event(
            connection,
            project_id,
            "provider_slo.created",
            actor,
            "provider_slo_definition",
            new_id,
            {
                "provider": provider,
                "metric_key": metric_key,
                "target": target_f,
                "comparator": comparator,
                "window_seconds": int(window_seconds),
            },
        )
        return row  # type: ignore[return-value]

    def list_slos(
        self,
        connection: Connection,
        project_id: str,
        *,
        provider: Optional[str] = None,
        provider_account_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses = ["project_id = :project_id"]
        params: dict[str, Any] = {
            "project_id": project_id,
            "limit": limit,
            "offset": offset,
        }
        if provider:
            clauses.append("provider = :provider")
            params["provider"] = provider.strip().lower()
        if provider_account_id:
            clauses.append("provider_account_id = :provider_account_id")
            params["provider_account_id"] = provider_account_id
        rows = connection.execute(
            text(
                f"""
                SELECT id, project_id, provider, provider_account_id, metric_key,
                       target, window_seconds, comparator, status, last_value,
                       evaluated_at, metadata, created_by, created_at, updated_at
                FROM provider_slo_definitions
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at DESC, created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        ).mappings().all()
        return [self._serialize_definition(row) for row in rows]

    def evaluate_slo(
        self,
        connection: Connection,
        slo_id: str,
        *,
        project_id: Optional[str] = None,
        actor: str = "system",
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        definition = self._get_definition(connection, slo_id, project_id=project_id)
        if definition is None:
            raise ValueError(f"provider SLO not found: {slo_id}")
        return self._evaluate_definition(
            connection, definition, actor=actor, now=now or now_utc()
        )

    def evaluate_all_for_project(
        self,
        connection: Connection,
        project_id: str,
        *,
        actor: str = "system",
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        definitions = self.list_slos(connection, project_id, limit=500, offset=0)
        clock = now or now_utc()
        evaluations: list[dict[str, Any]] = []
        for definition in definitions:
            evaluations.append(
                self._evaluate_definition(
                    connection, definition, actor=actor, now=clock
                )
            )
        summary = {
            "ok": sum(1 for e in evaluations if e["status"] == "ok"),
            "breached": sum(1 for e in evaluations if e["status"] == "breached"),
            "unknown": sum(1 for e in evaluations if e["status"] == "unknown"),
        }
        emit_event(
            connection,
            project_id,
            "provider_slo.evaluated_all",
            actor,
            "project",
            project_id,
            {"count": len(evaluations), **summary},
        )
        return {
            "project_id": project_id,
            "count": len(evaluations),
            "summary": summary,
            "evaluations": evaluations,
        }

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _evaluate_definition(
        self,
        connection: Connection,
        definition: dict[str, Any],
        *,
        actor: str,
        now: datetime,
    ) -> dict[str, Any]:
        metric_key = definition["metric_key"]
        window_seconds = int(definition["window_seconds"])
        target = float(definition["target"])
        comparator = definition["comparator"]
        provider = definition["provider"]
        project_id = definition["project_id"]
        account_id = definition.get("provider_account_id")
        since = now - timedelta(seconds=window_seconds)

        value, sample_meta = self._compute_metric(
            connection,
            project_id=project_id,
            provider=provider,
            provider_account_id=account_id,
            metric_key=metric_key,
            since=since,
            now=now,
        )

        if value is None:
            # Fail-closed: no data ⇒ unknown, never silent success.
            status = "unknown"
        else:
            status = "ok" if self._compare(value, comparator, target) else "breached"

        eval_meta = {
            "window_start": since.isoformat(),
            "window_end": now.isoformat(),
            "sample_meta": sample_meta,
            "fail_closed_no_data": value is None,
        }

        eval_id = make_id("psloe")
        connection.execute(
            text(
                """
                INSERT INTO provider_slo_evaluations (
                    id, definition_id, project_id, provider, provider_account_id,
                    metric_key, target, window_seconds, comparator, status,
                    last_value, evaluated_at, metadata
                )
                VALUES (
                    :id, :definition_id, :project_id, :provider, :provider_account_id,
                    :metric_key, :target, :window_seconds, :comparator, :status,
                    :last_value, :evaluated_at, CAST(:metadata AS JSONB)
                )
                """
            ),
            {
                "id": eval_id,
                "definition_id": definition["id"],
                "project_id": project_id,
                "provider": provider,
                "provider_account_id": account_id,
                "metric_key": metric_key,
                "target": target,
                "window_seconds": window_seconds,
                "comparator": comparator,
                "status": status,
                "last_value": value,
                "evaluated_at": now,
                "metadata": json.dumps(eval_meta),
            },
        )
        connection.execute(
            text(
                """
                UPDATE provider_slo_definitions
                SET status = :status,
                    last_value = :last_value,
                    evaluated_at = :evaluated_at,
                    updated_at = now()
                WHERE id = :id
                """
            ),
            {
                "id": definition["id"],
                "status": status,
                "last_value": value,
                "evaluated_at": now,
            },
        )
        emit_event(
            connection,
            project_id,
            "provider_slo.evaluated",
            actor,
            "provider_slo_evaluation",
            eval_id,
            {
                "definition_id": definition["id"],
                "provider": provider,
                "metric_key": metric_key,
                "status": status,
                "last_value": value,
                "target": target,
                "comparator": comparator,
            },
        )
        return self._serialize_evaluation(
            connection.execute(
                text(
                    """
                    SELECT id, definition_id, project_id, provider, provider_account_id,
                           metric_key, target, window_seconds, comparator, status,
                           last_value, evaluated_at, metadata, created_at
                    FROM provider_slo_evaluations
                    WHERE id = :id
                    """
                ),
                {"id": eval_id},
            ).mappings().one()
        )

    def _compute_metric(
        self,
        connection: Connection,
        *,
        project_id: str,
        provider: str,
        provider_account_id: Optional[str],
        metric_key: str,
        since: datetime,
        now: datetime,
    ) -> tuple[Optional[float], dict[str, Any]]:
        if metric_key == "cost_usd_micros":
            return self._metric_cost(
                connection,
                project_id=project_id,
                provider=provider,
                provider_account_id=provider_account_id,
                since=since,
            )
        # Health-based metrics
        samples = self._health_samples(
            connection,
            project_id=project_id,
            provider=provider,
            provider_account_id=provider_account_id,
            since=since,
        )
        count = len(samples)
        meta: dict[str, Any] = {"sample_count": count, "source": "provider_health"}

        if metric_key == "sample_count":
            # sample_count of zero is still a measured value (not "no data")
            return float(count), meta

        if count == 0:
            return None, {**meta, "reason": "no_health_samples_in_window"}

        if metric_key == "availability":
            available = sum(1 for s in samples if s["api_available"])
            return available / count, {**meta, "available": available}

        if metric_key == "healthy_ratio":
            healthy = sum(1 for s in samples if (s["status"] or "").lower() == "healthy")
            return healthy / count, {**meta, "healthy": healthy}

        if metric_key == "api_available":
            # Latest sample only (ordered DESC in query)
            latest = samples[0]
            return (1.0 if latest["api_available"] else 0.0), {
                **meta,
                "latest_status": latest["status"],
                "latest_checked_at": latest["last_checked_at"].isoformat()
                if latest.get("last_checked_at")
                else None,
            }

        return None, {**meta, "reason": f"unsupported_metric:{metric_key}"}

    def _metric_cost(
        self,
        connection: Connection,
        *,
        project_id: str,
        provider: str,
        provider_account_id: Optional[str],
        since: datetime,
    ) -> tuple[Optional[float], dict[str, Any]]:
        clauses = [
            "project_id = :project_id",
            "provider = :provider",
            "created_at >= :since",
        ]
        params: dict[str, Any] = {
            "project_id": project_id,
            "provider": provider,
            "since": since,
        }
        if provider_account_id:
            clauses.append("provider_account_id = :provider_account_id")
            params["provider_account_id"] = provider_account_id
        row = connection.execute(
            text(
                f"""
                SELECT COALESCE(SUM(amount_usd_micros), 0) AS total,
                       COUNT(*) AS event_count
                FROM cost_events
                WHERE {' AND '.join(clauses)}
                """
            ),
            params,
        ).mappings().one()
        event_count = int(row["event_count"] or 0)
        meta = {"source": "cost_events", "event_count": event_count}
        if event_count == 0:
            # Fail-closed: no cost events ⇒ unknown (do not treat zero as "ok")
            return None, {**meta, "reason": "no_cost_events_in_window"}
        return float(int(row["total"] or 0)), meta

    def _health_samples(
        self,
        connection: Connection,
        *,
        project_id: str,
        provider: str,
        provider_account_id: Optional[str],
        since: datetime,
    ) -> list[dict[str, Any]]:
        clauses = [
            "project_id = :project_id",
            "provider = :provider",
            "last_checked_at >= :since",
        ]
        params: dict[str, Any] = {
            "project_id": project_id,
            "provider": provider,
            "since": since,
        }
        if provider_account_id:
            clauses.append("provider_account_id = :provider_account_id")
            params["provider_account_id"] = provider_account_id
        rows = connection.execute(
            text(
                f"""
                SELECT id, status, api_available, last_checked_at
                FROM provider_health
                WHERE {' AND '.join(clauses)}
                ORDER BY last_checked_at DESC
                LIMIT 10000
                """
            ),
            params,
        ).mappings().all()
        return [dict(r) for r in rows]

    @staticmethod
    def _compare(value: float, comparator: str, target: float) -> bool:
        if comparator == "gte":
            return value >= target
        if comparator == "lte":
            return value <= target
        if comparator == "gt":
            return value > target
        if comparator == "lt":
            return value < target
        if comparator == "eq":
            return abs(value - target) < 1e-9
        return False

    def _find_definition(
        self,
        connection: Connection,
        *,
        project_id: str,
        provider: str,
        provider_account_id: Optional[str],
        metric_key: str,
        window_seconds: int,
    ) -> Optional[dict[str, Any]]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, provider, provider_account_id, metric_key,
                       target, window_seconds, comparator, status, last_value,
                       evaluated_at, metadata, created_by, created_at, updated_at
                FROM provider_slo_definitions
                WHERE project_id = :project_id
                  AND provider = :provider
                  AND metric_key = :metric_key
                  AND window_seconds = :window_seconds
                  AND provider_account_id IS NOT DISTINCT FROM :provider_account_id
                LIMIT 1
                """
            ),
            {
                "project_id": project_id,
                "provider": provider,
                "metric_key": metric_key,
                "window_seconds": int(window_seconds),
                "provider_account_id": provider_account_id,
            },
        ).mappings().first()
        return self._serialize_definition(row) if row else None

    def _get_definition(
        self,
        connection: Connection,
        slo_id: str,
        *,
        project_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        clauses = ["id = :id"]
        params: dict[str, Any] = {"id": slo_id}
        if project_id:
            clauses.append("project_id = :project_id")
            params["project_id"] = project_id
        row = connection.execute(
            text(
                f"""
                SELECT id, project_id, provider, provider_account_id, metric_key,
                       target, window_seconds, comparator, status, last_value,
                       evaluated_at, metadata, created_by, created_at, updated_at
                FROM provider_slo_definitions
                WHERE {' AND '.join(clauses)}
                """
            ),
            params,
        ).mappings().first()
        return self._serialize_definition(row) if row else None

    @staticmethod
    def _serialize_definition(row) -> dict[str, Any]:
        payload = dict(row)
        for key in ("created_at", "updated_at", "evaluated_at"):
            payload[key] = payload[key].isoformat() if payload.get(key) else None
        payload["metadata"] = payload.get("metadata") or {}
        payload["target"] = float(payload["target"]) if payload.get("target") is not None else None
        if payload.get("last_value") is not None:
            payload["last_value"] = float(payload["last_value"])
        return payload

    @staticmethod
    def _serialize_evaluation(row) -> dict[str, Any]:
        payload = dict(row)
        for key in ("created_at", "evaluated_at"):
            payload[key] = payload[key].isoformat() if payload.get(key) else None
        payload["metadata"] = payload.get("metadata") or {}
        payload["target"] = float(payload["target"]) if payload.get("target") is not None else None
        if payload.get("last_value") is not None:
            payload["last_value"] = float(payload["last_value"])
        return payload
