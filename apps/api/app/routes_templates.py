"""/v1/templates* and an app's templates, secrets and setup guide (F2, the template system).

Spec: docs/product/APP_TEMPLATES_AND_SECURITY_LOOP.md §2.6. Owner: stream S2.

DAY-1 INTERFACE (stream S0): every route is declared with its permission and answers
501 `not_implemented_yet` until S2 builds it. The paths, methods and declarations are the contract
the console and the other streams build against; change them only with the spec.

App-level routes (`/v1/apps/{app_key}/...`) name their project in the request (`project_id`): an
app key is unique within a project, not across projects.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from boss_engine.identity import P, Scope, permit

SPEC = "APP_TEMPLATES_AND_SECURITY_LOOP.md §2.6"


def not_implemented_yet() -> JSONResponse:
    return JSONResponse({"error": "not_implemented_yet", "owner": "S2", "spec": SPEC}, status_code=501)


def build_templates_router() -> APIRouter:
    router = APIRouter(tags=["templates"])

    # ---- the gallery and its management (org-wide: templates belong to the org) ----------------
    @router.get("/v1/templates")
    @permit(P.TEMPLATE_READ, scope=Scope.org())
    def list_templates() -> JSONResponse:
        return not_implemented_yet()

    @router.post("/v1/templates/import")
    @permit(P.TEMPLATE_IMPORT, scope=Scope.org())
    def import_template() -> JSONResponse:
        return not_implemented_yet()

    @router.get("/v1/templates/{template_id}")
    @permit(P.TEMPLATE_READ, scope=Scope.org())
    def describe_template(template_id: str) -> JSONResponse:
        return not_implemented_yet()

    # Seat-separated in the handler: the importer never approves their own import (D3).
    @router.post("/v1/templates/{template_id}/approve")
    @permit(P.TEMPLATE_APPROVE, scope=Scope.org())
    def approve_template(template_id: str) -> JSONResponse:
        return not_implemented_yet()

    @router.post("/v1/templates/{template_id}/reject")
    @permit(P.TEMPLATE_APPROVE, scope=Scope.org())
    def reject_template(template_id: str) -> JSONResponse:
        return not_implemented_yet()

    @router.post("/v1/templates/{template_id}/trial")
    @permit(P.TEMPLATE_APPROVE, scope=Scope.org())
    def trial_template(template_id: str) -> JSONResponse:
        return not_implemented_yet()

    # Built-ins cannot be deleted, only hidden.
    @router.delete("/v1/templates/{template_id}")
    @permit(P.TEMPLATE_APPROVE, scope=Scope.org())
    def delete_template(template_id: str) -> JSONResponse:
        return not_implemented_yet()

    # Apply = a governed build. The route checks workflow_run:start at the project; the handler
    # also requires template:read (§2.6).
    @router.post("/v1/templates/{template_id}/apply")
    @permit(P.WORKFLOW_RUN_START, scope=Scope.project(body="project_id"))
    def apply_template(template_id: str) -> JSONResponse:
        return not_implemented_yet()

    # ---- one app's templates, secrets and setup guide --------------------------------------------
    @router.get("/v1/apps/{app_key}/templates")
    @permit(P.PROJECT_READ, scope=Scope.project(query="project_id"))
    def app_templates(app_key: str) -> JSONResponse:
        return not_implemented_yet()

    # Presence, mode and a shape verdict only; never a value, hash, prefix or length (§2.4).
    @router.get("/v1/apps/{app_key}/secrets")
    @permit(P.APP_SECRET_READ_METADATA, scope=Scope.project(query="project_id"))
    def app_secrets(app_key: str) -> JSONResponse:
        return not_implemented_yet()

    # Write-only. mode=live additionally needs app_secret:live, checked in the handler.
    @router.put("/v1/apps/{app_key}/secrets/{name}")
    @permit(P.APP_SECRET_WRITE, scope=Scope.project(body="project_id"))
    def put_app_secret(app_key: str, name: str) -> JSONResponse:
        return not_implemented_yet()

    @router.delete("/v1/apps/{app_key}/secrets/{name}")
    @permit(P.APP_SECRET_WRITE, scope=Scope.project(query="project_id"))
    def delete_app_secret(app_key: str, name: str) -> JSONResponse:
        return not_implemented_yet()

    @router.get("/v1/apps/{app_key}/setup-guide")
    @permit(P.PROJECT_READ, scope=Scope.project(query="project_id"))
    def app_setup_guide(app_key: str) -> JSONResponse:
        return not_implemented_yet()

    return router
