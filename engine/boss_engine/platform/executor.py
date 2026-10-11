"""Execution through BOSS's existing machinery (spec §4.3). No job system of its own.

    run_hook(...)      one hook on one resource, wrapped in a RECEIPT whose dedupe key is the
                       contract's idempotency key (plugin, version, resource, hook, plan digest):
                       a retried job or a second click replays the stored result instead of
                       running the plugin again. The receipt carries plugin version + checksum,
                       plan digest, outcome and evidence.
    apply_plan(...)    an approved plan, in dependency order. Called from inside an existing
                       leased job (the stack deploy's `deployment.stack_apply`, or the
                       `infra.operation` job for a standalone destroy/backup/restore), whose
                       lease check is `is_cancelled`: a lost lease or a cancelled job stops
                       between hooks.

Rules enforced HERE, whatever the caller or the plugin does:
  * a mutating hook runs only with a plan digest (the approved one);
  * destroy of an attached resource is refused without calling the plugin;
  * verify succeeds only with real `connection` evidence;
  * a result, error or log line carrying a value the broker saw is replaced by a failure
    (`secret_leak`) before anything stores it; logs pass through the redactor.
"""
from __future__ import annotations

import json
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from sqlalchemy import text

from .bindings import Redactor, VaultBroker, bind_attached
from .contract import (
    MUTATING_HOOKS, ContractError, Hook, HookContext, InfraPlugin, OperationResult, OperationStatus,
    ResourceMode, ResourcePlan, ResourceRequest, SecretBroker, idempotency_key,
)
from .planner import plan_digest
from .policy import destroy_refusal
from .registry import RegisteredPlugin, Registry

EFFECT_PREFIX = "infra."


class ApplyRefused(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def org_scope(connection: Any, project_id: str) -> tuple[str, str]:
    row = connection.execute(text(
        "SELECT w.org_id, p.workspace_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE p.id = :p"), {"p": project_id}).mappings().one()
    return row["org_id"], row["workspace_id"]


def ledger_row(connection: Any, project_id: str, resource_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text("SELECT * FROM infra_platform_resources WHERE project_id = :p AND resource_id = :r"),
                             {"p": project_id, "r": resource_id}).mappings().first()
    return dict(row) if row else None


def ledger_for_app(connection: Any, project_id: str, app_key: str, environment: str) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT * FROM infra_platform_resources WHERE project_id = :p AND app_key = :a AND environment = :e "
        "AND destroyed_at IS NULL ORDER BY created_at"), {"p": project_id, "a": app_key, "e": environment})
    return [dict(r) for r in rows.mappings().all()]


