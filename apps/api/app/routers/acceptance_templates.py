"""Human-edited reusable checks; no execution or provider authority is created."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.db import begin
from boss_engine.identity import P, Scope, lineage, permit
from boss_engine.identity.principal import PrincipalKind, PrincipalStatus
from boss_engine.services import acceptance_templates as templates


class CreateChecks(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=100)
    run_id: str = Field(min_length=1, max_length=100)
    review_id: str = Field(min_length=1, max_length=100)
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    title: str = Field(min_length=1, max_length=120)
    submission_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,80}$")


class ReviseChecks(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=100)
    expected_revision_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    title: str = Field(min_length=1, max_length=120)
    criteria: list[str] = Field(min_length=1, max_length=20)
    submission_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,80}$")


def _human() -> str:
    ctx, acting = lineage.current_context(), lineage.current()
    if (ctx is None or ctx.principal.kind != PrincipalKind.USER
            or ctx.principal.status != PrincipalStatus.ACTIVE or ctx.run_token is not None
            or (acting is not None and (acting.run_id or acting.chain or acting.system))):
        raise HTTPException(403, "Sign in as a person to save reusable acceptance checks.")
    return ctx.principal.id


def build_acceptance_templates_router(*, engine, require_reader, require_write_actor) -> APIRouter:
    router = APIRouter(tags=["acceptance-checks"])

    def write(operation, payload, **extra):
        actor = _human()
        with begin(engine) as connection:
            require_write_actor(connection, payload.project_id, actor, "workflow_manage")
            try:
                return operation(connection, actor=actor, **payload.model_dump(), **extra)
            except templates.TemplateConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

    @router.get("/v1/acceptance-checks")
    @permit(P.WORKFLOW_READ, scope=Scope.project(query="project_id"))
    def list_checks(project_id: str):
        with begin(engine) as connection:
            require_reader(connection, project_id)
            return {"items": templates.list_latest(connection, project_id)}

    @router.post("/v1/acceptance-checks")
    @permit(P.WORKFLOW_CREATE, scope=Scope.project(body="project_id"))
    def create_checks(payload: CreateChecks):
        return write(templates.create, payload)

    @router.post("/v1/acceptance-checks/{template_id}/revisions")
    @permit(P.WORKFLOW_CREATE, scope=Scope.project(body="project_id"))
    def revise_checks(template_id: str, payload: ReviseChecks):
        return write(templates.revise, payload, template_id=template_id)

    return router
