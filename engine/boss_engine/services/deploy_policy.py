"""Whether a deploy asks a person first — the ONE answer both deploy paths read.

Two ways a deploy runs without opening a decision of its own, and no others:

  * THE WORKFLOW'S GATE ALREADY ASKED. A Deploy step a person approved at its gate has had its
    approval; the deployment it starts runs under that decision. HelpDesk's run asked twice —
    the step's gate, then the deployment's own card — and the second card sat in Approvals while
    the step looked approved. The runner passes the gate's decision EXPLICITLY; it is accepted
    only when it is approved, is a released deploy step's own gate, and has not already
    approved another deployment (no replay).
  * THE ENVIRONMENT'S POLICY SAYS SO. deployment_environment_policies.requires_approval, which
    nothing used to read. No row means ask; production asks unless someone set otherwise.

Either way the deployment still goes through its ChangeSet, reservation and receipt; only the
person's card is skipped, and who answered is recorded: the gate's approver, or
"policy:<policy id>" with the person who set the policy.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, now_utc


def _environment(environment: Any) -> str:
    return str(environment or "").strip().lower().replace(" ", "_")


def deploy_policy(connection: Connection, project_id: str, environment: Any) -> dict[str, Any]:
    """{requires_approval, policy_id, set_by, set_at, note} for `environment`. Default: ask."""
    env = _environment(environment)
    row = connection.execute(
        text("SELECT id, requires_approval, created_by, metadata, updated_at "
             "FROM deployment_environment_policies WHERE project_id = :p AND environment = :e "
             "AND status = 'active'"), {"p": project_id, "e": env}).mappings().first()
    if not row:
        return {"environment": env, "requires_approval": True, "policy_id": None,
                "set_by": None, "set_at": None, "note": None}
    meta = row["metadata"] if isinstance(row["metadata"], dict) else json.loads(
        row["metadata"] or "{}")
    return {"environment": env, "requires_approval": bool(row["requires_approval"]),
            "policy_id": row["id"],
            "set_by": meta.get("approval_set_by") or row["created_by"],
            "set_at": meta.get("approval_set_at") or (row["updated_at"].isoformat()
                                                     if row["updated_at"] else None),
            "note": meta.get("note")}


def set_approval_policy(connection: Connection, *, project_id: str, environment: Any,
                        requires_approval: bool, actor: str,
                        note: Optional[str] = None) -> dict[str, Any]:
    """Set ONLY whether `environment` asks before a deploy; every other rule of an existing
    policy is kept. Records who and when, and says so in an event."""
    env = _environment(environment)
    if not env:
        raise ValueError("environment is required")
    if not actor:
        raise ValueError("a person sets a deploy policy")
    stamp = {"approval_set_by": actor, "approval_set_at": now_utc().isoformat(),
             **({"note": note.strip()[:300]} if note and note.strip() else {})}
    policy_id = f"deppol_{project_id}_{env}"
    connection.execute(
        text("""
            INSERT INTO deployment_environment_policies
                (id, project_id, environment, requires_approval, status, created_by, metadata)
            VALUES (:id, :p, :e, :ra, 'active', :by, CAST(:m AS JSONB))
            ON CONFLICT (project_id, environment) DO UPDATE
            SET requires_approval = EXCLUDED.requires_approval,
                status = 'active',
                metadata = COALESCE(deployment_environment_policies.metadata, '{}'::jsonb)
                           || EXCLUDED.metadata,
                updated_at = now()
        """), {"id": policy_id, "p": project_id, "e": env, "ra": bool(requires_approval),
               "by": actor, "m": json.dumps(stamp)})
    policy = deploy_policy(connection, project_id, env)
    emit_event(connection, project_id, "deployment_environment_policy.approval_set", actor,
               "deployment_environment_policy", str(policy["policy_id"]),
               {"environment": env, "requires_approval": bool(requires_approval),
                "note": stamp.get("note")})
    return policy


def _gate_approval(connection: Connection, project_id: str,
                   decision_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text("SELECT status, approved_by FROM decisions WHERE id = :d AND project_id = :p"),
        {"d": decision_id, "p": project_id}).mappings().first()
    if not row or row["status"] != "approved":
        return None
    # A DEPLOY STEP'S OWN GATE, released for this very decision — not any approved decision
    # somebody (or a model filling a tool argument) names.
    gate = connection.execute(
        text("SELECT 1 FROM workflow_step_runs WHERE project_id = :p AND decision_id = :d "
             "AND binding_snapshot->>'released_for' = :d "
             "AND COALESCE(binding_snapshot->'binding'->>'capability', "
             "             binding_snapshot->>'capability', '') = 'deploy' LIMIT 1"),
        {"p": project_id, "d": decision_id}).first()
    if gate is None:
        return None
    # Once: a gate approves ONE deployment. A second deployment naming it asks again.
    used = connection.execute(
        text("SELECT 1 FROM deployment_runs WHERE decision_id = :d "
             "UNION ALL SELECT 1 FROM stack_deployments WHERE decision_id = :d LIMIT 1"),
        {"d": decision_id}).first()
    if used is not None:
        return None
    return {"source": "gate", "decision_id": decision_id,
            "approved_by": str(row["approved_by"] or "unknown")}


def pre_approval(connection: Connection, project_id: str, environment: Any, *,
                 gate_decision_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """How this deploy is already approved — {source, decision_id, approved_by, policy?} — or
    None, when a person must be asked. The gate first (it is a person's answer), then policy."""
    if gate_decision_id:
        found = _gate_approval(connection, project_id, str(gate_decision_id))
        if found:
            return found
    policy = deploy_policy(connection, project_id, environment)
    if policy["requires_approval"] or not policy["policy_id"]:
        return None
    return {"source": "policy", "decision_id": None,
            "approved_by": f"policy:{policy['policy_id']}", "policy": policy}


def record_pre_approval(connection: Connection, project_id: str, approval: dict[str, Any], *,
                        entity_type: str, entity_id: str, actor: str) -> None:
    """The event that says a deployment ran without a card of its own, and on whose say-so."""
    policy = approval.get("policy") or {}
    emit_event(connection, project_id, "deployment.approved_without_asking", actor,
               entity_type, entity_id,
               {"source": approval["source"], "decision_id": approval.get("decision_id"),
                "approved_by": approval["approved_by"],
                **({"policy_id": policy.get("policy_id"), "policy_set_by": policy.get("set_by"),
                    "policy_set_at": policy.get("set_at"), "environment": policy.get("environment")}
                   if policy else {})})
