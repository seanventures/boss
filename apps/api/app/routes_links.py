"""/v1/apps/{app_key}/links and test users (F5, cross-app wiring).

Spec: docs/product/APP_TEMPLATES_AND_SECURITY_LOOP.md §5.6 (and D9). Owner: stream S5.

DAY-1 INTERFACE (stream S0): every route is declared with its permission and answers
501 `not_implemented_yet` until S5 builds it. Links are same-project only (D7); creation and
revocation are audit events.

The spec says `project:write` for creating a link; the catalogue's key for that is
`project:update` (Owner, Admin; step-up). S5 or the operator may decide a link needs its own key.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from boss_engine.identity import P, Scope, permit

SPEC = "APP_TEMPLATES_AND_SECURITY_LOOP.md §5.6"


def not_implemented_yet() -> JSONResponse:
    return JSONResponse({"error": "not_implemented_yet", "owner": "S5", "spec": SPEC}, status_code=501)


def build_links_router() -> APIRouter:
    router = APIRouter(tags=["links"])

    @router.get("/v1/apps/{app_key}/links")
    @permit(P.PROJECT_READ, scope=Scope.project(query="project_id"))
    def list_links(app_key: str) -> JSONResponse:
        return not_implemented_yet()

    @router.post("/v1/apps/{app_key}/links")
    @permit(P.PROJECT_UPDATE, scope=Scope.project(body="project_id"))
    def create_link(app_key: str) -> JSONResponse:
        return not_implemented_yet()

    @router.delete("/v1/apps/{app_key}/links/{link_id}")
    @permit(P.PROJECT_UPDATE, scope=Scope.of("app_link", path="link_id"))
    def revoke_link(app_key: str, link_id: str) -> JSONResponse:
        return not_implemented_yet()

    # Presence and count only.
    @router.get("/v1/apps/{app_key}/test-users")
    @permit(P.PROJECT_READ, scope=Scope.project(query="project_id"))
    def test_users(app_key: str) -> JSONResponse:
        return not_implemented_yet()

    # D9: the generated test-user passwords, behind app_secret:write (step-up), audited.
    @router.post("/v1/apps/{app_key}/test-users/reveal")
    @permit(P.APP_SECRET_WRITE, scope=Scope.project(body="project_id"))
    def reveal_test_users(app_key: str) -> JSONResponse:
        return not_implemented_yet()

    return router