def _json(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else (v or {})


def _sanitize(result: OperationResult, redactor: Redactor, ctx: HookContext) -> OperationResult:
    body = result.model_dump_json()
    if redactor.leaks(body):
        return ctx.fail("secret_leak", "the plugin returned a secret value; the result was discarded")
    return result


def run_hook(engine: Any, *, project_id: str, app_key: str, reg: RegisteredPlugin, request: ResourceRequest,
             hook: Hook, plan: Optional[ResourcePlan], digest: Optional[str], actor_id: Optional[str],
             job_id: Optional[str] = None, is_cancelled: Callable[[], bool] = lambda: False,
             host: Any = None, plugin: Optional[InfraPlugin] = None,
             broker: Optional[SecretBroker] = None, redactor: Optional[Redactor] = None) -> OperationResult:
    from ..services.receipts import ReceiptService
    m = reg.manifest
    assert m is not None
    rid = request.resource_id(app_key)
    key = idempotency_key(m.id, m.version, rid, hook, digest)
    receipts = ReceiptService()
    with engine.begin() as c:
        org_id, workspace_id = org_scope(c, project_id)
        row = ledger_row(c, project_id, rid)
        mode = (row or {}).get("mode") or request.mode.value
        scope = {"project_id": project_id, "resource_id": rid, "hook": hook.value, "plugin_id": m.id,
                 "data_boundary": "internal"}
        payload = {"pluginId": m.id, "pluginVersion": m.version, "pluginChecksum": reg.checksum,
                   "planDigest": digest, "hook": hook.value, "resourceId": rid}
        receipt, _created = receipts.create_or_reuse(c, org_id, workspace_id, project_id, key,
                                                     EFFECT_PREFIX + hook.value, scope, payload,
                                                     created_by_job_id=job_id)
        if receipt.get("status") == "succeeded" and receipt.get("result"):
            # Idempotent replay: this exact operation already happened.
            return OperationResult.model_validate(_json(receipt["result"])["result"])
        if receipt.get("status") != "in_progress":
            c.execute(text("UPDATE receipts SET status = 'in_progress', error = NULL, updated_at = now() "
                           "WHERE id = :i"), {"i": receipt["id"]})
    # One redactor for the operation: the broker's, so every value it sees is stripped here.
    redactor = redactor or getattr(broker, "redactor", None) or Redactor()
    if broker is not None and getattr(broker, "redactor", None) is not None:
        broker.redactor = redactor
    log_lines: list[str] = []
    operation_id = f"op-{uuid.uuid4().hex[:16]}"

    def log(line: str) -> None:
        if len(log_lines) < 200:
            log_lines.append(redactor(line)[:500])

    allowed = [ref.get("secretRef") for ref in _json((row or {}).get("bindings")).values() if isinstance(ref, dict)]
    broker = broker or VaultBroker(engine, org_id=org_id, project_id=project_id, app_key=app_key, manifest=m,
                                   request=request, actor_id=actor_id, redactor=redactor, allowed=allowed)
    with tempfile.TemporaryDirectory(prefix="boss-infra-") as tmp:
        kw: dict[str, Any] = {"host": host} if host is not None else {}
        ctx = HookContext(hook=hook, operation_id=operation_id, idempotency_key=key, resource=request,
                          resource_id=rid, app_key=app_key, project_id=project_id, secrets=broker,
                          workdir=Path(tmp), plan=plan, plan_digest=digest,
                          state=dict(_json((row or {}).get("state"))),
                          labels={"boss.infra.resource": rid, "boss.infra.plugin": m.id, "boss.project": project_id},
                          is_cancelled=is_cancelled, log=log, plugin_id=m.id, plugin_version=m.version, **kw)
        started = time.monotonic()
        refusal = None
        if hook in MUTATING_HOOKS and not digest:
            refusal = "a mutating hook needs an approved plan digest"
        elif hook == Hook.DESTROY:
            refusal = destroy_refusal(mode)
        if refusal:
            result = ctx.result(OperationStatus.REFUSED, errors=[{"code": "policy", "message": refusal}])
        elif is_cancelled():
            result = ctx.result(OperationStatus.CANCELLED)
        else:
            from .runner import plugin_for
            result = _sanitize((plugin or plugin_for(reg)).run(ctx), redactor, ctx)
            if hook == Hook.VERIFY and result.ok and not result.connected:
                result = ctx.fail("verify_not_connected",
                                  "verify reported success without a real connection check",
                                  evidence=result.evidence)
        result = result.model_copy(update={"durationMs": int((time.monotonic() - started) * 1000),
                                           "pluginId": m.id, "pluginVersion": m.version})
    if hook == Hook.DESTROY and result.ok:
        # What BOSS bound for this resource goes with it: no orphan secrets after teardown.
        for ref in _json((row or {}).get("bindings")).values():
            if isinstance(ref, dict) and ref.get("secretRef"):
                broker.delete(ref["secretRef"])
    record = {"result": result.model_dump(mode="json"), "outcome": result.status.value,
              "evidence": [e.model_dump(mode="json") for e in result.evidence], "log": log_lines, **payload}
    with engine.begin() as c:
        if result.ok or result.status == OperationStatus.UNSUPPORTED:
            receipts.mark_succeeded(c, receipt["id"], operation_id, record)
        else:
            receipts.mark_failed(c, receipt["id"], record)
        _update_ledger(c, project_id=project_id, app_key=app_key, request=request, reg=reg, hook=hook,
                       result=result, digest=digest, mode=mode)
    return result


_STATUS_AFTER = {Hook.PROVISION: "provisioned", Hook.CONFIGURE: "configured", Hook.VERIFY: "verified",
                 Hook.DESTROY: "destroyed", Hook.RESTORE: "restored"}


def _update_ledger(c: Any, *, project_id: str, app_key: str, request: ResourceRequest, reg: RegisteredPlugin,
                   hook: Hook, result: OperationResult, digest: Optional[str], mode: str) -> None:
    from ..services.core import make_id
    rid = request.resource_id(app_key)
    row = ledger_row(c, project_id, rid)
    if row is None and hook not in (Hook.PROVISION, Hook.VERIFY, Hook.CONFIGURE):
        return
    status = (_STATUS_AFTER.get(hook, row["status"] if row else "planned") if result.ok
              else ("failed" if result.status == OperationStatus.FAILED else (row or {}).get("status", "planned")))
    state = {**_json((row or {}).get("state")), **result.state}
    bindings = {**_json((row or {}).get("bindings")),
                **{k: v.model_dump(mode="json") for k, v in result.bindings.items()}}
    evidence = [e.model_dump(mode="json") for e in result.evidence] or _json((row or {}).get("evidence")) or []
    params = {"p": project_id, "r": rid, "st": status, "state": json.dumps(state), "b": json.dumps(bindings),
              "ev": json.dumps(evidence), "d": digest, "op": result.operationId, "v": reg.version,
              "ck": reg.checksum, "destroyed": hook == Hook.DESTROY and result.ok,
              "revived": hook == Hook.PROVISION and result.ok}
    if row is None:
        c.execute(text(
            "INSERT INTO infra_platform_resources (id, project_id, resource_id, app_key, name, environment, capability, "
            "plugin_id, plugin_version, plugin_checksum, mode, status, public_access, state, bindings, evidence, "
            "last_plan_digest, last_operation_id) VALUES (:id, :p, :r, :a, :n, :e, :cap, :pid, :v, :ck, :m, :st, "
            ":pub, CAST(:state AS JSONB), CAST(:b AS JSONB), CAST(:ev AS JSONB), :d, :op)"),
            {**params, "id": make_id("ires"), "a": app_key, "n": request.name, "e": request.environment,
             "cap": request.capability, "pid": reg.id, "m": mode, "pub": request.public})
    else:
        c.execute(text(
            "UPDATE infra_platform_resources SET status = :st, state = CAST(:state AS JSONB), bindings = CAST(:b AS JSONB), "
            "evidence = CAST(:ev AS JSONB), last_plan_digest = COALESCE(:d, last_plan_digest), "
            "last_operation_id = :op, plugin_version = :v, plugin_checksum = :ck, updated_at = now(), "
            "destroyed_at = CASE WHEN :destroyed THEN now() WHEN :revived THEN NULL ELSE destroyed_at END "
            "WHERE project_id = :p AND resource_id = :r"), params)


def _plan_for(doc: dict[str, Any], name: str) -> Optional[ResourcePlan]:
    for r in doc.get("resources", []):
        if r.get("name") == name:
            fields = set(ResourcePlan.model_fields)
            return ResourcePlan.model_validate({k: v for k, v in r.items() if k in fields})
    return None


def check_plan(doc: dict[str, Any], approved_digest: Optional[str], registry: Registry) -> None:
    """The plan about to run is the one a person approved, built by the same plugin files."""
    if plan_digest(doc) != doc.get("digest"):
        raise ApplyRefused("plan_tampered", "the stored plan does not match its own digest")
    if approved_digest and approved_digest != doc.get("digest"):
        raise ApplyRefused("plan_changed", "the plan changed after it was approved; plan and approve again")
    if doc.get("refusals"):
        raise ApplyRefused("plan_refused", "; ".join(r["message"] for r in doc["refusals"]))
    for p in doc.get("plugins", []):
        reg = registry.get(p["id"], p["version"])
        if reg is None or not reg.usable:
            raise ApplyRefused("plugin_unavailable", f"{p['id']} {p['version']} is no longer installed and enabled")
        if reg.checksum != p["checksum"]:
            raise ApplyRefused("plugin_changed", f"{p['id']}'s files changed after the plan was approved")


def apply_plan(engine: Any, *, project_id: str, app_key: str, doc: dict[str, Any],
               requests: list[ResourceRequest], registry: Registry, actor_id: Optional[str],
               approved_digest: Optional[str] = None, job_id: Optional[str] = None,
               is_cancelled: Callable[[], bool] = lambda: False, host: Any = None,
               redactor: Optional[Redactor] = None,
               plugin_for: Optional[Callable[[RegisteredPlugin], InfraPlugin]] = None) -> list[OperationResult]:
    """Run an approved plan: provision → configure → verify per owned resource, bind + verify per
    attached one, destroy what was removed (owned only). Stops at the first failure."""
    check_plan(doc, approved_digest, registry)
    digest = doc["digest"]
    by_name = {r.name: r for r in requests}
    redactor = redactor or Redactor()
    results: list[OperationResult] = []

    def one(reg: RegisteredPlugin, req: ResourceRequest, hook: Hook) -> OperationResult:
        if is_cancelled():
            raise ApplyRefused("cancelled", "the job was cancelled or lost its lease")
        r = run_hook(engine, project_id=project_id, app_key=app_key, reg=reg, request=req, hook=hook,
                     plan=_plan_for(doc, req.name), digest=digest, actor_id=actor_id, job_id=job_id,
                     is_cancelled=is_cancelled, host=host, redactor=redactor,
                     plugin=plugin_for(reg) if plugin_for else None)
        results.append(r)
        if not (r.ok or (r.status == OperationStatus.UNSUPPORTED and hook == Hook.CONFIGURE)):
            raise ApplyRefused(f"{hook.value}_failed",
                               f"{req.name}: {hook.value} {r.status.value}"
                               + (f": {r.errors[0].message}" if r.errors else ""))
        return r

    for name in doc.get("order", []):
        req = by_name.get(name)
        if req is None:
            raise ApplyRefused("plan_mismatch", f"{name} is in the plan but not in the request")
        entry = next(p for p in doc["plugins"] if p["id"] == _plan_for(doc, name).pluginId)
        reg = registry.get(entry["id"], entry["version"])
        if req.mode == ResourceMode.ATTACHED:
            with engine.begin() as c:
                org_id, _ws = org_scope(c, project_id)
                refs = bind_attached(c, org_id=org_id, project_id=project_id, app_key=app_key, request=req,
                                     actor_id=actor_id)
                _update_ledger(c, project_id=project_id, app_key=app_key, request=req, reg=reg,
                               hook=Hook.CONFIGURE, digest=digest, mode="attached",
                               result=OperationResult(operationId="attach", resourceId=req.resource_id(app_key),
                                                      hook=Hook.CONFIGURE, status=OperationStatus.CONFIGURED,
                                                      bindings=refs))
            one(reg, req, Hook.VERIFY)
            continue
        one(reg, req, Hook.PROVISION)
        if reg.manifest.implements(Hook.CONFIGURE):
            one(reg, req, Hook.CONFIGURE)
        one(reg, req, Hook.VERIFY)
    for r in doc.get("resources", []):
        if not r.get("removed") or r.get("mode") == "attached":
            continue
        reg = registry.get(r["pluginId"], r["pluginVersion"]) or registry.get(r["pluginId"])
        if reg is None:
            raise ApplyRefused("plugin_unavailable", f"{r['pluginId']} is needed to destroy {r['name']}")
        with engine.begin() as c:
            row = ledger_row(c, project_id, r["resourceId"])
        req = ResourceRequest(name=r["name"], capability=(row or {}).get("capability") or "removed",
                              environment=(row or {}).get("environment") or "deploy")
        one(reg, req, Hook.DESTROY)
    return results


def env_values(engine: Any, *, project_id: str, app_key: str, environment: str = "deploy") -> dict[str, str]:
    """The binding values a deploy writes into the app's env file: resolved from the vault at
    write time, for the env names this app's live resources bound. The caller hides them."""
    from ..templates import secrets as app_secrets
    with engine.begin() as c:
        names = set()
        for row in ledger_for_app(c, project_id, app_key, environment):
            names |= set(_json(row.get("bindings")))
        if not names:
            return {}
        org_id, _ws = org_scope(c, project_id)
        values, _withheld = app_secrets.values_for_env(c, org_id=org_id, project_id=project_id,
                                                       app_key=app_key, env="deploy")
    return {k: v for k, v in values.items() if k in names}


__all__ = ["ApplyRefused", "ContractError", "apply_plan", "check_plan", "env_values", "run_hook"]
