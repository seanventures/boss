"""Versioned Agent Groups and typed Integration Connections.

These services own collaboration state and capability resolution. They never
turn consensus into proof and never expose raw credential values.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .budget import BudgetGovernor
from .core import emit_event, json_hash, make_id
from .jobs import JobLeaseService, enqueue_job
from .skill_scope import skill_reference_matches


class CollaborationError(RuntimeError):
    pass


class CollaborationNotFoundError(CollaborationError):
    pass


class CollaborationConflictError(CollaborationError):
    pass


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


class IntegrationRegistryService:
    REQUIRED_SKILL_CONTRACT_FIELDS = {
        "input_schema",
        "output_schema",
        "permissions",
        "risk_tier",
        "cost",
        "data_boundaries",
        "retry",
        "artifact_requirements",
        "evidence_requirements",
        "side_effect_class",
        "receipt_required",
        "dedupe_required",
    }

    def __init__(self, settings: Settings):
        self.settings = settings

    def create_definition(
        self,
        connection: Connection,
        *,
        project_id: str,
        integration_key: str,
        name: str,
        category: str,
        skill_contracts: dict[str, Any],
        executor_bindings: list[dict[str, Any]],
        provenance: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        integration_key = _required_text(integration_key, "integration_key")
        if not provenance.get("trusted"):
            raise ValueError("Integration definitions require trusted provenance.")
        if not isinstance(skill_contracts, dict) or not skill_contracts:
            raise ValueError("skill_contracts must be a non-empty object.")
        for skill_id, contract in skill_contracts.items():
            _required_text(skill_id, "skill_contracts key")
            if not isinstance(contract, dict):
                raise ValueError(f"Skill contract '{skill_id}' must be an object.")
            missing = sorted(self.REQUIRED_SKILL_CONTRACT_FIELDS - set(contract))
            if missing:
                raise ValueError(
                    f"Skill contract '{skill_id}' is missing: {', '.join(missing)}."
                )
            risk = int(contract["risk_tier"])
            if risk < 0 or risk > 3:
                raise ValueError(f"Skill contract '{skill_id}' has invalid risk_tier.")
            effect = str(contract["side_effect_class"])
            if effect != "read" and (
                not contract["receipt_required"] or not contract["dedupe_required"]
            ):
                raise ValueError(
                    f"Effectful skill '{skill_id}' must require receipts and dedupe."
                )
        payload = {
            "integration_key": integration_key,
            "name": name,
            "category": category,
            "skill_contracts": skill_contracts,
            "executor_bindings": executor_bindings,
            "provenance": provenance,
        }
        digest = json_hash(payload)
        duplicate = (
            connection.execute(
                text(
                    "SELECT * FROM integration_definitions "
                    "WHERE project_id=:p AND content_hash=:hash"
                ),
                {"p": project_id, "hash": digest},
            )
            .mappings()
            .first()
        )
        if duplicate:
            return {**_row(duplicate), "duplicate": True}
        current = (
            connection.execute(
                text(
                    "SELECT * FROM integration_definitions WHERE project_id=:p "
                    "AND integration_key=:key AND status='active' FOR UPDATE"
                ),
                {"p": project_id, "key": integration_key},
            )
            .mappings()
            .first()
        )
        version = int(current["version"]) + 1 if current else 1
        if current:
            connection.execute(
                text(
                    "UPDATE integration_definitions SET status='superseded' WHERE id=:id"
                ),
                {"id": current["id"]},
            )
        definition_id = make_id("integration")
        row = (
            connection.execute(
                text(
                    """
                INSERT INTO integration_definitions (
                  id,project_id,integration_key,version,name,category,skill_contracts,
                  executor_bindings,provenance,content_hash,created_by
                ) VALUES (
                  :id,:p,:key,:version,:name,:category,CAST(:skills AS JSONB),
                  CAST(:bindings AS JSONB),CAST(:provenance AS JSONB),:hash,:actor
                ) RETURNING *
                """
                ),
                {
                    "id": definition_id,
                    "p": project_id,
                    "key": integration_key,
                    "version": version,
                    "name": _required_text(name, "name"),
                    "category": _required_text(category, "category"),
                    "skills": json.dumps(skill_contracts),
                    "bindings": json.dumps(executor_bindings or []),
                    "provenance": json.dumps(provenance),
                    "hash": digest,
                    "actor": actor,
                },
            )
            .mappings()
            .one()
        )
        emit_event(
            connection,
            project_id,
            "integration.definition_published",
            actor,
            "integration_definition",
            definition_id,
            {"integration_key": integration_key, "version": version},
        )
        return {**_row(row), "duplicate": False}

    def list_definitions(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = (
            connection.execute(
                text(
                    "SELECT * FROM integration_definitions WHERE project_id=:p "
                    "ORDER BY integration_key,version DESC"
                ),
                {"p": project_id},
            )
            .mappings()
            .all()
        )
        return [_row(row) for row in rows]

    def create_connection(
        self,
        connection: Connection,
        *,
        project_id: str,
        integration_definition_id: str,
        connection_key: str,
        scope_type: str,
        scope_id: str,
        owner_ref: str,
        secret_ref_ids: list[str],
        granted_capabilities: list[str],
        data_boundaries: list[str],
        max_risk_tier: int,
        freshness_seconds: int,
        provenance: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        definition = self._definition(connection, project_id, integration_definition_id)
        contracts = definition["skill_contracts"] or {}
        unknown = sorted(set(granted_capabilities) - set(contracts))
        if unknown:
            raise ValueError(
                "Connection cannot grant undeclared capabilities: " + ", ".join(unknown)
            )
        if not granted_capabilities:
            raise ValueError("granted_capabilities must not be empty.")
        if not data_boundaries:
            raise ValueError("data_boundaries must not be empty.")
        if freshness_seconds <= 0:
            raise ValueError("freshness_seconds must be positive.")
        if scope_type == "project" and scope_id != project_id:
            raise ValueError("Project-scoped connection must use its own project_id.")
        if secret_ref_ids:
            rows = (
                connection.execute(
                    text(
                        "SELECT id,status FROM secret_refs WHERE project_id=:p AND id = ANY(:ids)"
                    ),
                    {"p": project_id, "ids": secret_ref_ids},
                )
                .mappings()
                .all()
            )
            states = {row["id"]: row["status"] for row in rows}
            invalid = [
                secret_id
                for secret_id in secret_ref_ids
                if states.get(secret_id) not in {"active", "rotating", "rotation_due"}
            ]
            if invalid:
                raise ValueError("Connection requires active project SecretRefs.")
        connection_id = make_id("iconn")
        row = (
            connection.execute(
                text(
                    """
                INSERT INTO integration_connections (
                  id,project_id,integration_definition_id,connection_key,
                  scope_type,scope_id,owner_ref,secret_ref_ids,granted_capabilities,
                  data_boundaries,max_risk_tier,freshness_seconds,provenance,created_by
                ) VALUES (
                  :id,:p,:definition,:key,:scope_type,:scope_id,:owner,
                  CAST(:secrets AS JSONB),CAST(:capabilities AS JSONB),
                  CAST(:boundaries AS JSONB),:risk,:freshness,
                  CAST(:provenance AS JSONB),:actor
                ) RETURNING *
                """
                ),
                {
                    "id": connection_id,
                    "p": project_id,
                    "definition": integration_definition_id,
                    "key": _required_text(connection_key, "connection_key"),
                    "scope_type": scope_type,
                    "scope_id": _required_text(scope_id, "scope_id"),
                    "owner": _required_text(owner_ref, "owner_ref"),
                    "secrets": json.dumps(secret_ref_ids or []),
                    "capabilities": json.dumps(granted_capabilities),
                    "boundaries": json.dumps(data_boundaries),
                    "risk": max_risk_tier,
                    "freshness": freshness_seconds,
                    "provenance": json.dumps(provenance or {}),
                    "actor": actor,
                },
            )
            .mappings()
            .one()
        )
        emit_event(
            connection,
            project_id,
            "integration.connection_installed",
            actor,
            "integration_connection",
            connection_id,
            {
                "integration_definition_id": integration_definition_id,
                "granted_capabilities": granted_capabilities,
            },
        )
        return _row(row)

    def update_connection(
        self,
        connection: Connection,
        *,
        project_id: str,
        connection_id: str,
        health_status: str,
        status: str,
        rate_limit_state: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        if status == "active" and health_status != "healthy":
            raise ValueError(
                "A connection can become active only after a healthy check."
            )
        if status == "revoked":
            health_status = "revoked"
        row = (
            connection.execute(
                text(
                    """
                UPDATE integration_connections
                SET health_status=:health,status=:status,
                    health_checked_at=CASE WHEN :health='pending' THEN health_checked_at ELSE now() END,
                    rate_limit_state=CAST(:limits AS JSONB),updated_at=now()
                WHERE id=:id AND project_id=:p RETURNING *
                """
                ),
                {
                    "health": health_status,
                    "status": status,
                    "limits": json.dumps(rate_limit_state or {}),
                    "id": connection_id,
                    "p": project_id,
                },
            )
            .mappings()
            .first()
        )
        if not row:
            raise CollaborationNotFoundError("Integration connection not found.")
        emit_event(
            connection,
            project_id,
            "integration.connection_updated",
            actor,
            "integration_connection",
            connection_id,
            {"status": status, "health_status": health_status},
        )
        return _row(row)

    def list_connections(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = (
            connection.execute(
                text(
                    """
                SELECT c.*,d.integration_key,d.name AS integration_name,d.version AS integration_version
                FROM integration_connections c
                JOIN integration_definitions d ON d.id=c.integration_definition_id
                WHERE c.project_id=:p ORDER BY c.updated_at DESC
                """
                ),
                {"p": project_id},
            )
            .mappings()
            .all()
        )
        return [_row(row) for row in rows]

    @staticmethod
    def _allows(authority: list[str], skill_id: str) -> bool:
        return any(skill_reference_matches(ref, skill_id) for ref in authority)

    def resolve_capability(
        self,
        connection: Connection,
        *,
        project_id: str,
        skill_id: str,
        actor_ref: str,
        role_allowed: list[str],
        group_allowed: list[str],
        workflow_allowed: list[str],
        project_allowed: list[str],
        requested_data_boundary: str,
        risk_tier: int,
        dedupe_key: str,
        workflow_step_ref: Optional[str] = None,
        agent_group_version_ref: Optional[str] = None,
    ) -> dict[str, Any]:
        existing = (
            connection.execute(
                text(
                    "SELECT * FROM capability_resolutions WHERE project_id=:p AND dedupe_key=:d"
                ),
                {"p": project_id, "d": dedupe_key},
            )
            .mappings()
            .first()
        )
        if existing:
            return {**_row(existing), "duplicate": True}
        authority_inputs = {
            "role_allowed": role_allowed,
            "group_allowed": group_allowed,
            "workflow_allowed": workflow_allowed,
            "project_allowed": project_allowed,
        }
        reasons = [
            f"{name} does not allow {skill_id}"
            for name, allowed in authority_inputs.items()
            if not allowed or not self._allows(allowed, skill_id)
        ]
        candidates = (
            connection.execute(
                text(
                    """
                SELECT c.*,d.skill_contracts,d.executor_bindings,d.provenance,
                       d.integration_key,d.version AS integration_version
                FROM integration_connections c
                JOIN integration_definitions d ON d.id=c.integration_definition_id
                WHERE c.project_id=:p AND c.status='active' AND d.status='active'
                  AND c.granted_capabilities ? :skill
                ORDER BY c.updated_at DESC
                """
                ),
                {"p": project_id, "skill": skill_id},
            )
            .mappings()
            .all()
        )
        selected = None
        contract: dict[str, Any] = {}
        candidate_reasons: list[str] = []
        now = datetime.now(timezone.utc)
        for raw in candidates:
            candidate = dict(raw)
            local_reasons: list[str] = []
            if candidate["health_status"] != "healthy":
                local_reasons.append("connection is not healthy")
            checked = candidate.get("health_checked_at")
            if not checked or (now - checked).total_seconds() > int(
                candidate["freshness_seconds"]
            ):
                local_reasons.append("connection health is stale")
            if requested_data_boundary not in (candidate["data_boundaries"] or []):
                local_reasons.append("data boundary is not granted")
            if risk_tier > int(candidate["max_risk_tier"]):
                local_reasons.append("risk tier exceeds connection grant")
            if not (candidate["provenance"] or {}).get("trusted"):
                local_reasons.append("integration provenance is untrusted")
            skill_contract = (candidate["skill_contracts"] or {}).get(skill_id)
            if not skill_contract:
                local_reasons.append(
                    "definition does not implement the requested skill"
                )
            elif requested_data_boundary not in skill_contract.get(
                "data_boundaries", []
            ):
                local_reasons.append("skill contract rejects the data boundary")
            elif risk_tier > int(skill_contract.get("risk_tier", -1)):
                local_reasons.append("skill contract rejects the risk tier")
            if not local_reasons and not reasons:
                selected = candidate
                contract = skill_contract
                break
            candidate_reasons.extend(local_reasons)
        if not candidates:
            candidate_reasons.append(
                "no active installed connection grants this capability"
            )
        reasons.extend(candidate_reasons)
        decision = "allowed" if selected else "denied"
        resolution_id = make_id("capres")
        row = (
            connection.execute(
                text(
                    """
                INSERT INTO capability_resolutions (
                  id,project_id,integration_connection_id,skill_id,actor_ref,
                  workflow_step_ref,agent_group_version_ref,requested_data_boundary,
                  risk_tier,authority_inputs,decision,denial_reasons,
                  resolved_contract,dedupe_key
                ) VALUES (
                  :id,:p,:connection,:skill,:actor,:workflow,:group,:boundary,:risk,
                  CAST(:authority AS JSONB),:decision,CAST(:reasons AS JSONB),
                  CAST(:contract AS JSONB),:dedupe
                ) RETURNING *
                """
                ),
                {
                    "id": resolution_id,
                    "p": project_id,
                    "connection": selected["id"] if selected else None,
                    "skill": skill_id,
                    "actor": actor_ref,
                    "workflow": workflow_step_ref,
                    "group": agent_group_version_ref,
                    "boundary": requested_data_boundary,
                    "risk": risk_tier,
                    "authority": json.dumps(authority_inputs),
                    "decision": decision,
                    "reasons": json.dumps(sorted(set(reasons))),
                    "contract": json.dumps(contract),
                    "dedupe": _required_text(dedupe_key, "dedupe_key"),
                },
            )
            .mappings()
            .one()
        )
        emit_event(
            connection,
            project_id,
            f"integration.capability_{decision}",
            actor_ref,
            "capability_resolution",
            resolution_id,
            {
                "skill_id": skill_id,
                "integration_connection_id": selected["id"] if selected else None,
                "denial_reasons": sorted(set(reasons)),
            },
        )
        return {**_row(row), "duplicate": False}

    def list_resolutions(
        self, connection: Connection, project_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        rows = (
            connection.execute(
                text(
                    "SELECT * FROM capability_resolutions WHERE project_id=:p "
                    "ORDER BY created_at DESC LIMIT :limit"
                ),
                {"p": project_id, "limit": limit},
            )
            .mappings()
            .all()
        )
        return [_row(row) for row in rows]

    @staticmethod
    def _definition(
        connection: Connection, project_id: str, definition_id: str
    ) -> dict[str, Any]:
        row = (
            connection.execute(
                text(
                    "SELECT * FROM integration_definitions WHERE id=:id AND project_id=:p "
                    "AND status='active'"
                ),
                {"id": definition_id, "p": project_id},
            )
            .mappings()
            .first()
        )
        if not row:
            raise CollaborationNotFoundError("Active integration definition not found.")
        return _row(row)


class AgentGroupService:
    TOPOLOGIES = {
        "panel",
        "producer_reviewer",
        "bounded_debate",
        "swarm",
        "fan_out",
        "sequential_handoff",
    }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.jobs = JobLeaseService(settings)
        self.budget = BudgetGovernor(settings)

    def create_group(
        self,
        connection: Connection,
        *,
        project_id: str,
        group_key: str,
        name: str,
        purpose: str,
        owner_ref: str,
        topology: str,
        members: list[dict[str, Any]],
        collaboration_protocol: dict[str, Any],
        allowed_capabilities: list[str],
        context_policy: dict[str, Any],
        independence_policy: dict[str, Any],
        bounds: dict[str, Any],
        completion_rule: dict[str, Any],
        escalation_target: str,
        actor: str,
    ) -> dict[str, Any]:
        if topology not in self.TOPOLOGIES:
            raise ValueError("Unsupported Agent Group topology.")
        _required_list(members, "members")
        member_keys = [
            _required_text(item.get("member_key"), "members[].member_key")
            for item in members
        ]
        if len(member_keys) != len(set(member_keys)):
            raise ValueError("Agent Group member_key values must be unique.")
        for member in members:
            _required_text(member.get("role_key"), "members[].role_key")
        if int(bounds.get("max_rounds", 0)) <= 0:
            raise ValueError("bounds.max_rounds must be positive.")
        if int(bounds.get("timeout_seconds", 0)) <= 0:
            raise ValueError("bounds.timeout_seconds must be positive.")
        rule = dict(completion_rule or {})
        if rule.get("kind") not in {
            "all",
            "quorum",
            "producer_reviewer",
            "evidence_gate",
        }:
            raise ValueError("completion_rule.kind is invalid.")
        rule["evidence_required"] = True
        group_key = _required_text(group_key, "group_key")
        spec = (
            connection.execute(
                text(
                    "SELECT * FROM agent_group_specs WHERE project_id=:p AND group_key=:key FOR UPDATE"
                ),
                {"p": project_id, "key": group_key},
            )
            .mappings()
            .first()
        )
        if spec is None:
            spec_id = make_id("agroup")
            spec = (
                connection.execute(
                    text(
                        """
                    INSERT INTO agent_group_specs (
                      id,project_id,group_key,name,purpose,owner_ref,created_by
                    ) VALUES (:id,:p,:key,:name,:purpose,:owner,:actor) RETURNING *
                    """
                    ),
                    {
                        "id": spec_id,
                        "p": project_id,
                        "key": group_key,
                        "name": _required_text(name, "name"),
                        "purpose": _required_text(purpose, "purpose"),
                        "owner": _required_text(owner_ref, "owner_ref"),
                        "actor": actor,
                    },
                )
                .mappings()
                .one()
            )
        else:
            spec_id = spec["id"]
        payload = {
            "topology": topology,
            "members": members,
            "collaboration_protocol": collaboration_protocol,
            "allowed_capabilities": allowed_capabilities,
            "context_policy": context_policy,
            "independence_policy": independence_policy,
            "bounds": bounds,
            "completion_rule": rule,
            "escalation_target": escalation_target,
        }
        digest = json_hash(payload)
        duplicate = (
            connection.execute(
                text(
                    "SELECT * FROM agent_group_versions WHERE agent_group_spec_id=:spec AND content_hash=:hash"
                ),
                {"spec": spec_id, "hash": digest},
            )
            .mappings()
            .first()
        )
        if duplicate:
            return {
                "spec": _row(spec),
                "version": {**_row(duplicate), "duplicate": True},
            }
        version = int(
            connection.execute(
                text(
                    "SELECT COALESCE(max(version),0)+1 FROM agent_group_versions WHERE agent_group_spec_id=:spec"
                ),
                {"spec": spec_id},
            ).scalar_one()
        )
        connection.execute(
            text(
                "UPDATE agent_group_versions SET status='retired' WHERE agent_group_spec_id=:spec AND status='published'"
            ),
            {"spec": spec_id},
        )
        version_id = make_id("agver")
        version_row = (
            connection.execute(
                text(
                    """
                INSERT INTO agent_group_versions (
                  id,project_id,agent_group_spec_id,version,status,topology,members,
                  collaboration_protocol,allowed_capabilities,context_policy,
                  independence_policy,bounds,completion_rule,escalation_target,
                  content_hash,created_by,published_at
                ) VALUES (
                  :id,:p,:spec,:version,'published',:topology,CAST(:members AS JSONB),
                  CAST(:protocol AS JSONB),CAST(:capabilities AS JSONB),
                  CAST(:context AS JSONB),CAST(:independence AS JSONB),
                  CAST(:bounds AS JSONB),CAST(:completion AS JSONB),:escalation,
                  :hash,:actor,now()
                ) RETURNING *
                """
                ),
                {
                    "id": version_id,
                    "p": project_id,
                    "spec": spec_id,
                    "version": version,
                    "topology": topology,
                    "members": json.dumps(members),
                    "protocol": json.dumps(collaboration_protocol or {}),
                    "capabilities": json.dumps(allowed_capabilities or []),
                    "context": json.dumps(context_policy or {}),
                    "independence": json.dumps(independence_policy or {}),
                    "bounds": json.dumps(bounds),
                    "completion": json.dumps(rule),
                    "escalation": _required_text(
                        escalation_target, "escalation_target"
                    ),
                    "hash": digest,
                    "actor": actor,
                },
            )
            .mappings()
            .one()
        )
        spec = (
            connection.execute(
                text(
                    "UPDATE agent_group_specs SET status='active',active_version_id=:version,updated_at=now() WHERE id=:id RETURNING *"
                ),
                {"version": version_id, "id": spec_id},
            )
            .mappings()
            .one()
        )
        emit_event(
            connection,
            project_id,
            "agent_group.version_published",
            actor,
            "agent_group_version",
            version_id,
            {"group_key": group_key, "version": version, "topology": topology},
        )
        return {
            "spec": _row(spec),
            "version": {**_row(version_row), "duplicate": False},
        }

    def list_groups(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = (
            connection.execute(
                text(
                    """
                SELECT s.*,v.version,v.topology,v.members,v.allowed_capabilities,
                       v.bounds,v.completion_rule,v.status AS version_status
                FROM agent_group_specs s
                LEFT JOIN agent_group_versions v ON v.id=s.active_version_id
                WHERE s.project_id=:p ORDER BY s.name
                """
                ),
                {"p": project_id},
            )
            .mappings()
            .all()
        )
        return [_row(row) for row in rows]

    def start_run(
        self,
        connection: Connection,
        *,
        project_id: str,
        agent_group_version_id: str,
        input_payload: dict[str, Any],
        context: dict[str, Any],
        capability_resolution_ids: list[str],
        governance_gate_results: dict[str, bool],
        dedupe_key: str,
        actor: str,
        assurance_case_id: Optional[str] = None,
        workflow_run_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> dict[str, Any]:
        existing = (
            connection.execute(
                text(
                    "SELECT * FROM agent_group_runs WHERE project_id=:p AND dedupe_key=:d"
                ),
                {"p": project_id, "d": dedupe_key},
            )
            .mappings()
            .first()
        )
        if existing:
            return {
                "run": _row(existing),
                "members": self._member_runs(connection, existing["id"]),
                "duplicate": True,
            }
        version = (
            connection.execute(
                text(
                    "SELECT * FROM agent_group_versions WHERE id=:id AND project_id=:p AND status='published'"
                ),
                {"id": agent_group_version_id, "p": project_id},
            )
            .mappings()
            .first()
        )
        if not version:
            raise CollaborationNotFoundError("Published Agent Group version not found.")
        for table, ref_name, ref_value in (
            ("assurance_cases", "assurance_case_id", assurance_case_id),
            ("workflow_runs", "workflow_run_id", workflow_run_id),
            ("tasks", "task_id", task_id),
        ):
            if ref_value and not connection.execute(
                text(f"SELECT 1 FROM {table} WHERE id=:id AND project_id=:p"),
                {"id": ref_value, "p": project_id},
            ).scalar():
                raise CollaborationConflictError(
                    f"{ref_name} must reference the same project."
                )
        resolutions = []
        if capability_resolution_ids:
            resolutions = (
                connection.execute(
                    text(
                        "SELECT id,decision,skill_id FROM capability_resolutions WHERE project_id=:p AND id = ANY(:ids)"
                    ),
                    {"p": project_id, "ids": capability_resolution_ids},
                )
                .mappings()
                .all()
            )
            if len(resolutions) != len(set(capability_resolution_ids)) or any(
                row["decision"] != "allowed" for row in resolutions
            ):
                raise CollaborationConflictError(
                    "Agent Group requires allowed, project-scoped capability resolutions."
                )
            group_capabilities = version["allowed_capabilities"] or []
            unauthorized = sorted(
                row["skill_id"]
                for row in resolutions
                if not any(
                    skill_reference_matches(ref, row["skill_id"])
                    for ref in group_capabilities
                )
            )
            if unauthorized:
                raise CollaborationConflictError(
                    "Agent Group version does not authorize resolved capabilities: "
                    + ", ".join(unauthorized)
                )
        bounds = version["bounds"] or {}
        budget_limit = int(bounds.get("budget_usd_micros") or 0)
        reservation_id = None
        if budget_limit > 0:
            reservation = self.budget.reserve(
                connection,
                project_id,
                "agent_group",
                budget_limit,
                f"Agent Group {agent_group_version_id} execution",
                max(5, int(bounds["timeout_seconds"]) // 60 + 5),
                risk_tier=1,
            )
            if reservation["status"] != "reserved":
                raise CollaborationConflictError(
                    reservation.get("message") or "Agent Group budget denied."
                )
            reservation_id = reservation["reservation_id"]
        policy_snapshot = {
            "agent_group_version_id": agent_group_version_id,
            "capability_resolution_ids": capability_resolution_ids,
            "governance_gate_results": governance_gate_results,
            "consensus_is_not_proof": True,
            "evidence_required": True,
            "budget_limit_usd_micros": budget_limit,
        }
        run_id = make_id("agrun")
        run = (
            connection.execute(
                text(
                    """
                INSERT INTO agent_group_runs (
                  id,project_id,agent_group_version_id,assurance_case_id,
                  workflow_run_id,task_id,status,input,context,policy_snapshot,
                  budget_reservation_id,dedupe_key,created_by,deadline_at
                ) VALUES (
                  :id,:p,:version,:case,:workflow,:task,'running',
                  CAST(:input AS JSONB),CAST(:context AS JSONB),
                  CAST(:policy AS JSONB),:reservation,:dedupe,:actor,:deadline
                ) RETURNING *
                """
                ),
                {
                    "id": run_id,
                    "p": project_id,
                    "version": agent_group_version_id,
                    "case": assurance_case_id,
                    "workflow": workflow_run_id,
                    "task": task_id,
                    "input": json.dumps(input_payload or {}),
                    "context": json.dumps(context or {}),
                    "policy": json.dumps(policy_snapshot),
                    "reservation": reservation_id,
                    "dedupe": _required_text(dedupe_key, "dedupe_key"),
                    "actor": actor,
                    "deadline": datetime.now(timezone.utc)
                    + timedelta(seconds=int(bounds["timeout_seconds"])),
                },
            )
            .mappings()
            .one()
        )
        for member in version["members"] or []:
            member_key = member["member_key"]
            role_key = member["role_key"]
            role_exists = connection.execute(
                text("SELECT 1 FROM roles WHERE project_id=:p AND role_key=:role"),
                {"p": project_id, "role": role_key},
            ).scalar()
            if not role_exists:
                raise CollaborationConflictError(
                    f"Agent Group role '{role_key}' is not installed."
                )
            job = enqueue_job(
                connection,
                project_id,
                "agent_group.member",
                {
                    "agent_group_run_id": run_id,
                    "agent_group_version_id": agent_group_version_id,
                    "member_key": member_key,
                    "role_key": role_key,
                    "input": input_payload,
                    "context_policy": version["context_policy"],
                    "independence_policy": version["independence_policy"],
                },
                task_id=task_id,
                priority=50,
                max_attempts=3,
                dedupe_key=f"agent-group:{dedupe_key}:{member_key}",
                workload_class="agent_group",
                data_boundary=(context or {}).get("data_boundary", "internal"),
            )
            connection.execute(
                text(
                    """
                    INSERT INTO agent_group_member_runs (
                      id,project_id,agent_group_run_id,member_key,role_key,job_id,
                      independent,input
                    ) VALUES (:id,:p,:run,:member,:role,:job,:independent,CAST(:input AS JSONB))
                    """
                ),
                {
                    "id": make_id("agmrun"),
                    "p": project_id,
                    "run": run_id,
                    "member": member_key,
                    "role": role_key,
                    "job": job["id"],
                    "independent": bool(member.get("independent", True)),
                    "input": json.dumps(input_payload or {}),
                },
            )
        emit_event(
            connection,
            project_id,
            "agent_group.run_started",
            actor,
            "agent_group_run",
            run_id,
            {
                "agent_group_version_id": agent_group_version_id,
                "member_count": len(version["members"] or []),
            },
        )
        return {
            "run": _row(run),
            "members": self._member_runs(connection, run_id),
            "duplicate": False,
        }

    def complete_member(
        self,
        connection: Connection,
        *,
        project_id: str,
        run_id: str,
        member_key: str,
        worker_id: str,
        job_lease_token: str,
        succeeded: bool,
        output: dict[str, Any],
        artifacts: list[Any],
        messages: list[Any],
        evidence_refs: list[Any],
        receipt_refs: list[Any],
        cost_usd_micros: int,
        error: dict[str, Any],
    ) -> dict[str, Any]:
        member = (
            connection.execute(
                text(
                    """
                SELECT m.*,r.policy_snapshot AS run_policy_snapshot,
                       r.budget_reservation_id,r.deadline_at
                FROM agent_group_member_runs m
                JOIN agent_group_runs r ON r.id=m.agent_group_run_id
                WHERE m.agent_group_run_id=:run AND m.member_key=:member
                  AND m.project_id=:p AND r.status IN ('running','waiting') FOR UPDATE
                """
                ),
                {"run": run_id, "member": member_key, "p": project_id},
            )
            .mappings()
            .first()
        )
        if not member:
            raise CollaborationNotFoundError("Active Agent Group member run not found.")
        self.jobs.require_active_lease(
            connection, worker_id, member["job_id"], job_lease_token
        )
        if cost_usd_micros > 0 and not receipt_refs:
            raise CollaborationConflictError(
                "Spendy member work requires receipt references."
            )
        budget_limit = int(
            (member.get("run_policy_snapshot") or {}).get(
                "budget_limit_usd_micros", 0
            )
        )
        spent = int(
            connection.execute(
                text(
                    "SELECT COALESCE(sum(cost_usd_micros),0) FROM agent_group_member_runs "
                    "WHERE agent_group_run_id=:run AND id<>:member"
                ),
                {"run": run_id, "member": member["id"]},
            ).scalar_one()
        )
        if budget_limit and spent + cost_usd_micros > budget_limit:
            succeeded = False
            error = {
                **(error or {}),
                "message": "Agent Group member exceeded the group budget limit.",
                "budget_limit_usd_micros": budget_limit,
                "reported_total_usd_micros": spent + cost_usd_micros,
            }
        if receipt_refs:
            valid_receipts = connection.execute(
                text(
                    "SELECT id FROM receipts WHERE project_id=:p AND id = ANY(:ids)"
                ),
                {"p": project_id, "ids": receipt_refs},
            ).scalars().all()
            if len(set(valid_receipts)) != len(set(receipt_refs)):
                raise CollaborationConflictError(
                    "Member receipt references must belong to the same project."
                )
        status = "completed" if succeeded else "failed"
        row = (
            connection.execute(
                text(
                    """
                UPDATE agent_group_member_runs SET status=:status,output=CAST(:output AS JSONB),
                  artifacts=CAST(:artifacts AS JSONB),messages=CAST(:messages AS JSONB),
                  evidence_refs=CAST(:evidence AS JSONB),receipt_refs=CAST(:receipts AS JSONB),
                  cost_usd_micros=:cost,error=CAST(:error AS JSONB),
                  started_at=COALESCE(started_at,now()),completed_at=now()
                WHERE id=:id RETURNING *
                """
                ),
                {
                    "status": status,
                    "output": json.dumps(output or {}),
                    "artifacts": json.dumps(artifacts or []),
                    "messages": json.dumps(messages or []),
                    "evidence": json.dumps(evidence_refs or []),
                    "receipts": json.dumps(receipt_refs or []),
                    "cost": cost_usd_micros,
                    "error": json.dumps(error or {}),
                    "id": member["id"],
                },
            )
            .mappings()
            .one()
        )
        emit_event(
            connection,
            project_id,
            f"agent_group.member_{status}",
            worker_id,
            "agent_group_member_run",
            member["id"],
            {"agent_group_run_id": run_id, "member_key": member_key},
        )
        if succeeded:
            self.jobs.complete(
                connection,
                worker_id,
                member["job_id"],
                job_lease_token,
                {"agent_group_run_id": run_id, "member_key": member_key},
            )
        else:
            self.jobs.fail(
                connection,
                worker_id,
                member["job_id"],
                job_lease_token,
                error or {"reason": "agent_group_member_failed"},
                retryable=False,
                retry_delay_seconds=0,
            )
        return _row(row)

    def finalize_run(
        self,
        connection: Connection,
        *,
        project_id: str,
        run_id: str,
        output: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        run = (
            connection.execute(
                text(
                    "SELECT * FROM agent_group_runs WHERE id=:id AND project_id=:p FOR UPDATE"
                ),
                {"id": run_id, "p": project_id},
            )
            .mappings()
            .first()
        )
        if not run:
            raise CollaborationNotFoundError("Agent Group run not found.")
        if run["status"] in {
            "completed",
            "failed",
            "blocked",
            "deadlettered",
            "cancelled",
        }:
            return {"run": _row(run), "members": self._member_runs(connection, run_id)}
        version = (
            connection.execute(
                text("SELECT * FROM agent_group_versions WHERE id=:id"),
                {"id": run["agent_group_version_id"]},
            )
            .mappings()
            .one()
        )
        members = self._member_runs(connection, run_id)
        completed = [member for member in members if member["status"] == "completed"]
        pending = [
            member
            for member in members
            if member["status"] in {"queued", "leased", "running"}
        ]
        rule = version["completion_rule"] or {}
        kind = rule.get("kind")
        timed_out = bool(
            run.get("deadline_at")
            and run["deadline_at"] <= datetime.now(timezone.utc)
        )
        if timed_out and pending:
            connection.execute(
                text(
                    "UPDATE agent_group_member_runs SET status='timed_out',completed_at=now(),"
                    "error=jsonb_build_object('message','Agent Group deadline exceeded') "
                    "WHERE agent_group_run_id=:run AND status IN ('queued','leased','running')"
                ),
                {"run": run_id},
            )
            members = self._member_runs(connection, run_id)
            completed = [member for member in members if member["status"] == "completed"]
            pending = []
            status = "failed"
            reason = "Agent Group deadline exceeded"
        elif pending:
            status = "waiting"
            reason = "member work is still active"
        else:
            required = (
                len(members)
                if kind == "all"
                else int(rule.get("min_count", max(1, len(members) // 2 + 1)))
            )
            quorum = len(completed) >= required
            evidence_count = sum(
                len(member.get("evidence_refs") or []) for member in completed
            )
            gates = (run["policy_snapshot"] or {}).get("governance_gate_results") or {}
            failed_gates = sorted(key for key, allowed in gates.items() if not allowed)
            budget_limit = int(
                (run["policy_snapshot"] or {}).get("budget_limit_usd_micros", 0)
            )
            producer_reviewer_ok = True
            if kind == "producer_reviewer":
                producer = rule.get("producer_member")
                reviewer = rule.get("reviewer_member")
                by_key = {member["member_key"]: member for member in completed}
                producer_reviewer_ok = bool(
                    producer in by_key
                    and reviewer in by_key
                    and (by_key[reviewer].get("output") or {}).get("verdict")
                    == "approve"
                )
            if not quorum:
                status = "failed"
                reason = "completion rule was not met"
            elif evidence_count == 0:
                status = "blocked"
                reason = "group agreement is not proof; no evidence was attached"
            elif failed_gates:
                status = "blocked"
                reason = "governance gates failed: " + ", ".join(failed_gates)
            elif budget_limit and sum(
                int(member.get("cost_usd_micros") or 0) for member in members
            ) > budget_limit:
                status = "blocked"
                reason = "Agent Group budget limit was exceeded"
            elif not producer_reviewer_ok:
                status = "blocked"
                reason = "producer-reviewer approval rule was not met"
            else:
                status = "completed"
                reason = "completion protocol and deterministic gates passed"
        dissent = []
        for member in members:
            member_output = member.get("output") or {}
            if member_output.get("dissent") or member["status"] != "completed":
                dissent.append(
                    {
                        "member_key": member["member_key"],
                        "status": member["status"],
                        "statement": member_output.get("dissent")
                        or (member.get("error") or {}).get("message"),
                        "evidence_refs": member.get("evidence_refs") or [],
                    }
                )
        total_cost = sum(int(member.get("cost_usd_micros") or 0) for member in members)
        receipts = sorted(
            {
                str(ref)
                for member in members
                for ref in (member.get("receipt_refs") or [])
            }
        )
        if status != "waiting" and run.get("budget_reservation_id"):
            budget_limit = int(
                (run["policy_snapshot"] or {}).get("budget_limit_usd_micros", 0)
            )
            if total_cost > 0:
                if not receipts:
                    status = "blocked"
                    reason = "Spendy Agent Group work has no valid receipt"
                else:
                    self.budget.consume(
                        connection,
                        project_id,
                        run["budget_reservation_id"],
                        receipts[0],
                        "agent_group",
                        f"agent_group:{run['agent_group_version_id']}",
                        budget_limit,
                        total_cost,
                        actor,
                    )
            else:
                self.budget.release(
                    connection,
                    project_id,
                    run["budget_reservation_id"],
                    "Agent Group completed without billable work.",
                )
        row = (
            connection.execute(
                text(
                    """
                UPDATE agent_group_runs SET status=:status,output=CAST(:output AS JSONB),
                  dissent=CAST(:dissent AS JSONB),total_cost_usd_micros=:cost,
                  receipt_refs=CAST(:receipts AS JSONB),updated_at=now(),
                  completed_at=CASE WHEN :status IN ('completed','failed','blocked') THEN now() ELSE NULL END
                WHERE id=:id RETURNING *
                """
                ),
                {
                    "status": status,
                    "output": json.dumps(
                        {**(output or {}), "disposition_reason": reason}
                    ),
                    "dissent": json.dumps(dissent),
                    "cost": total_cost,
                    "receipts": json.dumps(receipts),
                    "id": run_id,
                },
            )
            .mappings()
            .one()
        )
        emit_event(
            connection,
            project_id,
            f"agent_group.run_{status}",
            actor,
            "agent_group_run",
            run_id,
            {
                "reason": reason,
                "dissent_count": len(dissent),
                "evidence_count": sum(
                    len(member.get("evidence_refs") or []) for member in completed
                ),
            },
        )
        return {"run": _row(row), "members": members}

    def list_runs(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = (
            connection.execute(
                text(
                    """
                SELECT r.*,s.group_key,s.name AS group_name,v.version,v.topology
                FROM agent_group_runs r
                JOIN agent_group_versions v ON v.id=r.agent_group_version_id
                JOIN agent_group_specs s ON s.id=v.agent_group_spec_id
                WHERE r.project_id=:p ORDER BY r.updated_at DESC
                """
                ),
                {"p": project_id},
            )
            .mappings()
            .all()
        )
        return [_row(row) for row in rows]

    @staticmethod
    def _member_runs(connection: Connection, run_id: str) -> list[dict[str, Any]]:
        rows = (
            connection.execute(
                text(
                    "SELECT * FROM agent_group_member_runs WHERE agent_group_run_id=:run ORDER BY created_at,member_key"
                ),
                {"run": run_id},
            )
            .mappings()
            .all()
        )
        return [_row(row) for row in rows]
