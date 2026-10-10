"""Governed model capacity leases and invocation settlement.

The gateway brokers *authority*, not credentials.  A worker must already own a
durable BOSS job lease before it can acquire model capacity.  Public APIs return
only opaque model-lease tokens; provider material is resolved solely inside a
policy-checked adapter process.
"""
from __future__ import annotations

import hashlib
import json
import math
from secrets import compare_digest, token_urlsafe
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .budget import BudgetGovernor
from .core import create_blocking_decision, emit_event, make_id, now_utc
from .entitlements import EntitlementBrokerService
from .included_spend import source_is_included, split
from .jobs import JobLeaseService
from .receipts import ReceiptService
from .secret_backends import resolve_secret_ref
from .secrets_service import put_secret


class ModelGatewayError(RuntimeError):
    pass


class ModelCapacityDeniedError(ModelGatewayError):
    pass


class ModelGatewayLeaseError(ModelGatewayError):
    pass


class ModelGatewayInvocationError(ModelGatewayError):
    pass


class ModelCredentialPolicyError(ModelGatewayError):
    pass


class ModelGatewayService:
    PAID_SOURCE_KINDS = {
        "official_api",
        "enterprise_model_gateway",
        "managed_cloud_model_gateway",
    }
    TERMINAL_LEASE_STATES = {"completed", "failed", "released", "expired"}
    _SENSITIVE_METADATA_TERMS = {
        "prompt",
        "content",
        "message",
        "input",
        "output",
        "password",
        "secret",
        "token",
        "api_key",
    }
    _SAFE_METADATA_KEYS = {
        "stage",
        "trace_id",
        "span_id",
        "workflow_run_id",
        "evaluation_id",
        "operation",
        "attempt",
        "region",
    }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.entitlements = EntitlementBrokerService(settings)
        self.jobs = JobLeaseService(settings)
        self.budget = BudgetGovernor(settings)
        self.receipts = ReceiptService()

    @staticmethod
    def _hash_token(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _iso(value: Any) -> Optional[str]:
        return value.isoformat() if value is not None else None

    def _safe_metadata(self, payload: Any, path: str = "metadata") -> None:
        if isinstance(payload, dict):
            for key, value in payload.items():
                normalized = str(key).strip().lower()
                if any(term in normalized for term in self._SENSITIVE_METADATA_TERMS):
                    raise ValueError(
                        f"{path}.{key} is not allowed; store only non-secret observability metadata."
                    )
                if normalized not in self._SAFE_METADATA_KEYS:
                    raise ValueError(f"{path}.{key} is not an approved metadata field.")
                self._safe_metadata(value, f"{path}.{key}")
        elif isinstance(payload, list):
            raise ValueError(f"{path} lists are not allowed.")
        elif isinstance(payload, str) and len(payload) > 256:
            raise ValueError(f"{path} values must be at most 256 characters.")

    def _project_scope(self, connection: Connection, project_id: str) -> dict[str, str]:
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
            raise ModelGatewayError("Project not found.")
        return {"workspace_id": str(row["workspace_id"]), "org_id": str(row["org_id"])}

    def _source_for_update(
        self, connection: Connection, project_id: str, source_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, provider, source_id, source_kind,
                       entitlement_kind, owner_type, owner_id,
                       multi_tenant_allowed, auth_method, credential_ref,
                       credential_secret_ref_id, connection_ref, connection_status,
                       cost_mode, limit_shape, observed_capacity, runtime_supported
                FROM entitlement_sources
                WHERE project_id = :project_id AND source_id = :source_id
                FOR UPDATE
                """
            ),
            {"project_id": project_id, "source_id": source_id},
        ).mappings().first()
        if not row:
            raise ModelGatewayError("Selected entitlement source no longer exists.")
        return dict(row)

    @staticmethod
    def _is_owner_scoped(source: dict[str, Any]) -> bool:
        return source["entitlement_kind"] in {"subscription", "licensed_seat"} or source[
            "source_kind"
        ] in {"official_cli", "official_coding_agent"}

    def consumer_for(self, connection: Connection, project_id: str,
                     source_id: Optional[str], actor: str) -> str:
        """Whom a pinned call books capacity for; see typed_decision_spend.consumer_for."""
        from .typed_decision_spend import consumer_for

        return consumer_for(connection, project_id, source_id, actor, self.settings)

    def _assert_owner_scope(
        self, source: dict[str, Any], project_id: str, consumer_owner_id: Optional[str]
    ) -> None:
        if not self._is_owner_scoped(source):
            return
        project_owned = source["owner_type"] == "project" and source["owner_id"] == project_id
        if not project_owned and consumer_owner_id != source["owner_id"]:
            raise ModelCapacityDeniedError(
                "Personal subscription and coding-agent capacity is owner scoped and cannot be pooled."
            )
        if source.get("multi_tenant_allowed"):
            raise ModelCapacityDeniedError(
                "Owner-scoped subscription capacity cannot be marked multi-tenant."
            )

    def bind_credential_secret_ref(
        self,
        connection: Connection,
        project_id: str,
        source_id: str,
        secret_ref_id: str,
        *,
        actor: str,
    ) -> dict[str, Any]:
        source = self._source_for_update(connection, project_id, source_id)
        if self.entitlements._requires_official_oauth(source):
            raise ModelCredentialPolicyError(
                "Subscription sources must use their official connection-session flow; "
                "OAuth material cannot be uploaded or pooled as an API credential."
            )
        secret = connection.execute(
            text(
                """
                SELECT id, provider, status, secret_kind
                FROM secret_refs
                WHERE id = :secret_ref_id AND project_id = :project_id
                """
            ),
            {"secret_ref_id": secret_ref_id, "project_id": project_id},
        ).mappings().first()
        if not secret or secret["status"] not in {"active", "rotating", "rotation_due"}:
            raise ModelCredentialPolicyError("An active project SecretRef is required.")
        if secret["secret_kind"] not in {"api_token", "access_key", "client_secret"}:
            raise ModelCredentialPolicyError("SecretRef kind is not valid for a model credential.")
        if secret["provider"] and secret["provider"] != source["provider"]:
            raise ModelCredentialPolicyError("SecretRef provider does not match the source provider.")
        observed = {
            **(source.get("observed_capacity") or {}),
            "credential_present": True,
            "runtime_supported": True,
            "capacity_state": (source.get("observed_capacity") or {}).get(
                "capacity_state", "available"
            ),
        }
        connection.execute(
            text(
                """
                UPDATE entitlement_sources
                SET credential_secret_ref_id = :secret_ref_id,
                    connection_ref = :connection_ref,
                    connection_status = 'connected',
                    runtime_supported = true,
                    status = 'active',
                    observed_capacity = CAST(:observed AS JSONB),
                    updated_at = now()
                WHERE id = :source_pk
                """
            ),
            {
                "secret_ref_id": secret_ref_id,
                "connection_ref": f"secretref:{secret_ref_id}",
                "observed": json.dumps(observed),
                "source_pk": source["id"],
            },
        )
        emit_event(
            connection,
            project_id,
            "model_gateway.credential_bound",
            actor,
            "entitlement_source",
            source["id"],
            {"source_id": source_id, "secret_ref_id": secret_ref_id},
        )
        return self.entitlements.get_source(connection, project_id, source_id)

    def upload_credential(
        self,
        connection: Connection,
        project_id: str,
        source_id: str,
        *,
        value: str,
        backend: str,
        actor: str,
    ) -> dict[str, Any]:
        source = self._source_for_update(connection, project_id, source_id)
        if self.entitlements._requires_official_oauth(source):
            raise ModelCredentialPolicyError(
                "Subscription sources must be authenticated through an official connection session."
            )
        stored = put_secret(
            connection,
            self.settings,
            project_id=project_id,
            name=f"model_{source_id}_credential",
            value=value,
            secret_kind="api_token",
            backend=backend,
            actor=actor,
        )
        connection.execute(
            text("UPDATE secret_refs SET provider=:provider WHERE id=:id"),
            {"provider": source["provider"], "id": stored["secret_ref_id"]},
        )
        bound = self.bind_credential_secret_ref(
            connection, project_id, source_id, stored["secret_ref_id"], actor=actor
        )
        return {"source": bound, "secret": stored}

    def _serialize_lease(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row)
        payload.pop("lease_token_hash", None)
        if payload.get("status") == "active" and payload.get("expires_at") <= now_utc():
            payload["status"] = "expired"
        for key in ("acquired_at", "heartbeat_at", "expires_at", "completed_at"):
            payload[key] = self._iso(payload.get(key))
        payload["estimated_cost_usd_micros"] = int(
            payload.get("estimated_cost_usd_micros") or 0
        )
        payload["fencing_token"] = int(payload["fencing_token"])
        return payload

    def _expire_source_leases(
        self,
        connection: Connection,
        project_id: str,
        entitlement_source_id: str,
        *,
        actor: str = "model_gateway",
    ) -> int:
        rows = connection.execute(
            text(
                """
                SELECT id, budget_reservation_id
                FROM model_gateway_leases
                WHERE entitlement_source_id=:source_id
                  AND status='active' AND expires_at <= now()
                FOR UPDATE
                """
            ),
            {"source_id": entitlement_source_id},
        ).mappings().all()
        for row in rows:
            connection.execute(
                text(
                    "UPDATE model_gateway_leases "
                    "SET status='expired',completed_at=now() WHERE id=:id"
                ),
                {"id": row["id"]},
            )
            unresolved = connection.execute(text(
                "SELECT id FROM model_gateway_invocations WHERE model_gateway_lease_id=:id "
                "AND status='in_progress' LIMIT 1"), {"id": row['id']}).first()
            if unresolved:
                decision_id = create_blocking_decision(
                    connection, project_id, None,
                    f"Reconcile expired model invocation {unresolved[0]}",
                    {"action": "verify_provider_outcome_before_retry", "lease_id": row['id']},
                    {"budget_reservation_id": row['budget_reservation_id']},
                    'model_outcome_unknown',
                )
                emit_event(connection, project_id, 'model_gateway.reconciliation_required', actor,
                           'model_gateway_lease', row['id'],
                           {"decision_id": decision_id, "budget_reservation_id": row['budget_reservation_id']})
            elif row["budget_reservation_id"]:
                self.budget.release(
                    connection,
                    project_id,
                    row["budget_reservation_id"],
                    "Model capacity lease expired before settlement.",
                )
            emit_event(
                connection,
                project_id,
                "model_gateway.lease_expired",
                actor,
                "model_gateway_lease",
                row["id"],
                {"budget_reservation_id": row["budget_reservation_id"]},
            )
        return len(rows)

    def reap_expired(
        self, connection: Connection, project_id: str, *, actor: str
    ) -> dict[str, Any]:
        source_ids = connection.execute(
            text(
                """
                SELECT DISTINCT entitlement_source_id
                FROM model_gateway_leases
                WHERE project_id=:project_id AND status='active' AND expires_at <= now()
                """
            ),
            {"project_id": project_id},
        ).scalars().all()
        reaped = sum(
            self._expire_source_leases(
                connection, project_id, source_id, actor=actor
            )
            for source_id in source_ids
        )
        return {"project_id": project_id, "reaped": reaped}

    def list_leases(
        self,
        connection: Connection,
        project_id: str,
        *,
        status: Optional[str] = None,
        job_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        provider: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = ["project_id=:project_id"]
        params: dict[str, Any] = {"project_id": project_id, "limit": limit}
        for key, value in {
            "status": status,
            "job_id": job_id,
            "agent_id": agent_id,
            "provider": provider,
        }.items():
            if value:
                conditions.append(f"{key}=:{key}")
                params[key] = value
        rows = connection.execute(
            text(
                f"SELECT * FROM model_gateway_leases WHERE {' AND '.join(conditions)} "
                "ORDER BY acquired_at DESC LIMIT :limit"
            ),
            params,
        ).mappings().all()
        return [self._serialize_lease(dict(row)) for row in rows]

    def _load_lease(
        self,
        connection: Connection,
        project_id: str,
        lease_id: str,
        worker_id: str,
        lease_token: str,
        *,
        for_update: bool = True,
    ) -> dict[str, Any]:
        lock = "FOR UPDATE" if for_update else ""
        row = connection.execute(
            text(
                f"""
                SELECT * FROM model_gateway_leases
                WHERE id = :lease_id AND project_id = :project_id
                {lock}
                """
            ),
            {"lease_id": lease_id, "project_id": project_id},
        ).mappings().first()
        if not row:
            raise ModelGatewayLeaseError("Model gateway lease not found.")
        payload = dict(row)
        if payload["worker_id"] != worker_id:
            raise ModelGatewayLeaseError("Model capacity lease is owned by another worker.")
        if not compare_digest(payload["lease_token_hash"], self._hash_token(lease_token)):
            raise ModelGatewayLeaseError("Model capacity lease token is invalid.")
        if payload["status"] != "active":
            raise ModelGatewayLeaseError(f"Model capacity lease is {payload['status']}.")
        if payload["expires_at"] <= now_utc():
            raise ModelGatewayLeaseError("Model capacity lease has expired.")
        return payload

    def acquire(
        self,
        connection: Connection,
        project_id: str,
        *,
        job_id: str,
        worker_id: str,
        job_lease_token: str,
        agent_id: str,
        skill_id: str,
        preferred_capabilities: list[str],
        risk_tier: int,
        consumer_owner_id: Optional[str],
        estimated_cost_usd_micros: int,
        max_usage: dict[str, Any],
        dedupe_key: str,
        ttl_seconds: int,
        existing_reservation_id: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """`source_id` pins the lease to the source that will actually answer — a typed decision
        knows whether TypeSafe or the Claude CLI makes the call. Unpinned, the router chooses."""
        job = self.jobs.require_active_lease(
            connection, worker_id, job_id, job_lease_token
        )
        if job["project_id"] != project_id:
            raise ModelGatewayLeaseError("Job lease belongs to another project.")
        job_payload = job.get("payload") or {}
        expected_agent_id = job_payload.get("agent_id")
        if not expected_agent_id and isinstance(job_payload.get("agent_module"), dict):
            expected_agent_id = job_payload["agent_module"].get("id")
        if expected_agent_id and expected_agent_id != agent_id:
            raise ModelGatewayLeaseError(
                "Requested agent_id does not match the active job's agent scope."
            )
        for key, limit in (max_usage or {}).items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("max_usage keys must be non-empty strings.")
            if not isinstance(limit, (int, float)) or limit < 0:
                raise ValueError("max_usage values must be non-negative numbers.")
        existing = connection.execute(
            text(
                """
                SELECT * FROM model_gateway_leases
                WHERE project_id=:project_id AND dedupe_key=:dedupe_key
                """
            ),
            {"project_id": project_id, "dedupe_key": dedupe_key},
        ).mappings().first()
        if existing:
            if (
                existing["job_id"] != job_id
                or existing["worker_id"] != worker_id
                or existing["agent_id"] != agent_id
            ):
                raise ModelGatewayLeaseError(
                    "Model capacity dedupe key is already bound to another job scope."
                )
            return {"lease": self._serialize_lease(dict(existing)), "lease_token": None, "duplicate": True}

        decision = self.entitlements.simulate_model_route_decision(
            connection,
            project_id,
            skill_id=skill_id,
            task_kind=job["kind"],
            workload_class=job["workload_class"],
            risk_tier=risk_tier,
            preferred_capabilities=preferred_capabilities,
            data_boundary=job["data_boundary"],
            actor=worker_id,
            only_source_id=source_id,
        )
        if not decision.get("selected_source_id") or not decision["policy_result"]["allowed"]:
            raise ModelCapacityDeniedError(
                decision["policy_result"].get("denial_reason") or "No eligible model capacity."
            )
        source = self._source_for_update(
            connection, project_id, decision["selected_source_id"]
        )
        self._assert_owner_scope(source, project_id, consumer_owner_id)
        # A flat-rate or local source is not paid per call, whatever its kind: its reservation
        # is $0 of money (included_spend.py), a gate rather than a hold.
        paid = (source["source_kind"] in self.PAID_SOURCE_KINDS
                and not source_is_included(source))
        if paid and not source.get("credential_secret_ref_id"):
            raise ModelCapacityDeniedError(
                "Paid gateway capacity requires a canonical SecretRef credential binding."
            )
        self._expire_source_leases(
            connection, project_id, source["id"], actor=worker_id
        )
        max_concurrent = max(1, int((source.get("limit_shape") or {}).get("max_concurrent", 1)))
        active_count = int(
            connection.execute(
                text(
                    """
                    SELECT count(*) FROM model_gateway_leases
                    WHERE entitlement_source_id=:source_id AND status='active' AND expires_at > now()
                    """
                ),
                {"source_id": source["id"]},
            ).scalar_one()
        )
        if active_count >= max_concurrent:
            raise ModelCapacityDeniedError("Selected model source has no concurrent lease capacity.")

        if paid and estimated_cost_usd_micros <= 0:
            raise ModelCapacityDeniedError(
                "Paid model capacity requires a positive cost estimate and reservation."
            )
        reservation_id = None
        if existing_reservation_id:
            # The caller committed this reservation before the provider attempt.
            # Re-reserving would hold the same spend twice.
            active = self.budget.assert_active_reservation(
                connection, project_id, existing_reservation_id
            )
            if not active:
                raise ModelCapacityDeniedError(
                    "Model capacity requires an active budget reservation."
                )
            if int(active["amount_usd_micros"]) < int(estimated_cost_usd_micros):
                raise ModelCapacityDeniedError(
                    "Budget reservation does not cover the model estimate."
                )
            reservation_id = existing_reservation_id
        elif paid:
            reserved = self.budget.reserve(
                connection,
                project_id,
                "model_gateway",
                estimated_cost_usd_micros,
                f"Model capacity for job {job_id}",
                min(240, max(5, math.ceil(ttl_seconds / 60) + 5)),
                job_id=job_id,
                allocation_id=job.get("allocation_id"),
                risk_tier=risk_tier,
            )
            if reserved["status"] != "reserved":
                raise ModelCapacityDeniedError(reserved.get("message") or "Budget reservation denied.")
            reservation_id = reserved["reservation_id"]

        fencing_token = int(
            connection.execute(
                text(
                    "SELECT COALESCE(max(fencing_token), 0) + 1 FROM model_gateway_leases "
                    "WHERE entitlement_source_id=:source_id"
                ),
                {"source_id": source["id"]},
            ).scalar_one()
        )
        model_lease_token = token_urlsafe(32)
        lease_id = make_id("mlease")
        row = connection.execute(
            text(
                """
                INSERT INTO model_gateway_leases (
                    id, project_id, job_id, agent_id, worker_id, route_decision_id,
                    entitlement_source_id, entitlement_pool_id, source_id, provider,
                    model_id, auth_method, credential_secret_ref_id, consumer_owner_id,
                    lease_token_hash, fencing_token, estimated_cost_usd_micros,
                    budget_reservation_id, allocation_id, max_usage, dedupe_key,
                    expires_at
                ) VALUES (
                    :id,:project_id,:job_id,:agent_id,:worker_id,:route_decision_id,
                    :entitlement_source_id,:entitlement_pool_id,:source_id,:provider,
                    :model_id,:auth_method,:credential_secret_ref_id,:consumer_owner_id,
                    :lease_token_hash,:fencing_token,:estimated_cost,:reservation_id,
                    :allocation_id,CAST(:max_usage AS JSONB),:dedupe_key,
                    now() + make_interval(secs => :ttl_seconds)
                ) RETURNING *
                """
            ),
            {
                "id": lease_id,
                "project_id": project_id,
                "job_id": job_id,
                "agent_id": agent_id,
                "worker_id": worker_id,
                "route_decision_id": decision["id"],
                "entitlement_source_id": source["id"],
                "entitlement_pool_id": decision["selected_entitlement_pool_id"],
                "source_id": source["source_id"],
                "provider": source["provider"],
                "model_id": decision.get("selected_model_id"),
                "auth_method": source["auth_method"],
                "credential_secret_ref_id": source.get("credential_secret_ref_id"),
                "consumer_owner_id": consumer_owner_id,
                "lease_token_hash": self._hash_token(model_lease_token),
                "fencing_token": fencing_token,
                "estimated_cost": estimated_cost_usd_micros,
                "reservation_id": reservation_id,
                "allocation_id": job.get("allocation_id"),
                "max_usage": json.dumps(max_usage or {}),
                "dedupe_key": dedupe_key,
                "ttl_seconds": ttl_seconds,
            },
        ).mappings().one()
        emit_event(
            connection,
            project_id,
            "model_gateway.lease_acquired",
            worker_id,
            "model_gateway_lease",
            lease_id,
            {
                "job_id": job_id,
                "agent_id": agent_id,
                "source_id": source["source_id"],
                "model_id": decision.get("selected_model_id"),
                "fencing_token": fencing_token,
                "budget_reservation_id": reservation_id,
            },
        )
        return {"lease": self._serialize_lease(dict(row)), "lease_token": model_lease_token, "duplicate": False}

    def heartbeat(
        self,
        connection: Connection,
        project_id: str,
        lease_id: str,
        *,
        worker_id: str,
        lease_token: str,
        job_lease_token: str,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        lease = self._load_lease(connection, project_id, lease_id, worker_id, lease_token)
        self.jobs.require_active_lease(connection, worker_id, lease["job_id"], job_lease_token)
        row = connection.execute(
            text(
                """
                UPDATE model_gateway_leases
                SET heartbeat_at=now(), expires_at=now() + make_interval(secs => :ttl_seconds)
                WHERE id=:id RETURNING *
                """
            ),
            {"id": lease_id, "ttl_seconds": ttl_seconds},
        ).mappings().one()
        emit_event(connection, project_id, "model_gateway.lease_heartbeat", worker_id,
                   "model_gateway_lease", lease_id, {"job_id": lease["job_id"]})
        return self._serialize_lease(dict(row))

    def start_invocation(
        self,
        connection: Connection,
        project_id: str,
        lease_id: str,
        *,
        worker_id: str,
        lease_token: str,
        job_lease_token: str,
        request_hash: str,
        dedupe_key: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        self._safe_metadata(metadata)
        lease = self._load_lease(connection, project_id, lease_id, worker_id, lease_token)
        self.jobs.require_active_lease(connection, worker_id, lease["job_id"], job_lease_token)
        existing = connection.execute(
            text("SELECT * FROM model_gateway_invocations WHERE project_id=:p AND dedupe_key=:d"),
            {"p": project_id, "d": dedupe_key},
        ).mappings().first()
        if existing:
            if existing["model_gateway_lease_id"] != lease_id:
                raise ModelGatewayInvocationError(
                    "Invocation dedupe key is already bound to another model lease."
                )
            return {"invocation": self._serialize_invocation(dict(existing)), "duplicate": True}
        scope = self._project_scope(connection, project_id)
        receipt, _ = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            f"model-gateway:{project_id}:{dedupe_key}",
            "model.invoke",
            {
                "provider": lease["provider"],
                "source_id": lease["source_id"],
                "model_id": lease.get("model_id"),
                "data_boundary": self.jobs.require_active_lease(
                    connection, worker_id, lease["job_id"], job_lease_token
                )["data_boundary"],
            },
            {"request_hash": request_hash, "metadata": metadata},
            created_by_job_id=lease["job_id"],
        )
        invocation_id = make_id("minv")
        row = connection.execute(
            text(
                """
                INSERT INTO model_gateway_invocations (
                  id,project_id,model_gateway_lease_id,job_id,agent_id,receipt_id,
                  request_hash,dedupe_key,metadata
                ) VALUES (:id,:p,:l,:j,:a,:r,:h,:d,CAST(:m AS JSONB)) RETURNING *
                """
            ),
            {"id": invocation_id, "p": project_id, "l": lease_id, "j": lease["job_id"],
             "a": lease["agent_id"], "r": receipt["id"], "h": request_hash,
             "d": dedupe_key, "m": json.dumps(metadata)},
        ).mappings().one()
        emit_event(connection, project_id, "model_gateway.invocation_started", worker_id,
                   "model_gateway_invocation", invocation_id,
                   {"lease_id": lease_id, "receipt_id": receipt["id"], "request_hash": request_hash})
        return {"invocation": self._serialize_invocation(dict(row)), "duplicate": False}

    def _serialize_invocation(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row)
        for key in ("started_at", "completed_at"):
            payload[key] = self._iso(payload.get(key))
        if payload.get("actual_cost_usd_micros") is not None:
            payload["actual_cost_usd_micros"] = int(payload["actual_cost_usd_micros"])
        return payload

    def list_invocations(
        self,
        connection: Connection,
        project_id: str,
        *,
        lease_id: Optional[str] = None,
        job_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = ["project_id=:project_id"]
        params: dict[str, Any] = {"project_id": project_id, "limit": limit}
        columns = {
            "lease_id": "model_gateway_lease_id",
            "job_id": "job_id",
            "status": "status",
        }
        for key, column in columns.items():
            value = {"lease_id": lease_id, "job_id": job_id, "status": status}[key]
            if value:
                conditions.append(f"{column}=:{key}")
                params[key] = value
        rows = connection.execute(
            text(
                f"SELECT * FROM model_gateway_invocations WHERE {' AND '.join(conditions)} "
                "ORDER BY started_at DESC LIMIT :limit"
            ),
            params,
        ).mappings().all()
        return [self._serialize_invocation(dict(row)) for row in rows]

    def settle_invocation(
        self,
        connection: Connection,
        project_id: str,
        lease_id: str,
        invocation_id: str,
        *,
        worker_id: str,
        lease_token: str,
        job_lease_token: str,
        succeeded: bool,
        provider_ref: Optional[str],
        usage_units: dict[str, Any],
        actual_cost_usd_micros: Optional[int],
        observed_capacity: dict[str, Any],
        error: dict[str, Any],
    ) -> dict[str, Any]:
        lease = self._load_lease(connection, project_id, lease_id, worker_id, lease_token)
        self.jobs.require_active_lease(connection, worker_id, lease["job_id"], job_lease_token)
        invocation = connection.execute(
            text(
                """
                SELECT * FROM model_gateway_invocations
                WHERE id=:id AND project_id=:p AND model_gateway_lease_id=:l FOR UPDATE
                """
            ),
            {"id": invocation_id, "p": project_id, "l": lease_id},
        ).mappings().first()
        if not invocation:
            raise ModelGatewayInvocationError("Model invocation not found.")
        invocation = dict(invocation)
        if invocation["status"] != "in_progress":
            return self._serialize_invocation(invocation)

        # A SUBSCRIPTION CALL IS NOT MONEY (included_spend.py). The Claude CLI reports what a
        # call would cost at API list price whether or not a subscription answered it, and that
        # figure used to settle here as actual money: boss-1's $10 cap went red with no API key.
        # The lease names the source; on a subscription, seat or local model the money settled
        # is $0 and the reported figure is kept as the call's API-equivalent value.
        included = self._lease_source_included(connection, lease)
        money_usd_micros, equivalent_usd_micros = split(actual_cost_usd_micros, included)

        if succeeded:
            self.receipts.mark_succeeded(
                connection, invocation["receipt_id"], provider_ref,
                {"usage_units": usage_units, "model_id": lease.get("model_id"),
                 **({"included_in_subscription": True,
                     "equivalent_usd_micros": equivalent_usd_micros} if included else {})},
            )
        else:
            self.receipts.mark_failed(connection, invocation["receipt_id"], error or {"reason": "provider_call_failed"})

        reservation_id = lease.get("budget_reservation_id")
        chargeable = succeeded or (actual_cost_usd_micros or 0) > 0
        if reservation_id and chargeable:
            self.budget.consume(
                connection, project_id, reservation_id, invocation["receipt_id"],
                "model_gateway", lease.get("model_id") or lease["source_id"],
                int(lease["estimated_cost_usd_micros"]), actual_cost_usd_micros, worker_id,
                included_in_subscription=included,
            )
        elif reservation_id:
            self.budget.release(connection, project_id, reservation_id, "Model invocation failed before billable use.")

        status = "succeeded" if succeeded else "failed"
        usage_overruns = {
            key: {"allowed": int(limit), "actual": int(usage_units.get(key, 0))}
            for key, limit in (lease.get("max_usage") or {}).items()
            if isinstance(limit, (int, float))
            and isinstance(usage_units.get(key, 0), (int, float))
            and usage_units.get(key, 0) > limit
        }
        if usage_overruns:
            emit_event(
                connection,
                project_id,
                "model_gateway.usage_limit_exceeded",
                worker_id,
                "model_gateway_lease",
                lease_id,
                {"invocation_id": invocation_id, "overruns": usage_overruns},
            )
        row = connection.execute(
            text(
                """
                UPDATE model_gateway_invocations
                SET status=:status, provider_ref=:provider_ref,
                    usage_units=CAST(:usage AS JSONB), actual_cost_usd_micros=:actual,
                    error=CAST(:error AS JSONB), completed_at=now()
                WHERE id=:id RETURNING *
                """
            ),
            {"status": status, "provider_ref": provider_ref, "usage": json.dumps(usage_units),
             "actual": money_usd_micros, "error": json.dumps(error or {}), "id": invocation_id},
        ).mappings().one()
        connection.execute(
            text("UPDATE model_gateway_leases SET status=:status, completed_at=now(), result=CAST(:result AS JSONB) WHERE id=:id"),
            {"status": "completed" if succeeded else "failed", "id": lease_id,
             "result": json.dumps({"invocation_id": invocation_id,
                                   "receipt_id": invocation["receipt_id"],
                                   "usage_overruns": usage_overruns})},
        )
        self.entitlements.record_capacity_probe(
            connection, project_id, lease["entitlement_pool_id"], actor=worker_id,
            observed_capacity=observed_capacity or {"capacity_state": "available"},
            usage_units=usage_units, provider_ref=provider_ref,
            model_route_decision_id=lease["route_decision_id"],
            dedupe_key=f"model-invocation:{invocation_id}", status=status,
        )
        connection.execute(
            text(
                """
                INSERT INTO llm_calls (
                  id,project_id,stage,engine,model,prompt_tokens,completion_tokens,
                  total_tokens,cost_usd_micros,ok,error,included_in_subscription
                ) VALUES (:id,:p,'model_gateway',:engine,:model,:prompt,:completion,
                          :total,:cost,:ok,:error,:included)
                """
            ),
            {"id": make_id("llm"), "p": project_id, "engine": lease["source_id"],
             "model": lease.get("model_id"), "prompt": int(usage_units.get("prompt_tokens", 0)),
             "completion": int(usage_units.get("completion_tokens", 0)),
             "total": int(usage_units.get("total_tokens", 0)),
             "cost": int(actual_cost_usd_micros or 0), "ok": succeeded,
             "error": None if succeeded else json.dumps(error or {})[:2000],
             "included": included},
        )
        emit_event(connection, project_id, f"model_gateway.invocation_{status}", worker_id,
                   "model_gateway_invocation", invocation_id,
                   {"lease_id": lease_id, "receipt_id": invocation["receipt_id"],
                    "provider_ref": provider_ref})
        if reservation_id:
            self.budget._resolve_reconciliation(connection, project_id, reservation_id,
                                                 'Model invocation outcome recorded.')
        return self._serialize_invocation(dict(row))

    @staticmethod
    def _lease_source_included(connection: Connection, lease: dict[str, Any]) -> bool:
        """Whether the source this lease runs on is flat-rate or local capacity."""
        row = connection.execute(
            text("SELECT entitlement_kind, cost_mode FROM entitlement_sources WHERE id = :id"),
            {"id": lease.get("entitlement_source_id")},
        ).mappings().first()
        return source_is_included(dict(row) if row else None)

    def release(
        self, connection: Connection, project_id: str, lease_id: str, *,
        worker_id: str, lease_token: str, job_lease_token: str, reason: str
    ) -> dict[str, Any]:
        lease = self._load_lease(connection, project_id, lease_id, worker_id, lease_token)
        self.jobs.require_active_lease(connection, worker_id, lease["job_id"], job_lease_token)
        started = int(connection.execute(
            text("SELECT count(*) FROM model_gateway_invocations WHERE model_gateway_lease_id=:id"),
            {"id": lease_id}).scalar_one())
        if started:
            raise ModelGatewayLeaseError("A started invocation must be settled, not released.")
        if lease.get("budget_reservation_id"):
            self.budget.release(connection, project_id, lease["budget_reservation_id"], reason)
        row = connection.execute(
            text("UPDATE model_gateway_leases SET status='released',completed_at=now(),result=CAST(:r AS JSONB) WHERE id=:id RETURNING *"),
            {"id": lease_id, "r": json.dumps({"reason": reason})},
        ).mappings().one()
        emit_event(connection, project_id, "model_gateway.lease_released", worker_id,
                   "model_gateway_lease", lease_id, {"reason": reason})
        return self._serialize_lease(dict(row))

    def abandon_expired_attempt(
        self,
        connection: Connection,
        project_id: str,
        lease_id: str,
        *,
        worker_id: str,
        invocation_id: str,
        reason: str,
        lease_token: Optional[str] = None,
        job_lease_token: Optional[str] = None,
    ) -> dict[str, Any]:
        """Close a lease that expired before the provider was contacted.

        settle_invocation refuses an expired lease, so the reservation would
        stay held and the in-progress receipt would look like a call that might
        have been billed. This path is only for the window before the call.
        """
        row = connection.execute(
            text(
                """
                SELECT * FROM model_gateway_leases
                WHERE id = :lease_id AND project_id = :project_id
                FOR UPDATE
                """
            ),
            {"lease_id": lease_id, "project_id": project_id},
        ).mappings().first()
        if not row:
            raise ModelGatewayLeaseError("Model gateway lease not found.")
        lease = dict(row)
        if lease["worker_id"] != worker_id:
            raise ModelGatewayLeaseError("Model capacity lease is owned by another worker.")
        expired_job = False
        fenced = lease_token is not None or job_lease_token is not None
        if fenced:
            # The internal pre-submission Architecture caller knows no send occurred.
            # Both original tokens still need to own these locked rows: a reclaimed
            # job or replaced model lease cannot be cleaned up by the old caller.
            if not lease_token or not job_lease_token or not compare_digest(
                lease["lease_token_hash"], self._hash_token(lease_token),
            ):
                raise ModelGatewayLeaseError("Model capacity lease token is invalid.")
            job = self.jobs._load_leased_job_for_update(
                connection, worker_id, lease["job_id"], job_lease_token,
                allow_expired=True,
            )
            if job["project_id"] != project_id or lease["status"] not in ("active", "expired"):
                raise ModelGatewayLeaseError("Unused model authority no longer matches.")
            expired_job = job["lease_expires_at"] <= now_utc()
        if lease["status"] == "active" and lease["expires_at"] > now_utc() and not expired_job:
            raise ModelGatewayLeaseError("Model capacity lease is still active.")
        if lease["status"] == "active":
            connection.execute(
                text(
                    "UPDATE model_gateway_leases "
                    "SET status='expired', completed_at=now() WHERE id=:id"
                ),
                {"id": lease_id},
            )
            emit_event(
                connection, project_id, "model_gateway.lease_expired", worker_id,
                "model_gateway_lease", lease_id,
                {"budget_reservation_id": lease.get("budget_reservation_id"), "reason": reason},
            )
        invocation = connection.execute(
            text(
                """
                SELECT * FROM model_gateway_invocations
                WHERE id=:id AND project_id=:project_id AND model_gateway_lease_id=:lease_id
                FOR UPDATE
                """
            ),
            {"id": invocation_id, "project_id": project_id, "lease_id": lease_id},
        ).mappings().first()
        if fenced and (not invocation or invocation["status"] != "in_progress"):
            raise ModelGatewayLeaseError("Unused invocation is no longer in progress.")
        if invocation and invocation["status"] == "in_progress":
            self.receipts.mark_failed(
                connection, invocation["receipt_id"], {"reason": reason},
            )
            connection.execute(
                text(
                    """
                    UPDATE model_gateway_invocations
                    SET status='failed', error=CAST(:error AS JSONB), completed_at=now()
                    WHERE id=:id
                    """
                ),
                {"id": invocation_id, "error": json.dumps({"reason": reason})},
            )
            emit_event(
                connection, project_id, "model_gateway.invocation_failed", worker_id,
                "model_gateway_invocation", invocation_id,
                {"lease_id": lease_id, "receipt_id": invocation["receipt_id"], "reason": reason},
            )
        # The pre-dispatch caller has positively established that no provider call occurred.
        # This remains valid when the generic reaper already marked the lease expired.
        if lease.get("budget_reservation_id"):
            self.budget.release(connection, project_id, lease["budget_reservation_id"], reason)
        return {"lease_id": lease_id, "status": "expired"}

    def resolve_execution_binding(
        self, connection: Connection, project_id: str, lease_id: str, *,
        worker_id: str, lease_token: str, job_lease_token: str
    ) -> dict[str, Any]:
        """Internal adapter boundary; never mount this method as an HTTP endpoint."""
        lease = self._load_lease(connection, project_id, lease_id, worker_id, lease_token)
        self.jobs.require_active_lease(connection, worker_id, lease["job_id"], job_lease_token)
        if lease.get("credential_secret_ref_id"):
            return {
                "kind": "secret",
                "provider": lease["provider"],
                "model_id": lease.get("model_id"),
                "credential": resolve_secret_ref(
                    connection, lease["credential_secret_ref_id"], self.settings
                ),
                "max_usage": lease.get("max_usage") or {},
                "fencing_token": int(lease["fencing_token"]),
                "expires_at": self._iso(lease["expires_at"]),
            }
        return {
            "kind": "local_official_session",
            "provider": lease["provider"],
            "model_id": lease.get("model_id"),
            "source_id": lease["source_id"],
            "max_usage": lease.get("max_usage") or {},
            "fencing_token": int(lease["fencing_token"]),
            "expires_at": self._iso(lease["expires_at"]),
        }

    def usage_summary(
        self,
        connection: Connection,
        project_id: str,
        *,
        days: int = 30,
    ) -> dict[str, Any]:
        """Return project-fenced model usage from the canonical spend ledger."""
        if days < 1 or days > 366:
            raise ValueError("days must be between 1 and 366.")
        self._project_scope(connection, project_id)
        rows = connection.execute(
            text(
                """
                SELECT engine, model,
                       count(*) AS calls,
                       count(*) FILTER (WHERE ok) AS successful_calls,
                       COALESCE(sum(prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(sum(completion_tokens), 0) AS completion_tokens,
                       COALESCE(sum(total_tokens), 0) AS total_tokens,
                       COALESCE(sum(cost_usd_micros), 0) AS cost_usd_micros,
                       min(created_at) AS first_call_at,
                       max(created_at) AS last_call_at
                FROM llm_calls
                WHERE project_id = :project_id
                  AND created_at >= now() - (:days * interval '1 day')
                GROUP BY engine, model
                ORDER BY cost_usd_micros DESC, calls DESC, engine, model
                """
            ),
            {"project_id": project_id, "days": days},
        ).mappings().all()
        items = []
        for row in rows:
            item = dict(row)
            for key in (
                "calls",
                "successful_calls",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "cost_usd_micros",
            ):
                item[key] = int(item[key] or 0)
            item["failed_calls"] = item["calls"] - item["successful_calls"]
            item["first_call_at"] = self._iso(item["first_call_at"])
            item["last_call_at"] = self._iso(item["last_call_at"])
            items.append(item)
        return {
            "project_id": project_id,
            "window_days": days,
            "totals": {
                "calls": sum(item["calls"] for item in items),
                "successful_calls": sum(item["successful_calls"] for item in items),
                "failed_calls": sum(item["failed_calls"] for item in items),
                "prompt_tokens": sum(item["prompt_tokens"] for item in items),
                "completion_tokens": sum(item["completion_tokens"] for item in items),
                "total_tokens": sum(item["total_tokens"] for item in items),
                "cost_usd_micros": sum(item["cost_usd_micros"] for item in items),
            },
            "items": items,
        }
