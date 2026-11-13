"""/v1/extensions and /v1/ext: general-purpose extensions (boss.extension/v1, BOSSAPP-861).

Install-wide (like /v1/infra/plugins): an extension is installed once and enabled per project.

    GET    /v1/extensions?project_id=            installed extensions, trust verdict, enabled here?
    POST   /v1/extensions/preview                {source}: manifest + consent + computed checksum
    POST   /v1/extensions/install                {source, checksum, accept[]}: checksum + consent
    DELETE /v1/extensions/{id}                   uninstall (its tools' servers, cursors, state)
    POST   /v1/extensions/{id}/enable            {project_id}
    POST   /v1/extensions/{id}/disable           {project_id}
    GET    /v1/extensions/{id}/webhooks          each hook's URL and secret (to configure the sender)
    GET    /v1/extensions/slots?type=&project_id=  console slots of enabled, trusted extensions
    GET    /v1/ext/{id}/ui/{slot}?project_id=    the slot's frame document (text/html + sandbox CSP)
    GET    /v1/ext/{id}/ui/{slot}/context        what the frame's getContext() answers
    POST   /v1/ext/{id}/webhook/{hook}           PUBLIC: the per-hook HMAC signature is the key
    POST   /v1/ext/{id}/projects/{p}/mcp         PUBLIC: the per-install bearer (HMAC-derived) is
                                                 the key; BOSS's own MCPClient is the only caller

Permissions mirror the plugin registry's: reading needs infra:read; installing, enabling,
disabling, removing and the webhook secrets need instance:manage (BOSSAPP-872: running the
install is an instance admin's job, apart from any org's data; Owner/Admin hold it until the
first instance admin is named). Install / enable / disable / uninstall are audit events
(extension.*).
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from boss_engine.identity import P, Scope, audit, permit
from boss_engine.identity.declarations import PublicReason, public


def _refuse(status: int, error: str, detail: str = "") -> JSONResponse:
    body: dict[str, Any] = {"error": error}
    if detail:
        body["detail"] = detail
    return JSONResponse(body, status_code=status)


def _context(request: Request):
    try:
        from apps.api.app import identity_guard

        return identity_guard.request_auth(request)
    except Exception:  # noqa: BLE001 — a bare test app has no identity middleware
        return None


def _actor(request: Request) -> str:
    ctx = _context(request)
    return ctx.principal.id if ctx is not None else "operator"


def _audit(request: Request, db: Any, event_type: str, *, extension_id: str, permission: str,
           details: dict[str, Any]) -> None:
    ctx = _context(request)
    if ctx is None:
        return
    audit.record(audit.SecurityEvent.from_context(
        ctx, event_type, audit.Outcome.SUCCESS, target_type="extension", target_id=extension_id,
        permission=permission, scope="org", details=details), db=db)


def verify_extension_webhook(*_a: Any, **_k: Any) -> Any:
    """The webhook's protection: extensions.webhooks.verify (HMAC-SHA256 of the raw body with the
    per-hook secret), called by the handler before anything reaches the extension."""
    from boss_engine.extensions.webhooks import verify

    return verify(*_a, **_k)


def verify_extension_mcp_bearer(*_a: Any, **_k: Any) -> Any:
    """The MCP endpoint's protection: extensions.tools.authorized (the per-install, per-project
    bearer, HMAC(server key, extension, project, install nonce)), compared in constant time."""
    from boss_engine.extensions.tools import authorized

    return authorized(*_a, **_k)


def build_extensions_router(engine_getter: Optional[Callable[[], Any]] = None,
                            settings_getter: Optional[Callable[[], Any]] = None) -> APIRouter:
    from boss_engine.extensions import registry, tools, ui, webhooks

    router = APIRouter(tags=["extensions"])

    def _engine() -> Any:
        if engine_getter is not None:
            return engine_getter()
        from boss_engine.db import get_engine

        return get_engine()

    def _settings() -> Any:
        if settings_getter is not None:
            return settings_getter()
        from boss_engine.config import get_settings

        return get_settings()

    def _load(extension_id: str):
        with _engine().begin() as c:
            return registry.load(c, extension_id)

    @router.get("/v1/extensions")
    @permit(P.INFRA_READ, scope=Scope.org())
    def list_extensions(project_id: Optional[str] = None) -> Any:
        with _engine().begin() as c:
            found = registry.list_installed(c)
        return {"extensions": [e.view(project_id) for e in found]}

    @router.post("/v1/extensions/preview")
    @permit(P.INSTANCE_MANAGE, scope=Scope.org(), readonly_post=True)
    def preview_extension(payload: dict = Body(...)) -> Any:
        source = str((payload or {}).get("source") or "").strip()
        if not source:
            return _refuse(422, "source_required", "give the path of a bundle directory or archive")
        try:
            return {"preview": registry.preview(source)}
        except registry.ExtensionRefused as exc:
            return _refuse(exc.status, exc.code, str(exc))

    @router.post("/v1/extensions/install")
    @permit(P.INSTANCE_MANAGE, scope=Scope.org())
    def install_extension(request: Request, payload: dict = Body(...)) -> Any:
        body = payload or {}
        source = str(body.get("source") or "").strip()
        accept = body.get("accept")
        if not source or not isinstance(accept, list):
            return _refuse(422, "bad_request", "install needs source, checksum and accept (a list)")
        try:
            with _engine().begin() as db:
                ext = registry.install(db, _settings(), source, checksum=str(body.get("checksum") or ""),
                                       accept=[str(a) for a in accept], actor=_actor(request))
                _audit(request, db, "extension.installed", extension_id=ext.extension_id,
                       permission=P.INSTANCE_MANAGE.value,
                       details={"version": ext.version, "checksum": ext.approved_checksum,
                                "capabilities": [c["capability"] for c in ext.view()["capabilities"]]})
                return {"extension": ext.view()}
        except registry.ExtensionRefused as exc:
            return _refuse(exc.status, exc.code, str(exc))

    @router.delete("/v1/extensions/{extension_id}")
    @permit(P.INSTANCE_MANAGE, scope=Scope.org())
    def uninstall_extension(extension_id: str, request: Request) -> Any:
        try:
            with _engine().begin() as db:
                registry.uninstall(db, _settings(), extension_id, actor=_actor(request))
                _audit(request, db, "extension.uninstalled", extension_id=extension_id,
                       permission=P.INSTANCE_MANAGE.value, details={})
        except registry.ExtensionRefused as exc:
            return _refuse(exc.status, exc.code, str(exc))
        return {"ok": True}

    def _toggle(request: Request, extension_id: str, payload: Optional[dict], *, enabled: bool,
                permission: str) -> Any:
        project_id = str((payload or {}).get("project_id") or "").strip()
        if not project_id:
            return _refuse(422, "project_required", "name the project to turn it on or off in")
        try:
            with _engine().begin() as db:
                view = registry.set_enabled(db, _settings(), extension_id, project_id, enabled=enabled,
                                            actor=_actor(request))
                _audit(request, db, "extension.enabled" if enabled else "extension.disabled",
                       extension_id=extension_id, permission=permission,
                       details={"project_id": project_id, "version": view["version"]})
        except registry.ExtensionRefused as exc:
            return _refuse(exc.status, exc.code, str(exc))
        return {"extension": view}

    @router.post("/v1/extensions/{extension_id}/enable")
    @permit(P.INSTANCE_MANAGE, scope=Scope.org())
    def enable_extension(extension_id: str, request: Request, payload: Optional[dict] = Body(None)) -> Any:
        return _toggle(request, extension_id, payload, enabled=True, permission=P.INSTANCE_MANAGE.value)

    @router.post("/v1/extensions/{extension_id}/disable")
    @permit(P.INSTANCE_MANAGE, scope=Scope.org())
    def disable_extension(extension_id: str, request: Request, payload: Optional[dict] = Body(None)) -> Any:
        return _toggle(request, extension_id, payload, enabled=False, permission=P.INSTANCE_MANAGE.value)

    @router.get("/v1/extensions/{extension_id}/webhooks")
    @permit(P.INSTANCE_MANAGE, scope=Scope.org())
    def extension_webhooks(extension_id: str) -> Any:
        ext = _load(extension_id)
        if ext is None:
            return _refuse(404, "extension_unknown")
        try:
            manifest = ext.manifest
        except registry.ExtensionRefused as exc:
            return _refuse(exc.status, exc.code, str(exc))
        return {"webhooks": [{"name": w.name, "url": f"/v1/ext/{extension_id}/webhook/{w.name}",
                              "secret": webhooks.hook_secret(ext, w.name),
                              "signatureHeader": "X-Boss-Signature", "scheme": "sha256=<hex HMAC of the raw body>"}
                             for w in manifest.spec.webhooks]}

    @router.get("/v1/extensions/slots")
    @permit(P.INFRA_READ, scope=Scope.org())
    def extension_slots(type: str = "project_panel", project_id: Optional[str] = None) -> Any:
        if type not in ("project_panel", "settings_panel"):
            return _refuse(422, "slot_type", "type is project_panel or settings_panel")
        with _engine().begin() as c:
            found = registry.list_installed(c)
        out = []
        for ext in found:
            if type == "project_panel" and not ext.enabled_in(project_id):
                continue
            out.extend(ui.slots(ext, type))
        return {"slots": out}

    @router.get("/v1/ext/{extension_id}/ui/{slot}")
    @permit(P.INFRA_READ, scope=Scope.org())
    def extension_frame(extension_id: str, slot: str, project_id: Optional[str] = None) -> Any:
        ext = _load(extension_id)
        if ext is None:
            return _refuse(404, "extension_unknown")
        try:
            html = ui.document(ext, slot)
        except registry.ExtensionRefused as exc:
            return _refuse(exc.status, exc.code, str(exc))
        return HTMLResponse(html, headers=ui.headers())

    @router.get("/v1/ext/{extension_id}/ui/{slot}/context")
    @permit(P.INFRA_READ, scope=Scope.org())
    def extension_frame_context(extension_id: str, slot: str, project_id: Optional[str] = None) -> Any:
        ext = _load(extension_id)
        if ext is None:
            return _refuse(404, "extension_unknown")
        try:
            return {"context": ui.context(_engine(), ext, slot, project_id)}
        except registry.ExtensionRefused as exc:
            return _refuse(exc.status, exc.code, str(exc))

    @router.post("/v1/ext/{extension_id}/webhook/{hook}")
    @public(reason=PublicReason.HMAC_SIGNED_WEBHOOK, verifier=verify_extension_webhook)
    async def extension_webhook(extension_id: str, hook: str, request: Request) -> Any:
        from starlette.concurrency import run_in_threadpool

        body = await request.body()
        headers = dict(request.headers)

        def run() -> Any:
            # The broker call blocks (a child process); never on the event loop.
            return webhooks.receive(_engine(), _load(extension_id), hook, body, headers)

        status, answer = await run_in_threadpool(run)
        return JSONResponse(answer, status_code=status)

    @router.post("/v1/ext/{extension_id}/projects/{project_id}/mcp")
    @public(reason=PublicReason.HMAC_SIGNED_WEBHOOK, verifier=verify_extension_mcp_bearer)
    async def extension_mcp(extension_id: str, project_id: str, request: Request) -> Any:
        import json

        from starlette.concurrency import run_in_threadpool

        try:
            payload = json.loads(await request.body() or b"{}")
        except ValueError:
            return _refuse(400, "not_json")
        status, answer = await run_in_threadpool(
            tools.handle, _engine(), extension_id, project_id,
            request.headers.get("authorization"), payload)
        if answer is None:
            return Response(status_code=status)
        return JSONResponse(answer, status_code=status)

    return router
