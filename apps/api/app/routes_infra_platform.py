"""/v1/infra/plugins: the infrastructure plugin registry (boss.plugin/v1).

The registry is install-wide (engine/boss_engine/platform/registry.py): plugins come from the
repository's plugins/ tree and enable/disable lives in `infra_plugin_state`. So every route is
org-scoped, not project-scoped.

Permissions:
  list, search, matrix   infra:read
  disable                infra:provision   turning a plugin off is the emergency stop; anyone who
                                           may provision infrastructure may pull it
  enable                 setup:manage      install-wide trust: enabling a community plugin lets
                                           its code run, so it is an operator decision
Enable and disable are audit events (infra_plugin.enabled / infra_plugin.disabled).

/v1/infra/platform/{resources,plans,operations}: what the platform provisioned
(project-scoped), the plan a deployment carries, and standalone lifecycle operations
(platform/operations.py; destroy and restore open a decision first). Responses carry binding
NAMES only, never a secret reference path or a value.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from boss_engine.db import begin

from .request_bodies import StackDeployExecuteBody  # job_id, worker_id, lease_token
from boss_engine.identity import P, Scope, audit, permit


def _refuse(status: int, error: str, detail: str = "") -> JSONResponse:
    body: dict[str, Any] = {"error": error}
    if detail:
        body["detail"] = detail
    return JSONResponse(body, status_code=status)


def _context(request: Request):
    from apps.api.app import identity_guard

    return identity_guard.request_auth(request)


def _actor(request: Request) -> Optional[str]:
    ctx = _context(request)
    return ctx.principal.id if ctx is not None else None


def _audit(request: Request, db: Any, event_type: str, *, plugin_id: str, permission: str,
           details: dict[str, Any]) -> None:
    ctx = _context(request)
    if ctx is None:
        return
    audit.record(audit.SecurityEvent.from_context(
        ctx, event_type, audit.Outcome.SUCCESS, target_type="infra_plugin", target_id=plugin_id,
        permission=permission, scope="org", details=details), db=db)


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").lower() in ("1", "true", "yes", "on")


def build_infra_platform_router() -> APIRouter:
    from boss_engine.platform.registry import Registry, RegistryError, set_state, support_matrix

    router = APIRouter(tags=["infra-platform"])

    @router.get("/v1/infra/plugins")
    @permit(P.INFRA_READ, scope=Scope.org())
    def list_plugins(capability: Optional[str] = None, feature: Optional[str] = None,
                     engine: Optional[str] = None, include_disabled: Optional[str] = "true") -> Any:
        with begin() as db:
            registry = Registry.load(db)
        if capability or feature or engine:
            features = [f for f in (feature or "").split(",") if f]
            found = registry.search(capability=capability or None, features=features,
                                    engine=engine or None,
                                    include_disabled=_truthy(include_disabled))
        else:
            found = [p for p in registry.plugins if _truthy(include_disabled) or p.usable]
        return {"plugins": [p.view() for p in found]}

    @router.get("/v1/infra/plugins/matrix")
    @permit(P.INFRA_READ, scope=Scope.org())
    def plugin_matrix() -> Any:
        with begin() as db:
            registry = Registry.load(db)
        return {"rows": support_matrix(registry)}

    def _set(request: Request, plugin_id: str, payload: Optional[dict], *, enabled: bool,
             permission: str) -> Any:
        pin = (payload or {}).get("pinnedVersion")
        if pin is not None and not isinstance(pin, str):
            return _refuse(422, "bad_pin", "pinnedVersion is a string")
        try:
            with begin() as db:
                registry = Registry.load(db)
                plugin = registry.get(plugin_id)
                if plugin is None:
                    return _refuse(404, "plugin_unknown", f"no plugin {plugin_id} is installed")
                view = set_state(db, plugin, enabled=enabled, actor_id=_actor(request),
                                 pinned_version=pin)
                _audit(request, db, "infra_plugin.enabled" if enabled else "infra_plugin.disabled",
                       plugin_id=plugin_id, permission=permission,
                       details={"version": plugin.version, "source": plugin.source.value,
                                "checksum": plugin.checksum})
        except RegistryError as exc:
            return _refuse(exc.status, exc.code, str(exc))
        return {"plugin": view}

    @router.post("/v1/infra/plugins/{plugin_id}/enable")
    @permit(P.INSTANCE_MANAGE, scope=Scope.org())
    def enable_plugin(plugin_id: str, request: Request, payload: Optional[dict] = Body(None)) -> Any:
        return _set(request, plugin_id, payload, enabled=True, permission=P.INSTANCE_MANAGE.value)

    @router.post("/v1/infra/plugins/{plugin_id}/disable")
    @permit(P.INSTANCE_MANAGE, scope=Scope.org())
    def disable_plugin(plugin_id: str, request: Request, payload: Optional[dict] = Body(None)) -> Any:
        return _set(request, plugin_id, payload, enabled=False, permission=P.INSTANCE_MANAGE.value)

    @router.get("/v1/infra/platform/resources")
    @permit(P.INFRA_READ, scope=Scope.project(query="project_id"))
    def list_infra_resources(project_id: str) -> Any:
        from boss_engine.platform.operations import list_resources
        with begin() as db:
            return {"resources": list_resources(db, project_id)}

    @router.get("/v1/infra/platform/plans")
    @permit(P.INFRA_READ, scope=Scope.project(query="project_id"))
    def get_infra_plan(project_id: str, deployment_id: str) -> Any:
        from boss_engine.platform.deploy import plan_for_deployment
        with begin() as db:
            row = plan_for_deployment(db, deployment_id)
        if row is None or row["project_id"] != project_id:
            return _refuse(404, "not_found", "this deployment has no infrastructure plan")
        return {"id": row["id"], "status": row["status"], "digest": row["digest"],
                "decision_id": row["decision_id"], "plan": row["plan"]}

    @router.post("/v1/infra/platform/resources/operations")
    @permit(P.INFRA_PROVISION, scope=Scope.project(body="project_id"))
    def request_infra_operation(request: Request, payload: dict = Body(...)) -> Any:
        from boss_engine.config import get_settings
        from boss_engine.platform.operations import OperationRefused, request_operation
        try:
            with begin() as db:
                return request_operation(db, get_settings(), project_id=str(payload.get("project_id")),
                                         resource_id=str(payload.get("resource_id") or ""),
                                         hook=str(payload.get("hook") or ""),
                                         actor=_actor(request) or "operator")
        except OperationRefused as exc:
            return _refuse(exc.status, exc.code, str(exc))

    @router.post("/v1/infra/platform/operations/execute")
    @permit(P.WORKER_LEASE)
    def execute_infra_operation(payload: StackDeployExecuteBody) -> Any:
        """The worker hands its leased `infra.operation` job here; fenced by the lease."""
        from apps.api.app.identity_guard import require_worker_principal
        from boss_engine.config import get_settings
        from boss_engine.db import get_engine
        from boss_engine.platform.operations import execute_job
        require_worker_principal()
        return execute_job(get_engine(), get_settings(), job_id=payload.job_id,
                           worker_id=payload.worker_id, lease_token=payload.lease_token)

    return router
