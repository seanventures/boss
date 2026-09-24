from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import create_artifact, emit_event, now_utc


class AuditTrailService:
    def __init__(self, settings: Settings):
        self.settings = settings

    CHANGE_ARTIFACT_TYPES = {
        "diff_manifest",
        "review_packet",
        "test_report",
        "merge_execution",
        "deployment_request",
        "deployment_result",
        "deployment_rollout",
        "deployment_health_verification",
        "deployment_rollback",
        "self_improvement_proposal",
        "self_improvement_canary",
        "self_improvement_rollout",
        "self_improvement_rollback",
        "role_autonomy_change",
        "role_governance_review",
        "role_scope_violation",
        "infra_provision_request",
        "infra_provision_receipt",
        "infra_termination_receipt",
    }

    def list_receipts(
        self, connection: Connection, project_id: str, limit: int
    ) -> list[dict]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, dedupe_key, effect_kind, effect_scope, payload_hash,
                       status, provider_ref, created_by_job_id, data_boundary, result, error,
                       created_at, updated_at
                FROM receipts
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        return [dict(row) for row in rows]

    def list_change_journal(
        self, connection: Connection, project_id: str, limit: int
    ) -> list[dict]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, task_id, artifact_type, title, uri, content,
                       content_hash, created_by_job_id, data_boundary, created_at
                FROM artifacts
                WHERE project_id = :project_id
                  AND artifact_type IN :artifact_types
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ).bindparams(bindparam("artifact_types", expanding=True)),
            {
                "project_id": project_id,
                "artifact_types": sorted(self.CHANGE_ARTIFACT_TYPES),
                "limit": limit,
            },
        ).mappings().all()
        return [self._change_journal_item(row) for row in rows]

    def get_change_journal_item(
        self, connection: Connection, project_id: str, artifact_id: str
    ) -> dict:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, task_id, artifact_type, title, uri, content,
                       content_hash, created_by_job_id, data_boundary, created_at
                FROM artifacts
                WHERE project_id = :project_id
                  AND id = :artifact_id
                  AND artifact_type IN :artifact_types
                """
            ).bindparams(bindparam("artifact_types", expanding=True)),
            {
                "project_id": project_id,
                "artifact_id": artifact_id,
                "artifact_types": sorted(self.CHANGE_ARTIFACT_TYPES),
            },
        ).mappings().first()
        if not row:
            raise ValueError("Change artifact not found")
        item = self._change_journal_item(row)
        item["content"] = self._json_ready(row.get("content") or {})
        return item

    # project_id is the only filter an unscoped export applies, so the counts it reports are
    # exactly these. Kept beside `export` so the two cannot drift apart unnoticed.
    COUNT_TABLES = {
        "tasks": "tasks",
        "jobs": "jobs",
        "decisions": "decisions",
        "artifacts": "artifacts",
        "receipts": "receipts",
        "budget_reservations": "budget_reservations",
        "cost_receipts": "cost_receipts",
        "routing_logs": "routing_logs",
        "deadletters": "deadletters",
        "events": "events",
    }

    def summary(self, connection: Connection, project_id: str) -> dict:
        """The counts an unscoped export reports, without building the export.

        The console's audit panel displays eight integers. It got them by calling
        /v1/audit/export, which serialises every task, job, decision, artifact, receipt,
        reservation, routing log and event in the project so the caller can call len() on
        each list. Measured on the live deployment: 192 MB and 62 seconds, for eight numbers
        — and it is the whole reason the Ops area took ~25 seconds to render.

        It also emitted an `audit.export_generated` event every time, so simply opening Ops
        wrote an audit record claiming an export had been taken. Counting does not do that;
        a summary is a read, not an export, and the audit trail should not say otherwise.
        """
        project = connection.execute(
            text("SELECT id FROM projects WHERE id = :project_id"),
            {"project_id": project_id},
        ).first()
        if not project:
            raise ValueError("Unknown project_id")
        counts = {
            key: int(
                connection.execute(
                    text(f"SELECT COUNT(*) FROM {table} WHERE project_id = :project_id"),
                    {"project_id": project_id},
                ).scalar()
                or 0
            )
            for key, table in self.COUNT_TABLES.items()
        }
        return {
            "project_id": project_id,
            "generated_at": now_utc().isoformat(),
            "counts": counts,
        }

    def export(
        self,
        connection: Connection,
        project_id: str,
        *,
        actor: Optional[str] = None,
        task_id: Optional[str] = None,
        decision_id: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> dict:
        project = connection.execute(
            text(
                """
                SELECT p.id AS project_id, p.name AS project_name,
                       w.name AS workspace_name, o.name AS org_name
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                JOIN orgs o ON o.id = w.org_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if not project:
            raise ValueError("Unknown project_id")

        governance = self._audit_governance(connection, project_id)
        if governance.get("export_allowed") is False:
            reason = "Enterprise audit exports are disabled by the active audit controls."
            if actor:
                emit_event(
                    connection,
                    project_id,
                    "policy.denied",
                    actor,
                    "audit_export",
                    project_id,
                    {
                        "permission": "audit_export",
                        "reason": reason,
                        "governance": governance,
                    },
                )
            raise PermissionError(reason)

        scoped = any([task_id, decision_id, job_id])
        task_ids: set[str] = set()
        decision_ids: set[str] = set()
        job_ids: set[str] = set()

        if task_id:
            task_ids.add(task_id)
        if decision_id:
            decision = connection.execute(
                text(
                    """
                    SELECT id, task_id
                    FROM decisions
                    WHERE id = :decision_id AND project_id = :project_id
                    """
                ),
                {"decision_id": decision_id, "project_id": project_id},
            ).mappings().first()
            if decision:
                decision_ids.add(decision["id"])
                if decision["task_id"]:
                    task_ids.add(decision["task_id"])
        if job_id:
            job = connection.execute(
                text(
                    """
                    SELECT id, task_id
                    FROM jobs
                    WHERE id = :job_id AND project_id = :project_id
                    """
                ),
                {"job_id": job_id, "project_id": project_id},
            ).mappings().first()
            if job:
                job_ids.add(job["id"])
                if job["task_id"]:
                    task_ids.add(job["task_id"])

        tasks = self._fetch_tasks(connection, project_id, task_ids if scoped else None)
        task_ids.update(row["id"] for row in tasks)

        jobs = self._fetch_jobs(
            connection,
            project_id,
            task_ids=task_ids if scoped else None,
            job_ids=job_ids if scoped else None,
        )
        job_ids.update(row["id"] for row in jobs)

        decisions = self._fetch_decisions(
            connection,
            project_id,
            task_ids=task_ids if scoped else None,
            decision_ids=decision_ids if scoped else None,
        )
        decision_ids.update(row["id"] for row in decisions)

        artifacts = self._fetch_artifacts(
            connection,
            project_id,
            task_ids=task_ids if scoped else None,
            job_ids=job_ids if scoped else None,
        )
        artifact_ids = {row["id"] for row in artifacts}

        reservations = self._fetch_budget_reservations(
            connection,
            project_id,
            job_ids=job_ids if scoped else None,
        )
        reservation_ids = {row["id"] for row in reservations}

        receipts = self._fetch_receipts(
            connection,
            project_id,
            job_ids=job_ids if scoped else None,
        )
        receipt_ids = {row["id"] for row in receipts}

        cost_receipts = self._fetch_cost_receipts(
            connection,
            project_id,
            reservation_ids=reservation_ids if scoped and reservation_ids else None,
            receipt_ids=receipt_ids if scoped and receipt_ids else None,
        )
        reservation_ids.update(
            row["reservation_id"]
            for row in cost_receipts
            if row.get("reservation_id")
        )
        receipt_ids.update(
            row["receipt_id"] for row in cost_receipts if row.get("receipt_id")
        )
        if scoped:
            reservations = self._fetch_budget_reservations(
                connection,
                project_id,
                reservation_ids=reservation_ids,
                job_ids=job_ids,
            )
            receipts = self._fetch_receipts(
                connection,
                project_id,
                receipt_ids=receipt_ids,
                job_ids=job_ids,
            )

        routing_log_ids = self._extract_routing_log_ids(artifacts, receipts, [])
        deadletters = self._fetch_deadletters(
            connection,
            project_id,
            job_ids=job_ids if scoped else None,
        )
        deadletter_ids = {row["id"] for row in deadletters}

        entity_ids = {
            *task_ids,
            *job_ids,
            *decision_ids,
            *artifact_ids,
            *receipt_ids,
            *reservation_ids,
            *deadletter_ids,
        }
        events = self._fetch_events(
            connection,
            project_id,
            entity_ids=entity_ids if scoped else None,
        )
        routing_log_ids.update(self._extract_routing_log_ids(artifacts, receipts, events))
        routing_logs = self._fetch_routing_logs(
            connection,
            project_id,
            routing_log_ids=routing_log_ids if scoped else None,
        )
        generated_at = now_utc().isoformat()

        chain = {
            "task_ids": sorted(task_ids),
            "job_ids": sorted(job_ids),
            "decision_ids": sorted(decision_ids),
            "artifact_ids": sorted(artifact_ids),
            "receipt_ids": sorted(receipt_ids),
            "budget_reservation_ids": sorted(reservation_ids),
            "routing_log_ids": sorted(routing_log_ids),
            "deadletter_ids": sorted(deadletter_ids),
        }

        export_payload = {
            "project_id": project["project_id"],
            "project_name": project["project_name"],
            "workspace_name": project["workspace_name"],
            "org_name": project["org_name"],
            "generated_at": generated_at,
            "governance": governance,
            "scope": {
                "task_id": task_id,
                "decision_id": decision_id,
                "job_id": job_id,
            },
            "counts": {
                "tasks": len(tasks),
                "jobs": len(jobs),
                "decisions": len(decisions),
                "artifacts": len(artifacts),
                "receipts": len(receipts),
                "budget_reservations": len(reservations),
                "cost_receipts": len(cost_receipts),
                "routing_logs": len(routing_logs),
                "deadletters": len(deadletters),
                "events": len(events),
            },
            "chain": chain,
            "tasks": tasks,
            "jobs": jobs,
            "decisions": decisions,
            "artifacts": artifacts,
            "receipts": receipts,
            "budget_reservations": reservations,
            "cost_receipts": cost_receipts,
            "routing_logs": routing_logs,
            "deadletters": deadletters,
            "events": events,
        }
        if actor:
            emit_event(
                connection,
                project_id,
                "audit.export_generated",
                actor,
                "audit_export",
                project_id,
                {
                    "scope": {
                        "task_id": task_id,
                        "decision_id": decision_id,
                        "job_id": job_id,
                    },
                    "counts": export_payload["counts"],
                    "governance": governance,
                },
            )
        return export_payload

    def _audit_governance(
        self, connection: Connection, project_id: str
    ) -> dict[str, Any]:
        return {
            "edition_scope": "community",
            "cluster_id": None,
            "export_allowed": True,
            "audit_exports_enabled": True,
            "retention_policy_required": False,
            "retention_days": None,
            "compliance_evidence_workflows": False,
            "audit_export_mode": "on_demand",
            "blocking_gaps": [],
        }

    def create_replay_bundle(
        self,
        connection: Connection,
        project_id: str,
        *,
        actor: str,
        task_id: Optional[str] = None,
        decision_id: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> dict:
        export = self.export(
            connection,
            project_id,
            task_id=task_id,
            decision_id=decision_id,
            job_id=job_id,
        )
        bundle_content = self._json_ready(export)
        title_scope = task_id or decision_id or job_id or project_id
        artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            task_id,
            "replay_bundle",
            f"Replay bundle for {title_scope}",
            bundle_content,
        )
        emit_event(
            connection,
            project_id,
            "audit.replay_bundle_created",
            actor,
            "artifact",
            artifact_id,
            export["scope"],
        )
        return {
            "status": "created",
            "artifact_id": artifact_id,
            "project_id": project_id,
            "artifact_type": "replay_bundle",
            "created_at": now_utc().isoformat(),
            "scope": export["scope"],
            "counts": export["counts"],
        }

    def create_compliance_evidence_bundle(
        self,
        connection: Connection,
        project_id: str,
        *,
        actor: str,
        period_start: Optional[Any] = None,
        period_end: Optional[Any] = None,
    ) -> dict:
        project = connection.execute(
            text(
                """
                SELECT p.id AS project_id, p.name AS project_name,
                       w.name AS workspace_name, o.name AS org_name
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                JOIN orgs o ON o.id = w.org_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if not project:
            raise ValueError("Unknown project_id")

        members = self._fetch_project_members(connection, project_id)
        roles = self._fetch_role_governance(connection, project_id)
        policy_records = self._fetch_policy_records(connection, project_id)
        break_glass_grants = self._fetch_break_glass_grants(connection, project_id)
        decisions = self._fetch_decisions(connection, project_id)
        receipts = self._fetch_receipts(connection, project_id)
        budget_reservations = self._fetch_budget_reservations(connection, project_id)
        cost_receipts = self._fetch_cost_receipts(connection, project_id)
        deadletters = self._fetch_deadletters(connection, project_id)
        compliance_events = self._fetch_compliance_events(
            connection,
            project_id,
            period_start=period_start,
            period_end=period_end,
        )
        generated_at = now_utc().isoformat()
        period = {
            "period_start": period_start.isoformat()
            if hasattr(period_start, "isoformat")
            else period_start,
            "period_end": period_end.isoformat()
            if hasattr(period_end, "isoformat")
            else period_end,
        }
        counts = {
            "project_members": len(members),
            "roles": len(roles),
            "policy_records": len(policy_records),
            "break_glass_grants": len(break_glass_grants),
            "decisions": len(decisions),
            "receipts": len(receipts),
            "budget_reservations": len(budget_reservations),
            "cost_receipts": len(cost_receipts),
            "deadletters": len(deadletters),
            "compliance_events": len(compliance_events),
        }
        content = self._json_ready(
            {
                "bundle_type": "compliance_evidence",
                "project": dict(project),
                "generated_at": generated_at,
                "generated_by": actor,
                "period": period,
                "controls": {
                    "external_effects": "Receipts are required for external/material effects.",
                    "spend": "Budget reservations are required before spendy work.",
                    "worker_execution": "Workers execute through leased jobs only.",
                    "visibility": "Failures must become events, decisions, or deadletters.",
                    "identity": "Project members and optional API-key principals ground actor identity.",
                    "break_glass": "Emergency access requires identity, reason, scope, TTL, event log, and postmortem task.",
                },
                "counts": counts,
                "members": members,
                "roles": roles,
                "policy_records": policy_records,
                "break_glass_grants": break_glass_grants,
                "decisions": decisions,
                "receipts": receipts,
                "budget_reservations": budget_reservations,
                "cost_receipts": cost_receipts,
                "deadletters": deadletters,
                "compliance_events": compliance_events,
            }
        )
        artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            None,
            "compliance_evidence_bundle",
            f"Compliance evidence bundle for {project_id}",
            content,
        )
        emit_event(
            connection,
            project_id,
            "compliance.evidence_bundle_created",
            actor,
            "artifact",
            artifact_id,
            {"period": period, "counts": counts},
        )
        return {
            "status": "created",
            "artifact_id": artifact_id,
            "project_id": project_id,
            "artifact_type": "compliance_evidence_bundle",
            "created_at": generated_at,
            "period": period,
            "counts": counts,
        }

    def _fetch_project_members(
        self,
        connection: Connection,
        project_id: str,
    ) -> list[dict]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, member_id, display_name, member_type,
                       role_keys, permissions, status, created_by, created_at,
                       updated_at
                FROM project_members
                WHERE project_id = :project_id
                ORDER BY member_type ASC, member_id ASC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [dict(row) for row in rows]

    def _fetch_role_governance(
        self,
        connection: Connection,
        project_id: str,
    ) -> list[dict]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, role_key, display_name, autonomy_level,
                       trust_score, successful_runs, policy_violation_count,
                       duplicate_effect_attempt_count, hidden_failure_count,
                       low_quality_output_count, scope_violation_count,
                       last_reviewed_at, last_violation_at, source_module_id,
                       source_module_version, created_at
                FROM roles
                WHERE project_id = :project_id
                ORDER BY role_key ASC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [dict(row) for row in rows]

    def _fetch_policy_records(
        self,
        connection: Connection,
        project_id: str,
    ) -> list[dict]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, policy_key, version, title, status, body,
                       created_by, created_at, updated_at
                FROM policy_records
                WHERE project_id = :project_id
                ORDER BY policy_key ASC, version DESC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [dict(row) for row in rows]

    def _fetch_break_glass_grants(
        self,
        connection: Connection,
        project_id: str,
    ) -> list[dict]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, target_member_id, requested_by, reason,
                       scope, granted_permissions, status, expires_at,
                       postmortem_task_id, revoked_by, revoked_reason,
                       revoked_at, created_at, updated_at
                FROM break_glass_grants
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [dict(row) for row in rows]

    def _fetch_compliance_events(
        self,
        connection: Connection,
        project_id: str,
        *,
        period_start: Optional[Any] = None,
        period_end: Optional[Any] = None,
    ) -> list[dict]:
        params: dict[str, object] = {"project_id": project_id}
        time_clause = ""
        if period_start:
            time_clause += " AND created_at >= :period_start"
            params["period_start"] = period_start
        if period_end:
            time_clause += " AND created_at <= :period_end"
            params["period_end"] = period_end
        rows = connection.execute(
            text(
                f"""
                SELECT id, event_type, actor, entity_type, entity_id, data, created_at
                FROM events
                WHERE project_id = :project_id
                  AND (
                    event_type LIKE 'policy.%'
                    OR event_type LIKE 'break_glass.%'
                    OR event_type LIKE 'budget.%'
                    OR event_type LIKE 'decision.%'
                    OR event_type LIKE 'receipt.%'
                    OR event_type LIKE 'deadletter.%'
                  )
                  {time_clause}
                ORDER BY created_at ASC
                """
            ),
            params,
        ).mappings().all()
        return [dict(row) for row in rows]

    def _fetch_tasks(
        self, connection: Connection, project_id: str, task_ids: Optional[set[str]]
    ) -> list[dict]:
        if task_ids is not None and not task_ids:
            return []
        if task_ids is not None:
            statement = text(
                """
                SELECT id, project_id, title, description, status, priority, risk_tier,
                       owner_role_id, parent_task_id, dependency_task_ids, due_at,
                       data_boundary, dedupe_key, created_at, updated_at
                FROM tasks
                WHERE project_id = :project_id AND id IN :task_ids
                ORDER BY created_at ASC
                """
            ).bindparams(bindparam("task_ids", expanding=True))
            params = {"project_id": project_id, "task_ids": sorted(task_ids)}
        else:
            statement = text(
                """
                SELECT id, project_id, title, description, status, priority, risk_tier,
                       owner_role_id, parent_task_id, dependency_task_ids, due_at,
                       data_boundary, dedupe_key, created_at, updated_at
                FROM tasks
                WHERE project_id = :project_id
                ORDER BY created_at ASC
                """
            )
            params = {"project_id": project_id}
        rows = connection.execute(statement, params).mappings().all()
        return [dict(row) for row in rows]

    def _fetch_jobs(
        self,
        connection: Connection,
        project_id: str,
        *,
        task_ids: Optional[set[str]] = None,
        job_ids: Optional[set[str]] = None,
    ) -> list[dict]:
        clause = "project_id = :project_id"
        params: dict[str, object] = {"project_id": project_id}
        bind_params = []
        if task_ids is not None and not task_ids and job_ids is not None and not job_ids:
            return []
        if task_ids is not None or job_ids is not None:
            scoped_filters = []
            if task_ids:
                scoped_filters.append("task_id IN :task_ids")
                params["task_ids"] = sorted(task_ids)
                bind_params.append(bindparam("task_ids", expanding=True))
            if job_ids:
                scoped_filters.append("id IN :job_ids")
                params["job_ids"] = sorted(job_ids)
                bind_params.append(bindparam("job_ids", expanding=True))
            if not scoped_filters:
                return []
            clause = f"{clause} AND ({' OR '.join(scoped_filters)})"
        statement = text(
            f"""
            SELECT id, project_id, task_id, kind, status, data_boundary, priority,
                   max_attempts, attempt, run_after
            FROM jobs
            WHERE {clause}
            ORDER BY created_at ASC
            """
        )
        if bind_params:
            statement = statement.bindparams(*bind_params)
        rows = connection.execute(statement, params).mappings().all()
        return [dict(row) for row in rows]

    def _fetch_decisions(
        self,
        connection: Connection,
        project_id: str,
        *,
        task_ids: Optional[set[str]] = None,
        decision_ids: Optional[set[str]] = None,
    ) -> list[dict]:
        clause = "project_id = :project_id"
        params: dict[str, object] = {"project_id": project_id}
        bind_params = []
        if task_ids is not None and not task_ids and decision_ids is not None and not decision_ids:
            return []
        if task_ids is not None or decision_ids is not None:
            scoped_filters = []
            if task_ids:
                scoped_filters.append("task_id IN :task_ids")
                params["task_ids"] = sorted(task_ids)
                bind_params.append(bindparam("task_ids", expanding=True))
            if decision_ids:
                scoped_filters.append("id IN :decision_ids")
                params["decision_ids"] = sorted(decision_ids)
                bind_params.append(bindparam("decision_ids", expanding=True))
            if not scoped_filters:
                return []
            clause = f"{clause} AND ({' OR '.join(scoped_filters)})"
        statement = text(
            f"""
            SELECT id, task_id, question, status, risk_tier, default_action,
                   recommendation, options, cost_summary, deadline_at, approved_by,
                   approved_at, delegated_to, delegated_by, delegated_at,
                   revision_requested_by, revision_requested_at, executed_by,
                   executed_at, resolution_note, external_ref, created_at, updated_at
            FROM decisions
            WHERE {clause}
            ORDER BY created_at ASC
            """
        )
        if bind_params:
            statement = statement.bindparams(*bind_params)
        rows = connection.execute(statement, params).mappings().all()
        return [dict(row) for row in rows]

    def _fetch_artifacts(
        self,
        connection: Connection,
        project_id: str,
        *,
        task_ids: Optional[set[str]] = None,
        job_ids: Optional[set[str]] = None,
    ) -> list[dict]:
        clause = "project_id = :project_id"
        params: dict[str, object] = {"project_id": project_id}
        bind_params = []
        if task_ids is not None and not task_ids and job_ids is not None and not job_ids:
            return []
        if task_ids is not None or job_ids is not None:
            scoped_filters = []
            if task_ids:
                scoped_filters.append("task_id IN :task_ids")
                params["task_ids"] = sorted(task_ids)
                bind_params.append(bindparam("task_ids", expanding=True))
            if job_ids:
                scoped_filters.append("created_by_job_id IN :job_ids")
                params["job_ids"] = sorted(job_ids)
                bind_params.append(bindparam("job_ids", expanding=True))
            if not scoped_filters:
                return []
            clause = f"{clause} AND ({' OR '.join(scoped_filters)})"
        statement = text(
            f"""
            SELECT id, project_id, task_id, artifact_type, title, uri, content,
                   content_hash, created_by_job_id, data_boundary, created_at
            FROM artifacts
            WHERE {clause}
            ORDER BY created_at ASC
            """
        )
        if bind_params:
            statement = statement.bindparams(*bind_params)
        rows = connection.execute(statement, params).mappings().all()
        return [dict(row) for row in rows]

    def _fetch_budget_reservations(
        self,
        connection: Connection,
        project_id: str,
        *,
        reservation_ids: Optional[set[str]] = None,
        job_ids: Optional[set[str]] = None,
    ) -> list[dict]:
        clause = "project_id = :project_id"
        params: dict[str, object] = {"project_id": project_id}
        bind_params = []
        if (
            reservation_ids is not None
            and not reservation_ids
            and job_ids is not None
            and not job_ids
        ):
            return []
        if reservation_ids is not None or job_ids is not None:
            scoped_filters = []
            if reservation_ids:
                scoped_filters.append("id IN :reservation_ids")
                params["reservation_ids"] = sorted(reservation_ids)
                bind_params.append(bindparam("reservation_ids", expanding=True))
            if job_ids:
                scoped_filters.append("job_id IN :job_ids")
                params["job_ids"] = sorted(job_ids)
                bind_params.append(bindparam("job_ids", expanding=True))
            if not scoped_filters:
                return []
            clause = f"{clause} AND ({' OR '.join(scoped_filters)})"
        statement = text(
            f"""
            SELECT id, project_id, job_id, category, amount_usd_micros, status,
                   reason, expires_at, created_at
            FROM budget_reservations
            WHERE {clause}
            ORDER BY created_at ASC
            """
        )
        if bind_params:
            statement = statement.bindparams(*bind_params)
        rows = connection.execute(statement, params).mappings().all()
        return [dict(row) for row in rows]

    def _fetch_receipts(
        self,
        connection: Connection,
        project_id: str,
        *,
        receipt_ids: Optional[set[str]] = None,
        job_ids: Optional[set[str]] = None,
    ) -> list[dict]:
        clause = "project_id = :project_id"
        params: dict[str, object] = {"project_id": project_id}
        bind_params = []
        if receipt_ids is not None and not receipt_ids and job_ids is not None and not job_ids:
            return []
        if receipt_ids is not None or job_ids is not None:
            scoped_filters = []
            if receipt_ids:
                scoped_filters.append("id IN :receipt_ids")
                params["receipt_ids"] = sorted(receipt_ids)
                bind_params.append(bindparam("receipt_ids", expanding=True))
            if job_ids:
                scoped_filters.append("created_by_job_id IN :job_ids")
                params["job_ids"] = sorted(job_ids)
                bind_params.append(bindparam("job_ids", expanding=True))
            if not scoped_filters:
                return []
            clause = f"{clause} AND ({' OR '.join(scoped_filters)})"
        statement = text(
            f"""
            SELECT id, project_id, dedupe_key, effect_kind, effect_scope, payload_hash,
                   status, provider_ref, created_by_job_id, data_boundary, result, error,
                   created_at, updated_at
            FROM receipts
            WHERE {clause}
            ORDER BY created_at ASC
            """
        )
        if bind_params:
            statement = statement.bindparams(*bind_params)
        rows = connection.execute(statement, params).mappings().all()
        return [dict(row) for row in rows]

    def _fetch_cost_receipts(
        self,
        connection: Connection,
        project_id: str,
        *,
        reservation_ids: Optional[set[str]] = None,
        receipt_ids: Optional[set[str]] = None,
    ) -> list[dict]:
        clause = "project_id = :project_id"
        params: dict[str, object] = {"project_id": project_id}
        bind_params = []
        if (
            reservation_ids is not None
            and not reservation_ids
            and receipt_ids is not None
            and not receipt_ids
        ):
            return []
        if reservation_ids is not None or receipt_ids is not None:
            scoped_filters = []
            if reservation_ids:
                scoped_filters.append("reservation_id IN :reservation_ids")
                params["reservation_ids"] = sorted(reservation_ids)
                bind_params.append(bindparam("reservation_ids", expanding=True))
            if receipt_ids:
                scoped_filters.append("receipt_id IN :receipt_ids")
                params["receipt_ids"] = sorted(receipt_ids)
                bind_params.append(bindparam("receipt_ids", expanding=True))
            if not scoped_filters:
                return []
            clause = f"{clause} AND ({' OR '.join(scoped_filters)})"
        statement = text(
            f"""
            SELECT id, project_id, reservation_id, receipt_id, category,
                   skill_or_model_id, estimated_usd_micros, actual_usd_micros,
                   status, created_at
            FROM cost_receipts
            WHERE {clause}
            ORDER BY created_at ASC
            """
        )
        if bind_params:
            statement = statement.bindparams(*bind_params)
        rows = connection.execute(statement, params).mappings().all()
        return [dict(row) for row in rows]

    def _fetch_deadletters(
        self, connection: Connection, project_id: str, *, job_ids: Optional[set[str]] = None
    ) -> list[dict]:
        clause = "project_id = :project_id"
        params: dict[str, object] = {"project_id": project_id}
        if job_ids is not None and not job_ids:
            return []
        if job_ids:
            clause = f"{clause} AND source_kind = 'job' AND source_id IN :job_ids"
            params["job_ids"] = sorted(job_ids)
            statement = text(
                f"""
                SELECT id, project_id, source_kind, source_id, reason, payload, created_at
                FROM deadletters
                WHERE {clause}
                ORDER BY created_at ASC
                """
            ).bindparams(bindparam("job_ids", expanding=True))
        else:
            statement = text(
                f"""
                SELECT id, project_id, source_kind, source_id, reason, payload, created_at
                FROM deadletters
                WHERE {clause}
                ORDER BY created_at ASC
                """
            )
        rows = connection.execute(statement, params).mappings().all()
        return [dict(row) for row in rows]

    def _change_journal_item(self, row: dict) -> dict:
        content = row.get("content") or {}
        if not isinstance(content, dict):
            content = {}
        files_changed = self._string_list(
            content.get("files_changed")
            or content.get("files")
            or content.get("changed_files")
            or []
        )
        summary = self._first_text(
            content.get("summary"),
            content.get("description"),
            content.get("status_note"),
            content.get("title"),
        )
        diff_summary = self._first_text(
            content.get("diff_summary"),
            content.get("ci_validation_summary"),
            content.get("validation_summary"),
            content.get("risk_hotspots"),
        )
        rollback_plan = self._first_text(
            content.get("rollback_plan"),
            content.get("rollback"),
        )
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "task_id": row.get("task_id"),
            "artifact_type": row["artifact_type"],
            "change_kind": self._change_kind(row["artifact_type"]),
            "title": row["title"],
            "summary": summary,
            "diff_summary": diff_summary,
            "files_changed": files_changed,
            "rollback_plan": rollback_plan,
            "created_by_job_id": row.get("created_by_job_id"),
            "data_boundary": row.get("data_boundary") or "internal",
            "created_at": row["created_at"].isoformat(),
        }

    def _change_kind(self, artifact_type: str) -> str:
        if artifact_type.startswith("self_improvement"):
            return "self_improvement"
        if artifact_type.startswith("deployment"):
            return "deployment"
        if artifact_type.startswith("infra"):
            return "infrastructure"
        if artifact_type.startswith("role"):
            return "role_governance"
        if artifact_type in {"diff_manifest", "review_packet", "test_report"}:
            return "software_factory"
        return "change"

    def _first_text(self, *values: Any) -> Optional[str]:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, list):
                normalized = [str(item).strip() for item in value if str(item).strip()]
                if normalized:
                    return "; ".join(normalized[:5])
        return None

    def _string_list(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def _fetch_events(
        self, connection: Connection, project_id: str, *, entity_ids: Optional[set[str]] = None
    ) -> list[dict]:
        clause = "project_id = :project_id"
        params: dict[str, object] = {"project_id": project_id}
        if entity_ids is not None and not entity_ids:
            return []
        if entity_ids:
            clause = f"{clause} AND entity_id IN :entity_ids"
            params["entity_ids"] = sorted(entity_ids)
            statement = text(
                f"""
                SELECT id, event_type, actor, entity_type, entity_id, data, created_at
                FROM events
                WHERE {clause}
                ORDER BY created_at ASC
                """
            ).bindparams(bindparam("entity_ids", expanding=True))
        else:
            statement = text(
                f"""
                SELECT id, event_type, actor, entity_type, entity_id, data, created_at
                FROM events
                WHERE {clause}
                ORDER BY created_at ASC
                """
            )
        rows = connection.execute(statement, params).mappings().all()
        return [dict(row) for row in rows]

    def _fetch_routing_logs(
        self,
        connection: Connection,
        project_id: str,
        *,
        routing_log_ids: Optional[set[str]] = None,
    ) -> list[dict]:
        clause = "project_id = :project_id"
        params: dict[str, object] = {"project_id": project_id}
        if routing_log_ids is not None and not routing_log_ids:
            return []
        if routing_log_ids:
            clause = f"{clause} AND id IN :routing_log_ids"
            params["routing_log_ids"] = sorted(routing_log_ids)
            statement = text(
                f"""
                SELECT id, project_id, skill_id, task_kind, workload_class, risk_tier,
                       data_boundary, budget_state, selected_source_id, selected_provider,
                       selected_model_id, denial_reason, rejected_sources, decision,
                       created_at
                FROM routing_logs
                WHERE {clause}
                ORDER BY created_at ASC
                """
            ).bindparams(bindparam("routing_log_ids", expanding=True))
        else:
            statement = text(
                f"""
                SELECT id, project_id, skill_id, task_kind, workload_class, risk_tier,
                       data_boundary, budget_state, selected_source_id, selected_provider,
                       selected_model_id, denial_reason, rejected_sources, decision,
                       created_at
                FROM routing_logs
                WHERE {clause}
                ORDER BY created_at ASC
                """
            )
        rows = connection.execute(statement, params).mappings().all()
        return [dict(row) for row in rows]

    def _extract_routing_log_ids(
        self,
        artifacts: list[dict],
        receipts: list[dict],
        events: list[dict],
    ) -> set[str]:
        routing_log_ids: set[str] = set()
        for artifact in artifacts:
            content = artifact.get("content") or {}
            routing_log_id = content.get("routing_log_id")
            if isinstance(routing_log_id, str) and routing_log_id:
                routing_log_ids.add(routing_log_id)
        for receipt in receipts:
            effect_scope = receipt.get("effect_scope") or {}
            routing_log_id = effect_scope.get("routing_log_id")
            if isinstance(routing_log_id, str) and routing_log_id:
                routing_log_ids.add(routing_log_id)
        for event in events:
            data = event.get("data") or {}
            for key in ("routing_log_id", "route_decision_log_id"):
                routing_log_id = data.get(key)
                if isinstance(routing_log_id, str) and routing_log_id:
                    routing_log_ids.add(routing_log_id)
        return routing_log_ids

    def _json_ready(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self._json_ready(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._json_ready(item) for item in value]
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return value
