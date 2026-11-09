"""Generic ProviderOps applies own their transaction boundaries.

A committed claim is never permission to retry an uncertain remote effect. Host
stack/app deployments retain their separate phased owner and are refused here.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from .core import create_blocking_decision, emit_event, json_hash
from .resource_plane import ChangeSetNotFoundError, ResourcePlaneService

Authorize = Callable[[Connection, str], str]


class ApplyAuthorityRefused(PermissionError):
    """The current canonical principal no longer has this apply permission."""


def _operations(connection: Connection, changeset_id: str) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(text(
        "SELECT id, project_id, changeset_id, provider_account_id, operation_index, "
        "skill_id, action, resource_type, resource_ref, desired_state, risk_tier, "
        "destructive, production_affecting FROM changeset_operations "
        "WHERE changeset_id=:id ORDER BY operation_index"), {"id": changeset_id}).mappings()]


def _locked(connection: Connection, service: ResourcePlaneService, changeset_id: str) -> dict:
    if not connection.execute(text("SELECT id FROM changesets WHERE id=:id FOR UPDATE"),
                              {"id": changeset_id}).first():
        raise ChangeSetNotFoundError("ChangeSet not found.")
    return service._get_changeset_any_project(connection, changeset_id)


def _response(connection: Connection, service: ResourcePlaneService, changeset: dict) -> dict:
    rows = connection.execute(text("SELECT * FROM resource_receipts WHERE changeset_id=:id "
                                   "ORDER BY created_at,id"), {"id": changeset['id']}).mappings()
    decision = connection.execute(text(
        "SELECT id FROM decisions WHERE project_id=:p AND status='pending' "
        "AND recommendation->>'action'='reconcile_provider_changeset' "
        "AND recommendation->>'changeset_id'=:id ORDER BY created_at LIMIT 1"),
        {"p": changeset['project_id'], "id": changeset['id']}).scalar()
    return {"status": changeset['status'],
            "changeset": service.get_changeset(connection, changeset['project_id'], changeset['id']),
            "resource_receipts": [service._serialize_resource_receipt(row) for row in rows],
            "decision_id": decision}


def _account(connection: Connection, service: ResourcePlaneService, project: str, operation: dict) -> dict:
    if not operation['provider_account_id']:
        raise ValueError("Provider write operation requires a ProviderAccount.")
    account = service.get_provider_account(connection, project, operation['provider_account_id'])
    if account['provider'] != service._provider_from_skill(operation['skill_id']):
        raise ValueError("ProviderAccount does not match the operation.")
    service._require_account_secret_refs_usable(connection, account)
    if account['read_only'] and operation['action'] not in {'read', 'sync'}:
        raise ValueError("Provider account is read-only; provider write blocked.")
    if operation['destructive'] and account['destructive_actions_disabled']:
        raise ValueError("Provider account blocks destructive provider actions.")
    # Resolve before admission as well as at use, but never retain the credential.
    service._credential_value(account['credential_ref'])
    return account


def _account_binding(account: dict) -> str:
    return json_hash({key: account.get(key) for key in (
        'id', 'project_id', 'provider', 'credential_ref', 'credential_refs',
        'credential_version', 'status', 'read_only', 'destructive_actions_disabled')})


def _active_budget(connection: Connection, service: ResourcePlaneService, changeset: dict) -> None:
    if int(changeset['expected_cost_usd_micros']) > 0 or changeset['reservation_id']:
        connection.execute(text("SELECT id FROM budget_reservations WHERE id=:r AND project_id=:p FOR UPDATE"),
                           {"r": changeset['reservation_id'], "p": changeset['project_id']}).first()
        reservation = service.budget.assert_active_reservation(
            connection, changeset['project_id'], changeset['reservation_id'])
        if not reservation or int(reservation['amount_usd_micros']) < int(changeset['expected_cost_usd_micros']):
            raise ValueError("ChangeSet requires its active, sufficient BudgetReservation.")


def _current_surface(connection: Connection, project: str, actor: str, operation: dict) -> None:
    guard = (operation.get('desired_state') or {}).get('surface_ownership')
    if not guard:
        return
    # Admission's deduped preflight is historical evidence, not current authority.
    # Keep this row locked through the adapter call and result transaction.
    ownership = connection.execute(text(
        "SELECT owner_ref,version,status,expires_at FROM surface_ownerships "
        "WHERE project_id=:p AND surface_key=:s AND environment=:e FOR UPDATE"),
        {"p": project, "s": guard['surface_key'], "e": guard['environment']}).mappings().first()
    # Evaluate expiry after acquiring the lock, including any lock wait.
    now = connection.execute(text("SELECT clock_timestamp()")).scalar_one()
    if (not ownership or ownership['status'] != 'active'
            or ownership['owner_ref'] != actor or ownership['version'] != int(guard['version'])
            or (ownership['expires_at'] is not None and ownership['expires_at'] <= now)):
        raise ValueError("Surface ownership changed before provider contact.")


def _hold(engine: Engine, service: ResourcePlaneService, changeset_id: str, actor: str,
          error_type: str) -> dict:
    """No raw provider error or automatic release: contact may have succeeded."""
    with engine.begin() as c:
        changeset = _locked(c, service, changeset_id)
        if changeset['status'] == 'applying':
            error = {"code": "provider_outcome_unknown", "error_type": error_type,
                     "message": "Provider outcome needs reconciliation. Do not retry or release its budget."}
            c.execute(text("UPDATE changesets SET status='needs_reconciliation', "
                           "failure_reason=:reason, updated_at=now() WHERE id=:id"),
                      {"id": changeset_id, "reason": error['message']})
            c.execute(text("UPDATE resource_receipts SET error=CAST(:error AS JSONB), updated_at=now() "
                           "WHERE changeset_id=:id AND status='in_progress'"),
                      {"id": changeset_id, "error": json.dumps(error)})
            emit_event(c, changeset['project_id'], 'changeset.reconciliation_required', actor,
                       'changeset', changeset_id, error)
            changeset['status'] = 'needs_reconciliation'
        return _response(c, service, changeset)


def _unused(engine: Engine, service: ResourcePlaneService, changeset_id: str, actor: str) -> dict:
    """This live owner caught refusal before entering any adapter, unlike process loss."""
    with engine.begin() as c:
        changeset = _locked(c, service, changeset_id)
        if changeset['status'] != 'applying':
            return _response(c, service, changeset)
        error = {"code": "provider_not_dispatched", "message": "Apply authority changed before any provider call."}
        rows = c.execute(text("UPDATE resource_receipts SET status='failed', error=CAST(:error AS JSONB), "
                              "updated_at=now() WHERE changeset_id=:id AND status='in_progress' RETURNING receipt_id"),
                         {"id": changeset_id, "error": json.dumps(error)}).scalars().all()
        for receipt_id in rows:
            service.receipts.mark_failed(c, receipt_id, error)
        c.execute(text("UPDATE changeset_operations SET status='failed',updated_at=now() WHERE changeset_id=:id"),
                  {"id": changeset_id})
        if changeset['reservation_id']:
            service.budget.release(c, changeset['project_id'], changeset['reservation_id'],
                                   "Apply owner proved no provider adapter was entered.")
        c.execute(text("UPDATE changesets SET status='deadlettered', failure_reason=:reason,updated_at=now() WHERE id=:id"),
                  {"id": changeset_id, "reason": error['message']})
        c.execute(text("UPDATE decisions SET status='resolved', resolution_note=:reason,updated_at=now() "
                       "WHERE project_id=:p AND status='pending' AND recommendation->>'action'='reconcile_provider_changeset' "
                       "AND recommendation->>'changeset_id'=:id"),
                  {"id": changeset_id, "p": changeset['project_id'], "reason": error['message']})
        service._create_deadletter(c, project_id=changeset['project_id'], source_kind='changeset',
                                   source_id=changeset_id, reason=error['message'], payload=error)
        emit_event(c, changeset['project_id'], 'changeset.not_dispatched', actor, 'changeset', changeset_id, error)
        changeset['status'] = 'deadlettered'
        return _response(c, service, changeset)


def apply_changeset(engine: Engine, service: ResourcePlaneService, *, changeset_id: str,
                    authorize: Authorize, created_by_job_id: str | None = None,
                    dry_run: bool = False) -> dict:
    """Authorize, commit one claim, invoke each adapter once, then settle known results.

    No caller transaction spans admission. `authorize` must check the current
    canonical request principal at this project's existing permission boundary.
    A caller-supplied job ID is attribution, not lease proof: worker execution
    must use its owning leased protocol, never this human apply endpoint.
    """
    with engine.begin() as c:
        changeset = _locked(c, service, changeset_id)
        project = changeset['project_id']
        actor = authorize(c, project)
        service._refuse_phased(changeset)
        if created_by_job_id is not None:
            raise ValueError("Worker-owned applies require their leased owner protocol; a job ID is not authority.")
        if dry_run:
            return service.apply_changeset(c, changeset_id=changeset_id, actor=actor, dry_run=True)
        if changeset['status'] in {'applying', 'needs_reconciliation', 'applied'}:
            return _response(c, service, changeset)
        if changeset['status'] != 'approved':
            raise ValueError("ChangeSet must be approved before apply.")
        _active_budget(c, service, changeset)
        if changeset['reservation_id'] and c.execute(text(
                "SELECT id FROM changesets WHERE project_id=:p AND reservation_id=:r AND id<>:id "
                "AND status IN ('applying','needs_reconciliation','applied') LIMIT 1"),
                {"p": project, "r": changeset['reservation_id'], "id": changeset_id}).first():
            raise ValueError("Budget reservation already belongs to another admitted ChangeSet.")
        operations = _operations(c, changeset_id)
        if not operations:
            raise ValueError("ChangeSet has no operations to apply.")
        from .assurance import AssuranceService
        assurance = AssuranceService(service.settings)
        bindings = {}
        for operation in operations:
            if operation['project_id'] != project:
                raise ValueError("ChangeSet operation belongs to another project.")
            try:
                bindings[operation['id']] = _account_binding(_account(c, service, project, operation))
            except Exception as exc:
                raise ValueError("Provider account preflight refused before contact.") from exc
            guard = (operation.get('desired_state') or {}).get('surface_ownership')
            if guard:
                preflight = assurance.preflight_surface(
                    c, project_id=project, surface_key=guard['surface_key'],
                    environment=guard['environment'], actor_ref=actor,
                    expected_version=int(guard['version']),
                    effect_ref=f"changeset:{changeset_id}:operation:{operation['id']}",
                    dedupe_key=f"changeset-ownership:{changeset_id}:{operation['id']}")
                if not preflight['allowed']:
                    return {"status": "blocked", "changeset": service.get_changeset(c, project, changeset_id),
                            "resource_receipts": [], "decision_id": preflight['decision_id']}
        claims = [service.prepare_resource_action(c, changeset=changeset, operation=operation,
                  actor=actor, created_by_job_id=None) for operation in operations]
        c.execute(text("UPDATE changesets SET status='applying', applied_by=:actor, updated_at=now() WHERE id=:id"),
                  {"id": changeset_id, "actor": actor})
        c.execute(text("UPDATE changeset_operations SET status='applying', updated_at=now() WHERE changeset_id=:id"),
                  {"id": changeset_id})
        decision_id = create_blocking_decision(
            c, project, None, f"Check whether the provider change “{changeset.get('title') or 'its ChangeSet'}” "
            "finished applying, if its completion is lost",
            {"action": "reconcile_provider_changeset", "changeset_id": changeset_id,
             "reservation_id": changeset['reservation_id']},
            {"held_usd_micros": int(changeset['expected_cost_usd_micros'])},
            "provider_apply_in_progress")
        emit_event(c, project, 'changeset.apply_started', actor, 'changeset', changeset_id,
                   {"resource_receipt_ids": [x['resource_receipt_id'] for x in claims], "decision_id": decision_id})
    # The transaction above must finish before the first adapter call.
    contacted = False
    try:
        for claim in claims:
            operation = claim['operation']
            with engine.begin() as c:
                fresh = service._get_changeset_any_project(c, changeset_id)
                if fresh['status'] != 'applying' or authorize(c, project) != actor:
                    raise ValueError("Apply authority changed before contact.")
                _active_budget(c, service, fresh)
                current = next((x for x in _operations(c, changeset_id) if x['id'] == operation['id']), None)
                if current != operation:
                    raise ValueError("Claimed operation changed before contact.")
                account = _account(c, service, project, operation)
                if _account_binding(account) != bindings[operation['id']]:
                    raise ValueError("Claimed provider account changed before contact.")
                receipt = c.execute(text("SELECT status FROM resource_receipts WHERE id=:id AND project_id=:p "
                                         "AND changeset_id=:cs FOR UPDATE"),
                                    {"id": claim['resource_receipt_id'], "p": project, "cs": changeset_id}).scalar()
                if receipt != 'in_progress':
                    raise ValueError("Provider claim changed before contact.")
                _current_surface(c, project, actor, operation)
                contacted = True
                result = service._execute_provider_operation(c, account, operation)
                service.finish_resource_action(c, changeset=changeset, claim=claim, actor=actor, result=result)
        with engine.begin() as c:
            current = _locked(c, service, changeset_id)
            if current['status'] != 'applying':
                raise ValueError("Apply claim changed before settlement.")
            result = _response(c, service, current)
            receipts = result['resource_receipts']
            if len(receipts) != len(claims) or any(x['status'] != 'succeeded' for x in receipts):
                raise ValueError("Provider completion evidence is incomplete.")
            if changeset['reservation_id']:
                service.budget.settle(c, project, changeset['reservation_id'], receipts[0]['receipt_id'],
                                      'provider', 'resource_plane.changeset.apply',
                                      int(changeset['expected_cost_usd_micros']), None)
                service._record_changeset_cost_event(c, changeset=changeset, resource_receipts=receipts)
            service._mark_deployment_runs_deployed(c, project_id=project, changeset_id=changeset_id,
                                                   resource_receipts=receipts)
            service._register_elastic_workers_from_apply(c, project_id=project, changeset_id=changeset_id,
                                                         actor=actor, resource_receipts=receipts, operations=operations)
            c.execute(text("UPDATE changesets SET status='applied', applied_at=now(), failure_reason=NULL, "
                           "updated_at=now() WHERE id=:id"), {"id": changeset_id})
            c.execute(text("UPDATE decisions SET status='resolved', resolution_note='All provider results recorded; estimated cost settled.', "
                           "updated_at=now() WHERE id=:id AND status='pending'"), {"id": decision_id})
            emit_event(c, project, 'changeset.applied', actor, 'changeset', changeset_id,
                       {"status": "applied", "resource_receipt_ids": [x['id'] for x in receipts]})
            current['status'] = 'applied'
            return _response(c, service, current)
    except Exception as exc:
        # If even this write fails, the precontact applying claim and decision
        # remain durable. A restart never interprets them as permission to retry.
        result = (_hold(engine, service, changeset_id, actor, type(exc).__name__) if contacted
                  else _unused(engine, service, changeset_id, actor))
        if isinstance(exc, ApplyAuthorityRefused):
            raise
        return result
