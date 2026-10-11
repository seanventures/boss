"""Standalone operations on a provisioned resource: verify, observe, backup, restore, destroy.

They run as the `infra.operation` worker JOB (leased, retried, cancellable like every job), and
each writes a receipt through executor.run_hook. Policy:

    verify, observe, backup   queued directly (read-only, or additive)
    restore, destroy          need a person: a blocking decision whose approval queues the job
    destroy of an attached    refused outright; BOSS never destroys what it did not create

The operation is pinned the way a deploy plan is: its digest (resource, hook, plugin version and
checksum, observed state) is in the decision and the job; the job refuses if any moved.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text

from .contract import AttachSpec, Hook, ResourceMode, ResourceRequest, digest_of
from .executor import _json, ledger_row, run_hook
from .policy import destroy_refusal
from .registry import Registry

JOB_KIND = "infra.operation"
DECISION_ACTION = "infra_operation"
ALLOWED_HOOKS = frozenset({Hook.VERIFY, Hook.OBSERVE, Hook.BACKUP, Hook.RESTORE, Hook.DESTROY})
NEEDS_APPROVAL = frozenset({Hook.RESTORE, Hook.DESTROY})


class OperationRefused(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status, self.code = status, code
        super().__init__(message)


def _request_from_row(row: dict[str, Any]) -> ResourceRequest:
    mode = ResourceMode(row["mode"])
    attach = None
    if mode == ResourceMode.ATTACHED:
        names = list(_json(row.get("bindings"))) or ["BOSS_ATTACHED"]
        attach = AttachSpec(bindings={n: n for n in names})
    return ResourceRequest(name=row["name"], capability=row["capability"], mode=mode,
                           environment=row["environment"], public=bool(row.get("public_access")),
                           attach=attach)


def operation_digest(row: dict[str, Any], hook: Hook) -> str:
    return digest_of({"resourceId": row["resource_id"], "hook": hook.value, "pluginId": row["plugin_id"],
                      "pluginVersion": row["plugin_version"], "pluginChecksum": row.get("plugin_checksum"),
                      "state": _json(row.get("state"))})


def list_resources(connection: Any, project_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT resource_id, app_key, name, environment, capability, plugin_id, plugin_version, mode, status, "
        "public_access, bindings, evidence, last_plan_digest, created_at, updated_at, destroyed_at "
        "FROM infra_platform_resources WHERE project_id = :p ORDER BY app_key, name"),
        {"p": project_id}).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        # Names only: which env names the app gets, never a reference path or a value.
        d["bindings"] = sorted(_json(d.get("bindings")))
        d["evidence"] = _json(d.get("evidence")) if not isinstance(d.get("evidence"), list) else d["evidence"]
        for k in ("created_at", "updated_at", "destroyed_at"):
            d[k] = d[k].isoformat() if d.get(k) else None
        out.append(d)
    return out


def request_operation(connection: Any, settings: Any, *, project_id: str, resource_id: str, hook: str,
                      actor: str) -> dict[str, Any]:
    from ..services.core import create_blocking_decision
    from ..services.jobs import enqueue_job

    try:
        h = Hook(hook)
    except ValueError:
        raise OperationRefused(422, "bad_hook", f"{hook} is not a lifecycle hook") from None
    if h not in ALLOWED_HOOKS:
        raise OperationRefused(422, "bad_hook", f"{hook} runs only as part of a deploy plan")
    row = ledger_row(connection, project_id, resource_id)
    if row is None or row.get("destroyed_at"):
        raise OperationRefused(404, "not_found", "no such live resource in this project")
    if h == Hook.DESTROY:
        refusal = destroy_refusal(row["mode"])
        if refusal:
            raise OperationRefused(409, "attached_destroy", refusal)
    reg = Registry.load(connection).get(row["plugin_id"], row["plugin_version"])
    if reg is None or not reg.usable:
        raise OperationRefused(409, "plugin_unavailable", f"{row['plugin_id']} {row['plugin_version']} is not enabled")
    if not reg.manifest.implements(h):
        raise OperationRefused(409, "hook_unsupported", f"{reg.id} does not implement {h.value}")
    digest = operation_digest(row, h)
    payload = {"resource_id": resource_id, "hook": h.value, "digest": digest, "actor": actor}
    if h in NEEDS_APPROVAL:
        from ..services import action_preview
        inputs = {"resource": resource_id, "operation": h.value, "plugin": f"{reg.id} {reg.version}",
                  "mode": row["mode"], "digest": digest}
        rec = {"action": DECISION_ACTION, **payload, "project_id": project_id,
               "action_preview": action_preview.build(f"infra.{h.value}", inputs, step_label=h.value.title())}
        words = "Destroy" if h == Hook.DESTROY else "Restore"
        decision_id = create_blocking_decision(
            connection, project_id, None, f"{words} {row['name']} ({resource_id}) with {reg.id}?", rec,
            {"expected_cost_usd_micros": 0, "cost_confidence": "no_provider_spend"},
            f"infra_{h.value}_requires_approval", risk_tier=2, default_action="hold")
        return {"status": "awaiting_approval", "decision_id": decision_id, "hook": h.value}
    job = enqueue_job(connection, project_id, JOB_KIND, payload, max_attempts=3,
                      dedupe_key=f"infra-op:{resource_id}:{h.value}:{digest}", data_boundary="internal")
    return {"status": "queued", "job_id": job.get("id"), "hook": h.value}


def on_decision_approved(connection: Any, settings: Any, decision: dict, actor: str) -> Optional[dict]:
    from ..services.jobs import enqueue_job
    rec = decision.get("recommendation") or {}
    if isinstance(rec, str):
        rec = json.loads(rec)
    project_id = rec.get("project_id") or decision.get("project_id")
    payload = {k: rec.get(k) for k in ("resource_id", "hook", "digest")}
    payload["actor"] = actor
    return enqueue_job(connection, project_id, JOB_KIND, payload, max_attempts=3,
                       dedupe_key=f"infra-op:{payload['resource_id']}:{payload['hook']}:{payload['digest']}",
                       data_boundary="internal")


def execute_job(engine: Any, settings: Any, *, job_id: str, worker_id: str, lease_token: str) -> dict[str, Any]:
    """The worker's leased `infra.operation`, run here (the API holds the vault and host keys)."""
    from ..services.jobs import JobLeaseService, LeaseNotFoundError
    jobs = JobLeaseService(settings)
    with engine.begin() as c:
        try:
            job = jobs.require_active_lease(c, worker_id, job_id, lease_token)
        except LeaseNotFoundError as exc:
            return {"status": "failed", "retry": False, "error": f"lease: {exc}"}
        payload = _json(job.get("payload"))
        project_id = job["project_id"]
        row = ledger_row(c, project_id, str(payload.get("resource_id")))
        registry = Registry.load(c)
    if row is None:
        return {"status": "failed", "retry": False, "error": "no such resource"}
    hook = Hook(payload["hook"])
    if operation_digest(row, hook) != payload.get("digest"):
        return {"status": "failed", "retry": False,
                "error": "the resource or its plugin changed after this operation was approved"}
    reg = registry.get(row["plugin_id"], row["plugin_version"])
    if reg is None or not reg.usable:
        return {"status": "failed", "retry": False, "error": "the plugin is not enabled"}

    def lost() -> bool:
        try:
            with engine.begin() as c2:
                jobs.require_active_lease(c2, worker_id, job_id, lease_token)
            return False
        except Exception:  # noqa: BLE001 — a lost or cancelled lease stops the operation
            return True

    result = run_hook(engine, project_id=project_id, app_key=row["app_key"], reg=reg,
                      request=_request_from_row(row), hook=hook, plan=None, digest=payload["digest"],
                      actor_id=payload.get("actor"), job_id=job_id, is_cancelled=lost)
    out = {"status": "succeeded" if result.ok else "failed", "outcome": result.status.value,
           "resource_id": row["resource_id"], "hook": hook.value,
           "evidence": [e.model_dump(mode="json") for e in result.evidence],
           "errors": [e.model_dump(mode="json") for e in result.errors]}
    if not result.ok:
        out["retry"] = any(e.retryable for e in result.errors)
        out["error"] = result.errors[0].message if result.errors else result.status.value
    return out
