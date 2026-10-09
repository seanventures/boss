"""/v1/security/*: scans, findings, triage and the before/after report (F4, the security review).

Spec: docs/product/APP_TEMPLATES_AND_SECURITY_LOOP.md §4.3. Owner: stream S4. (The older
`/v1/security-scan` router in routers/security_scan.py is a different, in-process surface.)

DAY-1 INTERFACE (stream S0): every route is declared with its permission and answers
501 `not_implemented_yet` until S4 builds it. The full loop starts through the existing
`POST /v1/delivery-workflows/start` with `workflow_key=security_review`.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from boss_engine.identity import P, Scope, permit

SPEC = "APP_TEMPLATES_AND_SECURITY_LOOP.md §4.3"


def not_implemented_yet() -> JSONResponse:
    return JSONResponse({"error": "not_implemented_yet", "owner": "S4", "spec": SPEC}, status_code=501)


def build_security_router() -> APIRouter:
    router = APIRouter(tags=["security"])

    @router.post("/v1/security/scans")
    @permit(P.SECURITY_SCAN_RUN, scope=Scope.project(body="project_id"))
    def start_scan() -> JSONResponse:
        return not_implemented_yet()

    @router.get("/v1/security/scans")
    @permit(P.PROJECT_READ, scope=Scope.project(query="project_id"))
    def list_scans() -> JSONResponse:
        return not_implemented_yet()

    @router.get("/v1/security/scans/{scan_id}")
    @permit(P.PROJECT_READ, scope=Scope.of("security_scan", path="scan_id"))
    def get_scan(scan_id: str) -> JSONResponse:
        return not_implemented_yet()

    # Normalised and redacted.
    @router.get("/v1/security/scans/{scan_id}/findings")
    @permit(P.PROJECT_READ, scope=Scope.of("security_scan", path="scan_id"))
    def scan_findings(scan_id: str) -> JSONResponse:
        return not_implemented_yet()

    @router.post("/v1/security/findings/{finding_id}/triage")
    @permit(P.SECURITY_FINDING_TRIAGE, scope=Scope.of("security_finding", path="finding_id"))
    def triage_finding(finding_id: str) -> JSONResponse:
        return not_implemented_yet()

    # ?before=<scan>&after=<scan>; ?format=md for export.
    @router.get("/v1/security/reports/{app_key}")
    @permit(P.PROJECT_READ, scope=Scope.project(query="project_id"))
    def security_report(app_key: str) -> JSONResponse:
        return not_implemented_yet()

    return router
