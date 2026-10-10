"""/v1 multi-repo apps (docs/product/MULTIREPO.md). The engine is boss_engine.multirepo.

Registry (Settings › Repo and models › Repositories)
  GET    /v1/github/repos?project_id=              repository:read      the org's repositories
  POST   /v1/github/repos/refresh                  repository:register  re-read what GitHub lists
  PATCH  /v1/github/repos/{repo_id}                repository:register  tick / untick (checks first)
  PUT    /v1/github/repos/allowed                  repository:register  "Save · N allowed"
  POST   /v1/github/repos/{repo_id}/check          repository:register  run the three checks again
  POST   /v1/github/repos/recheck                  repository:register  "Recheck all" (allowed rows)

Project repositories (Factory › Projects › project › Repositories)
  GET    /v1/projects/{project_id}/repos           repository:read
  PUT    /v1/projects/{project_id}/repos/{repo_id} repository:manage    add (registry id) or update
  DELETE /v1/projects/{project_id}/repos/{repo_id} repository:manage    refused while an app uses it
  PUT    /v1/projects/{project_id}/repo-mode       repository:manage    One repo / Several repos
         (SPEC.md says PATCH /v1/projects/:id {repo_mode}; the project's own PATCH stays its own)

Components
  GET    /v1/apps/{app_key}/components?project_id= repository:read
  PUT    /v1/apps/{app_key}/components             repository:manage    whole list, validated
  POST   /v1/projects/{project_id}/suggest-components  repository:read  mapping + reasons
  GET    /v1/apps/{app_key}/consent?project_id=    repository:read      what the consent sheet shows

`repo_id` in a project path is the registry row (ghr_…) or the project's binding id. Every refusal
is {"error", "message"} with the status the engine chose. Registry changes are audit events.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from boss_engine.db import begin
from boss_engine.identity import P, Scope, audit, permit

SPEC = "docs/product/MULTIREPO.md"


def _refuse(error: Any) -> JSONResponse:
    return JSONResponse(error.body(), status_code=error.status)


def _context(request: Request):
    from apps.api.app import identity_guard

    return identity_guard.request_auth(request)


def _actor(request: Request, fallback: str = "operator") -> str:
    ctx = _context(request)
    return ctx.principal.id if ctx is not None else fallback


def _audit(request: Request, db: Any, event_type: str, *, target_type: str, target_id: str, permission: str,
           project_id: str, details: dict[str, Any]) -> None:
    ctx = _context(request)
    if ctx is None:
        return
    audit.record(audit.SecurityEvent.from_context(
        ctx, event_type, audit.Outcome.SUCCESS, target_type=target_type, target_id=target_id,
        permission=permission, scope=f"project:{project_id}", details=details), db=db)


def _settings():
    from boss_engine.config import get_settings

    return get_settings()


def build_multirepo_router() -> APIRouter:
    from boss_engine.multirepo import components, grants, project_repos, registry
    from boss_engine.multirepo.common import MultiRepoError

    router = APIRouter(tags=["multirepo"])

    # ── registry ──────────────────────────────────────────────────────────────────────────────

    @router.get("/v1/github/repos")
    @permit(P.REPOSITORY_READ, scope=Scope.project(query="project_id"))
    def list_repos(project_id: str) -> Any:
        try:
            with begin() as db:
                return registry.list_registry(db, project_id)
        except MultiRepoError as exc:
            return _refuse(exc)

    @router.post("/v1/github/repos/refresh")
    @permit(P.REPOSITORY_REGISTER, scope=Scope.project(body="project_id"))
    def refresh_repos(request: Request, payload: dict = Body(...)) -> Any:
        project_id = str(payload.get("project_id") or "")
        try:
            with begin() as db:
                result = registry.refresh(db, _settings(), project_id, actor=_actor(request))
                return {**registry.list_registry(db, project_id), "refreshed": result}
        except MultiRepoError as exc:
            return _refuse(exc)

    @router.patch("/v1/github/repos/{repo_id}")
    @permit(P.REPOSITORY_REGISTER, scope=Scope.project(body="project_id"))
    def patch_repo(repo_id: str, request: Request, payload: dict = Body(...)) -> Any:
        project_id = str(payload.get("project_id") or "")
        if not isinstance(payload.get("allowed"), bool):
            return JSONResponse({"error": "bad_request", "message": "Say allowed: true or false."}, status_code=422)
        try:
            with begin() as db:
                row = registry.set_allowed(db, _settings(), project_id, repo_id, payload["allowed"],
                                           actor=_actor(request))
                _audit(request, db, "repository.allowed" if payload["allowed"] else "repository.disallowed",
                       target_type="github_repo", target_id=repo_id, permission=P.REPOSITORY_REGISTER.value,
                       project_id=project_id, details={"repository": row["full_name"], "state": row["state"]})
                return row
        except MultiRepoError as exc:
            return _refuse(exc)

    @router.put("/v1/github/repos/allowed")
    @permit(P.REPOSITORY_REGISTER, scope=Scope.project(body="project_id"))
    def save_allowed(request: Request, payload: dict = Body(...)) -> Any:
        project_id = str(payload.get("project_id") or "")
        ids = payload.get("allowed_ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            return JSONResponse({"error": "bad_request", "message": "allowed_ids is a list of repository ids."},
                                status_code=422)
        try:
            with begin() as db:
                before = {i["id"] for i in registry.list_registry(db, project_id)["items"] if i["allowed"]}
                out = registry.save_allowed(db, _settings(), project_id, ids, actor=_actor(request))
                after = {i["id"] for i in out["items"] if i["allowed"]}
                for rid, kind in [(r, "repository.allowed") for r in sorted(after - before)] + \
                                 [(r, "repository.disallowed") for r in sorted(before - after)]:
                    name = next((i["full_name"] for i in out["items"] if i["id"] == rid), rid)
                    _audit(request, db, kind, target_type="github_repo", target_id=rid,
                           permission=P.REPOSITORY_REGISTER.value, project_id=project_id,
                           details={"repository": name})
                return out
        except MultiRepoError as exc:
            return _refuse(exc)

    @router.post("/v1/github/repos/recheck")
    @permit(P.REPOSITORY_REGISTER, scope=Scope.project(body="project_id"))
    def recheck_all(request: Request, payload: dict = Body(...)) -> Any:
        project_id = str(payload.get("project_id") or "")
        try:
            with begin() as db:
                for item in registry.list_registry(db, project_id)["items"]:
                    if item["allowed"]:
                        registry.check(db, _settings(), project_id, item["id"], actor=_actor(request))
                return registry.list_registry(db, project_id)
        except MultiRepoError as exc:
            return _refuse(exc)

    @router.post("/v1/github/repos/{repo_id}/check")
    @permit(P.REPOSITORY_REGISTER, scope=Scope.project(body="project_id"))
    def check_repo(repo_id: str, request: Request, payload: dict = Body(...)) -> Any:
        project_id = str(payload.get("project_id") or "")
        try:
            with begin() as db:
                return registry.check(db, _settings(), project_id, repo_id, actor=_actor(request))
        except MultiRepoError as exc:
            return _refuse(exc)

    # ── project repositories ──────────────────────────────────────────────────────────────────

    @router.get("/v1/projects/{project_id}/repos")
    @permit(P.REPOSITORY_READ, scope=Scope.project(path="project_id"))
    def project_repos_list(project_id: str) -> Any:
        try:
            with begin() as db:
                return project_repos.list_project_repos(db, project_id)
        except MultiRepoError as exc:
            return _refuse(exc)

    @router.put("/v1/projects/{project_id}/repos/{repo_id}")
    @permit(P.REPOSITORY_MANAGE, scope=Scope.project(path="project_id"))
    def project_repo_put(project_id: str, repo_id: str, request: Request, payload: dict = Body(default={})) -> Any:
        try:
            with begin() as db:
                binding_id = _binding_for(db, project_id, repo_id)
                if binding_id is None:
                    project_repos.add(db, _settings(), project_id, registry_repo_id=repo_id,
                                      role_hint=payload.get("role_hint"), base_branch=payload.get("base_branch"),
                                      actor=_actor(request))
                else:
                    project_repos.update(db, project_id, binding_id,
                                         role_hint=payload["role_hint"] if "role_hint" in payload else ...,
                                         base_branch=payload.get("base_branch") or ..., actor=_actor(request))
                return project_repos.list_project_repos(db, project_id)
        except MultiRepoError as exc:
            return _refuse(exc)

    @router.delete("/v1/projects/{project_id}/repos/{repo_id}")
    @permit(P.REPOSITORY_MANAGE, scope=Scope.project(path="project_id"))
    def project_repo_delete(project_id: str, repo_id: str, request: Request) -> Any:
        try:
            with begin() as db:
                binding_id = _binding_for(db, project_id, repo_id)
                if binding_id is None:
                    raise MultiRepoError(404, "unknown_repository", "That repository is not in this project.")
                project_repos.remove(db, project_id, binding_id, actor=_actor(request))
                return project_repos.list_project_repos(db, project_id)
        except MultiRepoError as exc:
            return _refuse(exc)

    @router.put("/v1/projects/{project_id}/repo-mode")
    @permit(P.REPOSITORY_MANAGE, scope=Scope.project(path="project_id"))
    def project_repo_mode(project_id: str, request: Request, payload: dict = Body(...)) -> Any:
        try:
            with begin() as db:
                project_repos.set_mode(db, project_id, str(payload.get("repo_mode") or ""), actor=_actor(request))
                return project_repos.list_project_repos(db, project_id)
        except MultiRepoError as exc:
            return _refuse(exc)

    # ── components ────────────────────────────────────────────────────────────────────────────

    def _app_key(db: Any, project_id: str, raw: str, *, create: bool = False) -> Optional[str]:
        """Any handle of an existing app, or (create=True) the key a NEW app's name files under."""
        from boss_engine.app_links import service as links

        key = None
        try:
            key = links.resolve_app_key(db, project_id, raw)
        except Exception:  # noqa: BLE001 — an app with no runs yet
            key = None
        if key:
            return key
        known = db.execute(_sql("SELECT 1 FROM app_components WHERE project_id = :p AND app_key = :a LIMIT 1"),
                           {"p": project_id, "a": raw}).first()
        if known:
            return raw
        return components.app_key_of(raw) if create else None

    @router.get("/v1/apps/{app_key}/components")
    @permit(P.REPOSITORY_READ, scope=Scope.project(query="project_id"))
    def get_components(app_key: str, project_id: str) -> Any:
        with begin() as db:
            key = _app_key(db, project_id, app_key) or components.app_key_of(app_key)
            out = components.get(db, project_id, key)
            out["change_sets"] = _recent_change_sets(db, project_id, key)
            return out

    @router.put("/v1/apps/{app_key}/components")
    @permit(P.REPOSITORY_MANAGE, scope=Scope.project(body="project_id"))
    def put_components(app_key: str, request: Request, payload: dict = Body(...)) -> Any:
        project_id = str(payload.get("project_id") or "")
        try:
            with begin() as db:
                key = _app_key(db, project_id, app_key, create=True)
                out = components.put(db, project_id, key, payload.get("components") or [],
                                     app_name=payload.get("app_name") or None, actor=_actor(request))
                _audit(request, db, "app.components_set", target_type="app", target_id=key,
                       permission=P.REPOSITORY_MANAGE.value, project_id=project_id,
                       details={"components": [c["name"] for c in out["components"] + out["deploy_config"]],
                                "repositories": sorted({c["repository"] for c in out["components"]
                                                        + out["deploy_config"] if c.get("repository")})})
                return out
        except MultiRepoError as exc:
            return _refuse(exc)

    @router.post("/v1/projects/{project_id}/suggest-components")
    @permit(P.REPOSITORY_READ, scope=Scope.project(path="project_id"))
    def suggest_components(project_id: str, payload: dict = Body(...)) -> Any:
        ids = payload.get("binding_ids") or payload.get("repo_ids") or []
        if not isinstance(ids, list):
            return JSONResponse({"error": "bad_request", "message": "binding_ids is a list."}, status_code=422)
        try:
            with begin() as db:
                return components.suggest(db, project_id, [str(i) for i in ids])
        except MultiRepoError as exc:
            return _refuse(exc)

    @router.get("/v1/apps/{app_key}/consent")
    @permit(P.REPOSITORY_READ, scope=Scope.project(query="project_id"))
    def app_consent(app_key: str, project_id: str) -> Any:
        with begin() as db:
            key = _app_key(db, project_id, app_key) or components.app_key_of(app_key)
            return grants.consent_details(db, project_id, key)

    return router


def _sql(statement: str):
    from sqlalchemy import text

    return text(statement)


def _binding_for(db: Any, project_id: str, repo_id: str) -> Optional[str]:
    """The project's active binding a path id names: the binding itself, or the registry row's."""
    row = db.execute(_sql(
        "SELECT b.id FROM github_repository_bindings b WHERE b.project_id = :p AND b.status = 'active' AND "
        "(b.id = :r OR b.registry_repo_id = :r OR lower(b.full_name) = (SELECT lower(full_name) FROM github_repos "
        "WHERE id = :r)) LIMIT 1"), {"p": project_id, "r": repo_id}).scalar()
    return str(row) if row else None


def _recent_change_sets(db: Any, project_id: str, app_key: str) -> list[dict[str, Any]]:
    try:
        from boss_engine.multirepo import change_sets

        with db.begin_nested():
            return change_sets.recent_for_app(db, project_id, app_key)
    except Exception:  # noqa: BLE001 — before change sets exist
        return []
