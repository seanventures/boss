"""/v1/security/*: scans, findings, triage and the before/after report (F4, the security review).

Spec: docs/product/APP_TEMPLATES_AND_SECURITY_LOOP.md §4.3. Owner: stream S4. The engine is
`boss_engine/security_scan/`. (The older `/v1/security-scan` router in routers/security_scan.py is a
different, in-process surface.)

A scan is a worker job (`security.scan`): the worker leases it and asks the API to run it
(`POST /v1/security/scans/{id}/run`, worker only), because the scanners and the sandbox helper are in
the api image. The full loop starts through `POST /v1/delivery-workflows/start` with
`workflow_key=security_review`; `/v1/security/reviews*` reads its runs and records the person's
choice of fixes (services/security_review_steps.py, security_scan/review.py).
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from boss_engine.identity import P, Scope, permit

SPEC = "APP_TEMPLATES_AND_SECURITY_LOOP.md §4.3"


class ZapOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["baseline", "api"] = "baseline"
    #: `sandbox` or `preview:<id>`; anything else (a URL) is refused by the target resolver.
    target: str = Field(default="sandbox", max_length=140)


class ScanRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=128)
    app_key: str = Field(min_length=1, max_length=128)
    ref: Optional[str] = Field(default=None, max_length=200)
    tools: Optional[list[str]] = Field(default=None, max_length=3)
    zap: Optional[ZapOptions] = None
    workflow_run_id: Optional[str] = Field(default=None, max_length=128)


class AcceptItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finding_id: str = Field(min_length=1, max_length=128)
    #: checked by security_scan.service.check_reason (one rule, said in words), not here
    reason: str = Field(default="", max_length=2000)


class SelectionBody(BaseModel):
    """A person's choice at the review's decide step: the recommendations to fix, and findings
    they will not fix, accepted with a reason."""
    model_config = ConfigDict(extra="forbid")
    selected: list[str] = Field(default_factory=list, max_length=100)
    accept: list[AcceptItem] = Field(default_factory=list, max_length=200)
    #: "Fix selected": also approve the review's gate (this decision) in the SAME commit. The
    #: caller's right to approve is checked FIRST, before anything is written; a refused approval
    #: leaves the choice unrecorded (one action, never half of one).
    approve: bool = False
    note: Optional[str] = Field(default=None, max_length=2000)


class TriageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["accepted", "false_positive"]
    #: checked by security_scan.service.check_reason (one rule, said in words), not here
    reason: str = Field(default="", max_length=2000)
    #: also every open finding of the same scan in its group (one rule, one route class)
    whole_group: bool = False


def _engine_settings():
    from . import main

    return main.engine, main.settings


def _actor() -> str:
    from boss_engine.identity import lineage

    ctx = lineage.current_context()
    if ctx is not None:
        return str(ctx.principal.id)
    from .main import current_api_principal

    return str(current_api_principal.get() or "system")


def _refused(exc: Any) -> JSONResponse:
    return JSONResponse({"error": exc.code, "message": str(exc)}, status_code=exc.status)


def build_security_router() -> APIRouter:
    router = APIRouter(tags=["security"])

    @router.post("/v1/security/scans")
    @permit(P.SECURITY_SCAN_RUN, scope=Scope.project(body="project_id"))
    def start_scan(payload: ScanRequestBody) -> Any:
        from boss_engine.security_scan import scope, service

        engine, settings = _engine_settings()
        try:
            with engine.begin() as conn:
                scan = service.request_scan(
                    conn, project_id=payload.project_id, app_key=payload.app_key, ref=payload.ref,
                    tools=payload.tools, zap=payload.zap.model_dump() if payload.zap else None,
                    triggered_by=_actor(), workflow_run_id=payload.workflow_run_id, settings=settings)
        except scope.ScopeRefused as exc:
            return _refused(exc)
        return JSONResponse({"scan_id": scan["id"], "scan": scan}, status_code=202)

    @router.get("/v1/security/scans")
    @permit(P.PROJECT_READ, scope=Scope.project(query="project_id"))
    def list_scans(project_id: str = Query(..., max_length=128),
                   app_key: Optional[str] = Query(default=None, max_length=128),
                   limit: int = Query(default=50, ge=1, le=200)) -> Any:
        from boss_engine.security_scan import service

        engine, _ = _engine_settings()
        with engine.begin() as conn:
            return {"scans": service.list_scans(conn, project_id, app_key, limit)}

    @router.get("/v1/security/scans/{scan_id}")
    @permit(P.PROJECT_READ, scope=Scope.of("security_scan", path="scan_id"))
    def get_scan(scan_id: str) -> Any:
        from boss_engine.security_scan import service

        engine, _ = _engine_settings()
        with engine.begin() as conn:
            scan = service.get_scan(conn, scan_id)
        if scan is None:
            return JSONResponse({"error": "scan_not_found"}, status_code=404)
        return scan

    # Normalised and redacted.
    @router.get("/v1/security/scans/{scan_id}/findings")
    @permit(P.PROJECT_READ, scope=Scope.of("security_scan", path="scan_id"))
    def scan_findings(scan_id: str, tool: Optional[Literal["semgrep", "osv", "zap"]] = None,
                      severity: Optional[Literal["critical", "high", "medium", "low", "info"]] = None,
                      status: Optional[Literal["open", "fixed", "accepted", "false_positive"]] = None) -> Any:
        from boss_engine.security_scan import service

        engine, _ = _engine_settings()
        with engine.begin() as conn:
            scan = service.get_scan(conn, scan_id)
            if scan is None:
                return JSONResponse({"error": "scan_not_found"}, status_code=404)
            findings = service.list_findings(conn, scan_id, tool=tool, severity=severity, status=status)
        # `groups`: the same problem once (a header missing on every page is one row, with where)
        return {"scan_id": scan_id, "status": scan["status"], "findings": findings,
                "groups": service.group_findings(findings)}

    @router.post("/v1/security/findings/{finding_id}/triage")
    @permit(P.SECURITY_FINDING_TRIAGE, scope=Scope.of("security_finding", path="finding_id"))
    def triage_finding(finding_id: str, payload: TriageBody) -> Any:
        from boss_engine.security_scan import service

        engine, _ = _engine_settings()
        try:
            with engine.begin() as conn:
                return service.triage(conn, finding_id, status=payload.status, reason=payload.reason,
                                      actor=_actor(), whole_group=payload.whole_group)
        except service.TriageRefused as exc:
            return _refused(exc)

    # ?before=<scan>&after=<scan>; ?format=md for export.
    @router.get("/v1/security/reports/{app_key}")
    @permit(P.PROJECT_READ, scope=Scope.project(query="project_id"))
    def security_report(app_key: str, project_id: str = Query(..., max_length=128),
                        before: Optional[str] = Query(default=None, max_length=128),
                        after: Optional[str] = Query(default=None, max_length=128),
                        format: Literal["json", "md"] = "json") -> Any:  # noqa: A002
        from boss_engine.security_scan import report

        engine, _ = _engine_settings()
        try:
            with engine.begin() as conn:
                from boss_engine.app_links.service import resolve_app_id

                # Any handle of the app (name in any case, slug, run id) names its reports.
                app_key = resolve_app_id(conn, project_id, app_key) or app_key
                rep = report.build_report(conn, project_id, app_key, before=before, after=after)
        except report.ReportError as exc:
            return _refused(exc)
        if format == "md":
            return PlainTextResponse(report.to_markdown(rep), media_type="text/markdown; charset=utf-8")
        return rep

    # ---- the security_review workflow (S4-workflow): its runs, as App › Security draws them ----

    @router.get("/v1/security/reviews")
    @permit(P.PROJECT_READ, scope=Scope.project(query="project_id"))
    def list_reviews(project_id: str = Query(..., max_length=128),
                     app_key: Optional[str] = Query(default=None, max_length=128),
                     limit: int = Query(default=20, ge=1, le=100)) -> Any:
        from boss_engine.services import security_review_steps

        engine, _ = _engine_settings()
        with engine.begin() as conn:
            return {"reviews": security_review_steps.list_reviews(conn, project_id, app_key, limit)}

    # One review: its steps, recommendations (the checklist) and the findings they cite.
    @router.get("/v1/security/reviews/{run_id}")
    @permit(P.PROJECT_READ, scope=Scope.of("workflow_run", path="run_id"))
    def get_review(run_id: str) -> Any:
        from boss_engine.security_scan import review, service
        from boss_engine.services import security_review_steps

        engine, _ = _engine_settings()
        with engine.begin() as conn:
            state = security_review_steps.review_state(conn, run_id)
            if state is None:
                return JSONResponse({"error": "review_not_found"}, status_code=404)
            scan_id = next((s.get("scan_id") for s in state["steps"] if s["id"] == "scan"), None)
            findings = service.list_findings(conn, scan_id) if scan_id else []
        state["scan_id"] = scan_id
        state["findings"] = {f["fingerprint"]: {"id": f["id"], "title": f["title"], "severity": f["severity"],
                                                "tool": f["tool"], "status": f["status"],
                                                "where": review.where(f)} for f in findings}
        return state

    @router.post("/v1/security/reviews/{run_id}/selection")
    @permit(P.SECURITY_FINDING_TRIAGE, scope=Scope.of("workflow_run", path="run_id"))
    def record_selection(run_id: str, payload: SelectionBody, request: Request) -> Any:
        from boss_engine.security_scan import review
        from boss_engine.services import security_review_steps

        engine, _ = _engine_settings()
        with engine.begin() as conn:
            project_id = conn.execute(text("SELECT project_id FROM workflow_runs WHERE id = :r"),
                                      {"r": run_id}).scalar()
            state = security_review_steps.review_state(conn, run_id) if payload.approve else None
        if project_id is None:
            return JSONResponse({"error": "review_not_found"}, status_code=404)
        decision_id = (state or {}).get("decision_id")
        if payload.approve:
            if not decision_id:
                return JSONResponse({"error": "not_awaiting_choice",
                                     "message": "this review is not waiting for a choice"}, status_code=409)
            # FIRST: may this person approve the gate at all? Refused here, nothing is written.
            from .routers.identity_access import require_permission

            require_permission(request, P.DECISION_APPROVE, str(project_id), label="security review")
        from boss_engine.identity.approvals import ApprovalPending

        try:
            with engine.begin() as conn:
                out = review.record_selection(
                    conn, project_id=str(project_id), run_id=run_id, selected=payload.selected,
                    accept=[a.model_dump() for a in payload.accept], actor=_actor())
                if payload.approve:
                    from . import main
                    from .main import DecisionActionRequest

                    try:
                        main.approve_decision_in(conn, decision_id, DecisionActionRequest(
                            actor=_actor(), note=payload.note or "Fix the selected recommendations."))
                        out = {**out, "approved": True, "decision_id": decision_id}
                    except ApprovalPending as pending:
                        # the first of two approvals: recorded with the choice; a second person approves
                        out = {**out, "approved": False, "decision_id": decision_id, "pending": pending.body()}
                return out
        except review.ReviewRefused as exc:
            return _refused(exc)

    # Worker-invoked: run one leased `security.scan` job here, where the scanners are.
    @router.post("/v1/security/scans/{scan_id}/run")
    @permit(P.WORKER_LEASE)
    def run_scan(scan_id: str) -> Any:
        from boss_engine.security_scan import service

        from .identity_guard import require_worker_principal

        require_worker_principal()
        engine, settings = _engine_settings()
        return service.execute_scan(engine, settings, scan_id)

    return router
