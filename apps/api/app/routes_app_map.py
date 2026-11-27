"""/v1 App Map endpoints (BOSSAPP-555, BOSSAPP-556).

Routes:
  GET  /v1/projects/{project_id}/app-maps            repository:read    list project's stored App Maps
  GET  /v1/apps/{app_key}/app-map?project_id=        repository:read    get one app's stored App Map
  POST /v1/projects/{project_id}/app-maps/scan       repository:manage  scan repo & propose App Map(s)
  PUT  /v1/apps/{app_key}/app-map                    repository:manage  confirm / edit stored App Map
  POST /v1/apps/{app_key}/app-map/baseline           repository:manage  run baseline install+test verify
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from boss_engine.db import begin
from boss_engine.identity import P, Scope, audit, permit
from boss_engine.services import app_map, app_map_scanner


def _refuse(error: app_map.AppMapError) -> JSONResponse:
    return JSONResponse(error.body(), status_code=error.status)


def _context(request: Request):
    from apps.api.app import identity_guard

    return identity_guard.request_auth(request)


def _actor(request: Request, fallback: str = "operator") -> str:
    ctx = _context(request)
    return ctx.principal.id if ctx is not None else fallback


def _audit(
    request: Request,
    db: Any,
    event_type: str,
    *,
    target_type: str,
    target_id: str,
    permission: str,
    project_id: str,
    details: dict[str, Any],
) -> None:
    ctx = _context(request)
    if ctx is None:
        return
    audit.record(
        audit.SecurityEvent.from_context(
            ctx,
            event_type,
            audit.Outcome.SUCCESS,
            target_type=target_type,
            target_id=target_id,
            permission=permission,
            scope=f"project:{project_id}",
            details=details,
        ),
        db=db,
    )


def _allowed_scan_roots() -> list[Path]:
    raw = str(os.environ.get("BOSS_APP_MAP_SCAN_ROOTS") or "").strip()
    roots: list[Path] = []
    if raw:
        for part in raw.split(os.pathsep):
            p = part.strip()
            if p:
                try:
                    roots.append(Path(p).resolve())
                except OSError:
                    continue
    return roots


def _resolve_checkout_path(
    db: Any,
    project_id: str,
    *,
    repository: Optional[str] = None,
    requested_path: Optional[str] = None,
) -> Optional[Path]:
    """Resolve a repository checkout path for scanning or baseline verification.

    Never allows arbitrary filesystem traversal: an explicit `requested_path` is accepted ONLY
    when it lies inside an administrator-configured `BOSS_APP_MAP_SCAN_ROOTS` directory.
    Otherwise resolves the project's managed checkout via `ambient.dossier.checkout_for`.
    """
    if requested_path:
        try:
            cand = Path(str(requested_path)).resolve()
        except OSError as exc:
            raise app_map.AppMapError("invalid_repo_path", f"Invalid repository path: {exc}") from None
        allowed = _allowed_scan_roots()
        if not allowed or not any(cand == r or r in cand.parents for r in allowed):
            raise app_map.AppMapError(
                "repo_path_not_allowed",
                "Scanning an arbitrary filesystem path is not allowed. Connect the repository to "
                "this project or configure BOSS_APP_MAP_SCAN_ROOTS.",
                status=403,
            )
        if not cand.is_dir():
            raise app_map.AppMapError("repo_path_missing", "Repository checkout directory does not exist.", status=404)
        return cand

    try:
        from boss_engine.config import get_settings
        from boss_engine.services.ambient import dossier as dossier_service

        _full, checkout, _files = dossier_service.checkout_for(
            db, get_settings(), project_id, repository
        )
        if checkout is not None and Path(checkout).is_dir():
            return Path(checkout)
    except Exception:  # noqa: BLE001 — checkout not materialized on this host
        return None
    return None


def build_app_map_router() -> APIRouter:
    router = APIRouter(tags=["app-map"])

    @router.get("/v1/projects/{project_id}/app-maps")
    @permit(P.REPOSITORY_READ, scope=Scope.project(path="project_id"))
    def list_project_app_maps(project_id: str) -> Any:
        try:
            with begin() as db:
                checkout = _resolve_checkout_path(db, project_id)
                items = app_map.list_app_maps(db, project_id, repo_root=checkout)
                return {"project_id": project_id, "items": items}
        except app_map.AppMapError as exc:
            return _refuse(exc)

    @router.get("/v1/apps/{app_key}/app-map")
    @permit(P.REPOSITORY_READ, scope=Scope.project(query="project_id"))
    def get_app_map(app_key: str, project_id: str) -> Any:
        try:
            with begin() as db:
                entry = app_map.load_app_map(db, project_id, app_key=app_key)
                if entry is None:
                    return {
                        "project_id": project_id,
                        "app_key": app_key,
                        "app_map": None,
                        "status": "unmapped",
                        "message": app_map.NO_MAP_REFUSAL,
                    }
                out = entry.as_dict()
                checkout = _resolve_checkout_path(db, project_id, repository=entry.repository)
                drift = None
                if checkout is not None:
                    drift = app_map.detect_manifest_drift(entry, checkout, rescan=False)
                    if drift["drifted"] and out["status"] == "confirmed":
                        out["status"] = "stale"
                return {
                    "project_id": project_id,
                    "app_key": entry.app_key,
                    "app_map": out,
                    "status": out["status"],
                    "drift": drift,
                }
        except app_map.AppMapError as exc:
            return _refuse(exc)

    @router.post("/v1/projects/{project_id}/app-maps/scan")
    @permit(P.REPOSITORY_MANAGE, scope=Scope.project(path="project_id"))
    def scan_project_app_maps(project_id: str, request: Request, payload: dict = Body(default={})) -> Any:
        try:
            with begin() as db:
                repository = str(payload.get("repository") or "").strip() or None
                binding_id = str(payload.get("binding_id") or "").strip() or None
                requested_path = str(payload.get("repo_path") or "").strip() or None
                persist = bool(payload.get("persist", True))
                checkout = _resolve_checkout_path(
                    db, project_id, repository=repository, requested_path=requested_path
                )
                if checkout is None:
                    return JSONResponse(
                        {
                            "error": "checkout_unavailable",
                            "message": (
                                "No local checkout is available for this project's repository yet. "
                                "Sync or clone the repository first, or edit the App Map directly."
                            ),
                        },
                        status=409,
                    )
                scan = app_map_scanner.scan_repository(checkout)
                stored_items: list[dict[str, Any]] = []
                drifted_apps: list[dict[str, Any]] = []
                actor = _actor(request)

                # Detect drift against any already-stored App Maps for this project
                for existing_dict in app_map.list_app_maps(db, project_id):
                    existing_entry = app_map.AppMapEntry.from_dict(existing_dict)
                    if existing_entry is None:
                        continue
                    drift = app_map.detect_manifest_drift(existing_entry, checkout, rescan=True)
                    if drift["drifted"]:
                        drifted_apps.append({
                            "app_key": existing_entry.app_key,
                            "root_path": existing_entry.root_path,
                            **drift,
                        })
                        if persist:
                            from dataclasses import replace as _dc_replace

                            stale_entry = _dc_replace(existing_entry, status="stale")
                            app_map.upsert_app_map(
                                db, project_id, stale_entry,
                                binding_id=binding_id or existing_entry.binding_id,
                                actor=actor, confirm=False,
                            )

                if persist and scan.selected is not None:
                    saved = app_map.upsert_app_map(
                        db, project_id, scan.selected,
                        binding_id=binding_id, actor=actor,
                        confirm=(scan.source == "boss_apps_yaml"),
                    )
                    stored_items.append(saved.as_dict())

                _audit(
                    request, db, "app_map.scanned",
                    target_type="project", target_id=project_id,
                    permission=P.REPOSITORY_MANAGE.value, project_id=project_id,
                    details={
                        "status": scan.status,
                        "source": scan.source,
                        "apps_found": [a.root_path for a in scan.apps],
                        "drifted": [d["app_key"] for d in drifted_apps],
                    },
                )
                return {
                    "project_id": project_id,
                    **scan.as_dict(),
                    "stored": stored_items,
                    "drifted": drifted_apps,
                }
        except app_map.AppMapError as exc:
            return _refuse(exc)

    @router.put("/v1/apps/{app_key}/app-map")
    @permit(P.REPOSITORY_MANAGE, scope=Scope.project(body="project_id"))
    def put_app_map(app_key: str, request: Request, payload: dict = Body(...)) -> Any:
        project_id = str(payload.get("project_id") or "").strip()
        if not project_id:
            return JSONResponse({"error": "missing_project_id", "message": "project_id is required."}, status_code=422)
        try:
            with begin() as db:
                requested_path = str(payload.get("repo_path") or "").strip() or None
                checkout = _resolve_checkout_path(
                    db, project_id,
                    repository=str(payload.get("repository") or "").strip() or None,
                    requested_path=requested_path,
                )
                actor = _actor(request)
                saved = app_map.confirm_app_map(
                    db, project_id, app_key, payload, repo_root=checkout, actor=actor
                )
                _audit(
                    request, db, "app_map.confirmed" if saved.status == "confirmed" else "app_map.updated",
                    target_type="app_map", target_id=saved.app_key,
                    permission=P.REPOSITORY_MANAGE.value, project_id=project_id,
                    details={
                        "root_path": saved.root_path,
                        "workspace_root": saved.workspace_root,
                        "status": saved.status,
                        "fingerprint": saved.fingerprint,
                    },
                )
                return {
                    "project_id": project_id,
                    "app_key": saved.app_key,
                    "app_map": saved.as_dict(),
                    "status": saved.status,
                }
        except app_map.AppMapError as exc:
            return _refuse(exc)

    @router.post("/v1/apps/{app_key}/app-map/baseline")
    @permit(P.REPOSITORY_MANAGE, scope=Scope.project(body="project_id"))
    def run_app_map_baseline(app_key: str, request: Request, payload: dict = Body(...)) -> Any:
        project_id = str(payload.get("project_id") or "").strip()
        if not project_id:
            return JSONResponse({"error": "missing_project_id", "message": "project_id is required."}, status_code=422)
        try:
            with begin() as db:
                entry = app_map.load_app_map(db, project_id, app_key=app_key)
                if entry is None:
                    return JSONResponse(
                        {"error": "app_map_not_found", "message": app_map.NO_MAP_REFUSAL},
                        status_code=404,
                    )
                requested_path = str(payload.get("repo_path") or "").strip() or None
                checkout = _resolve_checkout_path(
                    db, project_id, repository=entry.repository, requested_path=requested_path
                )
                if checkout is None:
                    return JSONResponse(
                        {
                            "error": "checkout_unavailable",
                            "message": "No local checkout is available to run baseline verification.",
                        },
                        status=409,
                    )
                commit_sha = str(payload.get("commit_sha") or "").strip() or None
                timeout_s = min(max(int(payload.get("timeout_seconds") or 300), 10), 900)
                actor = _actor(request)
                baseline = app_map.verify_baseline(
                    checkout, entry, db=db, project_id=project_id,
                    commit_sha=commit_sha, timeout_seconds=timeout_s, actor=actor,
                )
                _audit(
                    request, db, "app_map.baseline_run",
                    target_type="app_map", target_id=entry.app_key,
                    permission=P.REPOSITORY_MANAGE.value, project_id=project_id,
                    details={
                        "root_path": entry.root_path,
                        "status": baseline["status"],
                        "commit_sha": baseline["commit_sha"],
                        "duration_ms": baseline["duration_ms"],
                    },
                )
                updated = app_map.load_app_map(db, project_id, app_key=app_key)
                return {
                    "project_id": project_id,
                    "app_key": entry.app_key,
                    "baseline": baseline,
                    "app_map": updated.as_dict() if updated else entry.as_dict(),
                }
        except app_map.AppMapError as exc:
            return _refuse(exc)

    return router
