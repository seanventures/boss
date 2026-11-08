"""Delete a project, with a full cleanup (services/projects_delete.py).

GET  /v1/projects/{project_id}/delete   what deleting it would remove, counted by kind (the dialog)
POST /v1/projects/{project_id}/delete   delete it: its apps off their servers, its work and previews
                                        stopped, then every row in one transaction

The write needs `project:delete` (owner, step-up), `step_up: "confirmed"` (the console's dialog
sends it) and `confirm_name` equal to the project's name. Every refusal answers in words and
writes nothing: a wrong name (422 name_mismatch), the install's home project (409
delete_refused), a live production app without `also_production` (409 production_running).
One `project.deleted` row in the org's security audit log records it, with the counts.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from boss_engine.identity import P, Scope, permit
from boss_engine.services import projects_delete as delete_service

router = APIRouter(tags=["projects"])


class DeleteBody(BaseModel):
    confirm_name: Optional[str] = None
    step_up: Optional[str] = None
    also_production: bool = False
    actor: Optional[str] = None


def _refused(refused: delete_service.DeleteRefused) -> HTTPException:
    return HTTPException(status_code=refused.status, detail=refused.detail())


@router.get("/v1/projects/{project_id}/delete")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def delete_preview(project_id: str) -> dict[str, Any]:
    """What deleting the project would remove, by kind, its apps on servers, and what stays."""
    from apps.api.app.main import begin, engine, settings

    with begin(engine) as connection:
        seen = delete_service.preview(connection, settings, project_id)
    if seen is None:
        raise HTTPException(status_code=404, detail=f"There is no project {project_id}.")
    return seen


@router.post("/v1/projects/{project_id}/delete")
@permit(P.PROJECT_DELETE, scope=Scope.org())
def delete_project(project_id: str, body: DeleteBody, request: Request) -> dict[str, Any]:
    """Delete the project and everything it holds in BOSS. The GitHub repository is untouched."""
    from apps.api.app.main import begin, engine, settings
    from apps.api.app.routers.projects_archive import _caller

    if body.step_up != "confirmed":
        raise HTTPException(status_code=428, detail={
            "error": "confirmation_required",
            "message": "Deleting a project needs an explicit confirmation (step_up: "
                       "\"confirmed\"), the one the console's dialog sends. Nothing was deleted."})
    with begin(engine) as connection:
        ctx, actor = _caller(request, connection, project_id, body)
    try:
        done = delete_service.delete_project(
            engine, settings, project_id, actor=actor, confirm_name=body.confirm_name, auth=ctx,
            also_production=body.also_production)
    except delete_service.DeleteRefused as refused:
        raise _refused(refused) from None
    except LookupError as missing:
        raise HTTPException(status_code=404, detail=str(missing)) from None
    return {
        "deleted": True, "id": done["id"], "name": done["name"], "counts": done["counts"],
        "rows": done["rows"], "graph_dropped": done["graph_dropped"],
        "removed_apps": done["removed_apps"], "stopped": done["stopped"],
        "kept_for_other_projects": done["kept_for_others"],
        # Its files on this machine (services/project_files.py): counts, and any path a row
        # pointed outside BOSS's directories, which was left alone.
        "files": done.get("files"), "files_refused": done.get("files_refused") or [],
    }
