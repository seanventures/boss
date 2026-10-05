"""Infra's view endpoints and routed actions (infra-observability spec §6.2, §6.5).

One ETag'd view per screen, polled by the console through `usePolled`; an unchanged view is a
304. Every write keeps its existing route; the few new ones here route by kind so the console
has one button where it had three (`Deploy`, `Roll back`).

Mounted from main.py with the auth helpers injected, so this module does not import main.
Stream S1 owns this file.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from sqlalchemy import text

from boss_engine.db import begin


class InfraBody(BaseModel):
    project_id: str
    actor: Optional[str] = None


class TargetDeployBody(InfraBody):
    ref: Optional[str] = None
    #: registry-only extras (`More`): dollars, converted to micros server-side.
    expected_cost_usd: Optional[float] = None
    budget_reservation_id: Optional[str] = None
    release_packet: Optional[dict[str, Any]] = None


class TargetMoveBody(InfraBody):
    host_id: str


class AttentionActBody(InfraBody):
    id: str
    option: str


class HostRetireBody(InfraBody):
    confirm_name: str
    revoke_key: bool = False
    uninstall_docker: bool = False
    delete_record: bool = False
    detach_apps: bool = False


def build_infra_router(
    *,
    engine,
    settings: Callable[[], Any],
    require_write_actor: Callable[..., str],
    require_reader: Callable[..., None],
    reader_project: Callable[[str], str],
    viewer_of: Callable[[Optional[str]], Optional[str]],
    etagged: Callable[[Request, Response, dict], Any],
) -> APIRouter:
    from boss_engine.services import infra_view

    router = APIRouter(tags=["infra"])

    def _read(project_id: str, request: Request, response: Response, build: Callable[[Any], dict]):
        pid = reader_project(project_id)
        with begin(engine) as connection:
            require_reader(connection, pid)
            try:
                body = build(connection)
            except NotImplementedError as error:
                raise HTTPException(status_code=501, detail="Not built yet.") from error
            except LookupError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
        return etagged(request, response, jsonable_encoder(body))

    @router.get("/v1/infra/fleet")
    def infra_fleet(project_id: str, request: Request, response: Response,
                    viewer: Optional[str] = None):
        """Machines, what is placed on them, what is not placed, and what needs you (§4.3)."""
        return _read(project_id, request, response, lambda c: infra_view.fleet(
            c, settings(), project_id.strip(), viewer=viewer_of(viewer)))

    @router.get("/v1/infra/hosts/{host_id}")
    def infra_host(host_id: str, project_id: str, request: Request, response: Response):
        return _read(project_id, request, response, lambda c: infra_view.host_detail(
            c, settings(), project_id.strip(), host_id))

    @router.get("/v1/infra/targets/{target_id}")
    def infra_target(target_id: str, project_id: str, request: Request, response: Response):
        return _read(project_id, request, response, lambda c: infra_view.target_detail(
            c, settings(), project_id.strip(), target_id))

    @router.get("/v1/infra/deploys")
    def infra_deploys(project_id: str, request: Request, response: Response,
                      state: Optional[str] = None, app: Optional[str] = None,
                      env: Optional[str] = None, cursor: Optional[str] = None, limit: int = 30,
                      viewer: Optional[str] = None):
        return _read(project_id, request, response, lambda c: infra_view.deploy_rows(
            c, settings(), project_id.strip(), state=state, app=app, env=env, cursor=cursor,
            limit=max(1, min(int(limit), 100)), viewer=viewer_of(viewer)))

    @router.get("/v1/infra/deploys/{ref}")
    def infra_deploy(ref: str, project_id: str, request: Request, response: Response):
        return _read(project_id, request, response, lambda c: infra_view.deploy_detail(
            c, settings(), project_id.strip(), ref))

    @router.get("/v1/infra/previews")
    def infra_previews(project_id: str, request: Request, response: Response):
        return _read(project_id, request, response, lambda c: infra_view.preview_rows(
            c, settings(), project_id.strip()))

    @router.get("/v1/infra/cloud")
    def infra_cloud(project_id: str, request: Request, response: Response,
                    view: str = "accounts", viewer: Optional[str] = None,
                    provider: Optional[str] = None, account: Optional[str] = None,
                    env: Optional[str] = None, region: Optional[str] = None,
                    service: Optional[str] = None):
        filters = {k: v for k, v in {"provider": provider, "account": account, "env": env,
                                      "region": region, "service": service}.items() if v}
        # The provider profiles live in resource_plane, which loads the SSH adapter; the API
        # reads them so the view module never imports it (§6.8).
        from boss_engine.services.resource_plane import PROVIDER_CONNECTION_PROFILES

        return _read(project_id, request, response, lambda c: infra_view.cloud(
            c, settings(), project_id.strip(), view, viewer=viewer_of(viewer), filters=filters,
            profiles=PROVIDER_CONNECTION_PROFILES))

    # ── writes ───────────────────────────────────────────────────────────────────────────
    # Every write binds its actor through require_write_actor with the SAME permission the
    # existing route uses, and ends in the same engine function: these routes choose, they do
    # not re-implement.

    def _target(connection, project_id: str, target_id: str) -> dict:
        row = connection.execute(
            text("SELECT * FROM deployment_targets WHERE id = :i AND project_id = :p"),
            {"i": target_id, "p": project_id}).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="No such app target in this project.")
        return dict(row)

    def _stack_error(exc) -> HTTPException:
        return HTTPException(status_code=getattr(exc, "status_code", 422), detail=exc.to_dict())

    def _micros(dollars: Optional[float]) -> int:
        if dollars in (None, ""):
            return 0
        try:
            value = int(round(float(dollars) * 1_000_000))
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail="Cost must be a number of dollars.") \
                from error
        if value < 0:
            raise HTTPException(status_code=422, detail="Cost cannot be negative.")
        return value

    def _refuse_in_community() -> None:
        if infra_view.community(settings()):
            raise HTTPException(status_code=409, detail=infra_view.PAUSED_IN_COMMUNITY)

    @router.post("/v1/infra/targets/{target_id}/deploy")
    def infra_target_deploy(target_id: str, body: TargetDeployBody):
        """One Deploy button, routed by the target's kind (§6.5): a stack target plans a stack
        deployment, an app target a delivery deploy, a cloud-account target a registry run.
        Returns `{ref: "<kind>:<id>"}` for the deploy drawer."""
        pid = reader_project(body.project_id)
        with begin(engine) as connection:
            target = _target(connection, pid, target_id)
            actor = require_write_actor(connection, pid, body.actor, "providerops_manage")
        route = infra_view.target_kind_route(target)
        if route == "stack":
            from boss_engine.services.stack_deploy import StackDeployError, plan_stack_deploy

            try:
                deployment = plan_stack_deploy(
                    engine, settings(), project_id=pid, target_id=target_id, repository=None,
                    ref=(body.ref or "").strip() or "main", actor=actor)
            except StackDeployError as exc:
                raise _stack_error(exc)
            return {"ref": f"stack:{deployment['id']}", "kind": "stack",
                    "deployment": deployment}
        # The per-app container deploy and the registry run stay paused in Community, as the
        # console has always said (`Paused in Community`); now the server says it too.
        _refuse_in_community()
        cost = _micros(body.expected_cost_usd)
        if route == "registry":
            from boss_engine.services.resource_plane import (
                ProviderAccountNotFoundError, ResourcePlaneService,
            )

            with begin(engine) as connection:
                try:
                    item = ResourcePlaneService(settings()).plan_deployment_run(
                        connection, project_id=pid, deployment_target_id=target_id,
                        commit_ref=(body.ref or "").strip() or None,
                        release_packet=dict(body.release_packet or {}),
                        expected_cost_usd_micros=cost,
                        reservation_id=(body.budget_reservation_id or None), actor=actor)
                except (ProviderAccountNotFoundError, ValueError) as error:
                    raise HTTPException(status_code=422, detail=str(error)) from error
            run_id = item.get("deployment_run_id") or item.get("id")
            return {"ref": f"registry:{run_id}", "kind": "registry", "run": item}
        from .. import main as api_main
        from ..request_bodies import ManualDeployBody

        out = api_main.delivery_deploy_endpoint(ManualDeployBody(
            target_id=target_id, actor=actor, ref=(body.ref or None),
            expected_cost_usd_micros=cost, reservation_id=(body.budget_reservation_id or None)))
        return {"ref": f"delivery:{out.get('deployment_run_id')}", "kind": "delivery", **out}

    @router.post("/v1/infra/targets/{target_id}/move")
    def infra_target_move(target_id: str, body: TargetMoveBody):
        """Point an app at a machine (what POST /v1/deployment-hosts/{id}/attach does)."""
        from boss_engine.services.deployment_fleet import attach_target

        pid = reader_project(body.project_id)
        with begin(engine) as connection:
            _target(connection, pid, target_id)
            require_write_actor(connection, pid, body.actor, "task_create")
            try:
                return attach_target(connection, target_id=target_id, host_id=body.host_id)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

    def _rollback(pid: str, ref: str, claimed: Optional[str]) -> dict:
        try:
            kind, ident = infra_view._parse_ref(ref)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        if kind == "stack":
            from boss_engine.services.stack_deploy import (
                StackDeployError, get_stack_deployment, rollback_stack_deploy,
            )

            with begin(engine) as connection:
                current = get_stack_deployment(connection, ident)
                if not current or current["project_id"] != pid:
                    raise HTTPException(status_code=404, detail="No such deploy in this project.")
                actor = require_write_actor(connection, pid, claimed, "providerops_manage")
            try:
                deployment = rollback_stack_deploy(engine, settings(), deployment_id=ident,
                                                   actor=actor)
            except StackDeployError as exc:
                raise _stack_error(exc)
            return {"ref": f"stack:{deployment['id']}", "kind": "stack",
                    "deployment": deployment}
        if kind == "registry":
            raise HTTPException(status_code=409, detail=(
                "A registry run rolls back through its rollout, in the deploy drawer."))
        from boss_engine.services.delivery_pipeline import redeploy

        with begin(engine) as connection:
            run = connection.execute(
                text("SELECT id, deployment_target_id FROM deployment_runs "
                     "WHERE id = :i AND project_id = :p AND trigger IS NOT NULL"),
                {"i": ident, "p": pid}).mappings().first()
            if not run:
                raise HTTPException(status_code=404, detail="No such deploy in this project.")
            actor = require_write_actor(connection, pid, claimed, "providerops_manage")
            history = infra_view._run_history(connection, [run["deployment_target_id"]])
            source = infra_view.delivery_rollback_source(
                ident, history.get(run["deployment_target_id"]) or [])
        if source is None:
            raise HTTPException(status_code=409, detail=(
                "Roll back is offered only on a target's newest deploy when it failed and an "
                "earlier deploy succeeded."))
        _refuse_in_community()
        result = redeploy(engine, settings(), source, actor=actor, execute=False)
        if result.get("status") == "not_found":
            raise HTTPException(status_code=404, detail="No such deploy in this project.")
        if result.get("status") == "blocked":
            raise HTTPException(status_code=409, detail=result.get("reason")
                                or "Deployment policy blocked this run.")
        new_id = result.get("deployment_run_id")
        if new_id:
            with begin(engine) as connection:
                connection.execute(text("UPDATE deployment_runs SET trigger = 'rollback' "
                                        "WHERE id = :i"), {"i": new_id})
        return {"ref": f"delivery:{new_id}", "kind": "delivery", "rollback_of": ref,
                "redeploy_of": source, **result}

    @router.post("/v1/infra/deploys/{ref}/rollback")
    def infra_deploy_rollback(ref: str, body: InfraBody):
        """Roll back by kind: a stack deploy plans the previous healthy commit (the existing
        stack rollback); a delivery deploy redeploys the target's previous good run."""
        return _rollback(reader_project(body.project_id), ref, body.actor)

    @router.post("/v1/infra/hosts/{host_id}/retire")
    def infra_host_retire(host_id: str, body: HostRetireBody):
        """Retire a machine. The server checks the typed name too; then the existing retire
        path runs with the four options of the confirm sheet."""
        from boss_engine.services.deployment_fleet import (
            get_host, require_direct_host_write_policy, retire_host,
        )

        pid = reader_project(body.project_id)
        with begin(engine) as connection:
            host = get_host(connection, host_id)
            if not host or host.get("project_id") != pid:
                raise HTTPException(status_code=404, detail="No such machine in this project.")
            actor = require_write_actor(connection, pid, body.actor, "providerops_manage")
            if (body.confirm_name or "").strip() != str(host["name"]):
                raise HTTPException(status_code=422,
                                    detail="Type the machine's name to retire it.")
            try:
                require_direct_host_write_policy(settings())
                return retire_host(connection, settings(), host_id=host_id,
                                   revoke_key=body.revoke_key,
                                   remove_docker=body.uninstall_docker,
                                   purge=body.delete_record, force=body.detach_apps,
                                   actor=actor)
            except PermissionError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/v1/infra/previews/{preview_id}/restart")
    def infra_preview_restart(preview_id: str, body: InfraBody):
        """Restart a preview (operator decision §11.3): stop it, then start a fresh preview of
        the same build on the same substrate. Same permission as Stop."""
        from boss_engine.services.preview_service import start_preview_async, stop_preview

        pid = reader_project(body.project_id)
        with begin(engine) as connection:
            row = connection.execute(
                text("SELECT id, run_id, mode, status FROM app_previews "
                     "WHERE id = :i AND project_id = :p"), {"i": preview_id, "p": pid}
            ).mappings().first()
            if not row:
                raise HTTPException(status_code=404, detail="Preview not found.")
            actor = require_write_actor(connection, pid, body.actor, "task_create")
            if not row["run_id"]:
                raise HTTPException(status_code=409,
                                    detail="This preview has no build to start again.")
            live = [r[0] for r in connection.execute(
                text("SELECT id FROM app_previews WHERE run_id = :r "
                     "AND status IN ('running', 'starting')"), {"r": row["run_id"]})]
        stopped = []
        for prior in live:
            try:
                stop_preview(engine, prior, settings())
                stopped.append(prior)
            except ValueError:
                pass
        mode = row["mode"] if row["mode"] in ("process", "docker", "rootfs") else "auto"
        try:
            started = start_preview_async(engine, settings(), row["run_id"], mode=mode,
                                          actor=actor)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"id": started.get("id"), "restarted": preview_id, "stopped": stopped,
                "preview": started}

    @router.post("/v1/infra/attention/act")
    def infra_attention_act(body: AttentionActBody):
        """Press an infra card's answer: `rollback` (deploy_failed), `reverify`
        (host_unreachable) or `open` (any; returns `{open}`, the console href)."""
        pid = reader_project(body.project_id)
        with begin(engine) as connection:
            require_reader(connection, pid)
            items = infra_view.attention(connection, settings(), pid)
        item = next((i for i in items if i.get("id") == body.id and i.get("kind") == "infra"),
                    None)
        if item is None:
            raise HTTPException(status_code=404, detail="This no longer needs you.")
        offered = {a.get("key") for a in item.get("answers") or []}
        if body.option not in offered:
            raise HTTPException(status_code=409, detail="That is not offered on this card.")
        if body.option == "open":
            return {"open": item.get("open")}
        if body.option == "rollback":
            ref = str(item.get("open") or "").rpartition("deploy=")[2]
            return _rollback(pid, ref, body.actor)
        if body.option == "reverify":
            from boss_engine.services.deployment_fleet import get_host, verify_host

            host_id = str(body.id).rpartition(":")[2]
            with begin(engine) as connection:
                host = get_host(connection, host_id)
                if not host or host.get("project_id") != pid:
                    raise HTTPException(status_code=404, detail="No such machine.")
                require_write_actor(connection, pid, body.actor, "task_create")
                return verify_host(connection, settings(), host_id=host_id)
        raise HTTPException(status_code=409, detail="That is not offered on this card.")

    return router
