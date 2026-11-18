"""Easy Mode (BOSSAPP-1274) and its attachments (BOSSAPP-1275).

POST /v1/factory/launch        {prompt, mode, template_id, attachment_ids, github_repo}
POST /v1/factory/attachments   multipart specs, files and images, or a connected Drive source
GET  /v1/factory/templates     the yaml catalogue, skipping files that are not the schema; each
                               template says whether this install can build it (`available`,
                               `needs`: an opt-in toolchain such as Go that is not turned on)

A launch that cannot name a project and a run is a failure. A build start that needs the
person's consent answers 409 consent_required with the kept `project_id`: the project is
committed, and the same launch sent again after the grant starts the build in it. GitHub is accepted only when that
repository is already connected for the org, and the refusal is the existing connect error.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.config import get_settings
from boss_engine.db import begin, database_is_reachable, get_engine
from boss_engine.identity import P, Scope, authenticated, permit
from boss_engine.identity.consent import ConsentRequired
from boss_engine.multirepo.common import MultiRepoError
from boss_engine.services.factory_launch import FactoryLaunchError
from boss_engine.services.projects import ProjectExists

router = APIRouter(tags=["factory"])
_settings = get_settings()
_engine = get_engine(_settings)


class LaunchBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    prompt: str = Field(default="", max_length=20000)
    mode: str = Field(default="", max_length=40)
    template_id: Optional[str] = Field(default=None, max_length=80)
    attachment_ids: list[str] = Field(default_factory=list, max_length=20)
    github_repo: Optional[str] = Field(default=None, max_length=200)


def _caller(request: Request) -> tuple[Any, str, str]:
    from apps.api.app import identity_guard

    ctx = identity_guard.request_auth(request)
    if ctx is not None:
        return ctx, str(ctx.principal.org_id), identity_guard.actor_for(ctx)
    return None, str(_settings.default_org_id), "operator"


def _jobs() -> Any:
    from apps.api.app.main import jobs

    return jobs


def _failure(error: FactoryLaunchError) -> JSONResponse:
    return JSONResponse(error.body(), status_code=error.status)


@router.get("/v1/factory/templates")
@authenticated(note="The factory template catalogue is not secret.")
def factory_templates() -> dict[str, Any]:
    """List engine/boss_engine/data/factory_templates/*.yaml. Malformed files are counted."""
    from boss_engine.services.factory_launch import list_templates

    return list_templates()


@router.post("/v1/factory/attachments", status_code=201)
@permit(P.PROJECT_CREATE, scope=Scope.org())
def factory_attachments(
    request: Request,
    spec: Optional[list[UploadFile]] = File(default=None),
    file: Optional[list[UploadFile]] = File(default=None),
    image: Optional[list[UploadFile]] = File(default=None),
    drive_source_id: str = Form(default=""),
) -> Any:
    """Store specs, files and images on the launch, scoped to the org. Refuse a file over the
    small limit. A Drive source id is stored only when that source is already connected."""
    from boss_engine.services.factory_launch import store_attachments

    if not database_is_reachable(_engine):
        return JSONResponse({"error": "database", "message": "Postgres is not reachable."},
                            status_code=503)
    _auth, org_id, _actor = _caller(request)
    uploads: list[tuple[str, str, str, bytes]] = []
    for kind, group in (("spec", spec), ("file", file), ("image", image)):
        for upload in group or []:
            uploads.append((
                kind,
                upload.filename or kind,
                upload.content_type or "",
                upload.file.read(),
            ))
    try:
        with begin(_engine) as connection:
            return store_attachments(
                connection, org_id=org_id, uploads=uploads, drive_source_id=drive_source_id)
    except FactoryLaunchError as error:
        return _failure(error)


@router.post("/v1/factory/launch", status_code=201)
@permit(P.PROJECT_CREATE, scope=Scope.org())
def factory_launch(payload: LaunchBody, request: Request) -> Any:
    """Create a project from a prompt, a template, or Easy Mode, and start its build."""
    from boss_engine.services import factory_launch as launch_service

    if not database_is_reachable(_engine):
        return JSONResponse({"error": "database", "message": "Postgres is not reachable."},
                            status_code=503)
    auth, org_id, actor = _caller(request)
    asked: Optional[ConsentRequired] = None
    try:
        with begin(_engine) as connection:
            try:
                result = launch_service.launch(
                    connection, _settings, org_id=org_id, actor=actor, auth=auth,
                    prompt=payload.prompt, mode=payload.mode, template_id=payload.template_id,
                    attachment_ids=payload.attachment_ids, github_repo=payload.github_repo,
                    jobs=_jobs())
            except ConsentRequired as consent_asked:
                # KEPT, NOT ROLLED BACK: the project and its agents the consent body names are
                # committed with this transaction; the same launch sent again after the grant
                # starts the build in that project (services/factory_launch._kept).
                asked = consent_asked
                result = {}
            if asked is None and (not result.get("project_id") or not result.get("run_id")):
                raise FactoryLaunchError(
                    502, "launch_incomplete",
                    "A launch without a project and a run is not done.")
    except FactoryLaunchError as error:
        return _failure(error)
    except MultiRepoError as error:
        return JSONResponse(error.body(), status_code=error.status)
    except ProjectExists as clash:
        return JSONResponse({
            "error": "project_exists",
            "message": f"A project with the slug {clash.slug} already exists.",
            "project_id": clash.project_id,
        }, status_code=409)
    except ValueError as error:
        return JSONResponse({"error": "invalid_launch", "message": str(error)}, status_code=422)
    if asked is not None:
        # The flat body every console start reads (identity_agents.install_consent_handler).
        return JSONResponse(asked.body, status_code=409)
    return result
