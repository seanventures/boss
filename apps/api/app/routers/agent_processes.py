"""Agent process HTTP — templates, start, advance, handoff timeline."""
from __future__ import annotations

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from boss_engine.services.agent_process import AgentProcessService

from ..config import get_settings
from ..db import begin, get_engine


router = APIRouter(tags=["agent-processes"])


class CompileProcessRequest(BaseModel):
    project_id: str
    actor: str = "executive_console"
    text: str = ""
    description: str = ""


class TemplateCreateRequest(BaseModel):
    project_id: str
    actor: str = "executive_console"
    process_key: str
    name: str
    description: str = ""
    spec: Optional[dict[str, Any]] = None
    from_text: Optional[str] = None


class ProcessStartRequest(BaseModel):
    project_id: str
    actor: str = "executive_console"
    prd: str
    title: str = ""
    process_key: str = "prd_to_accepted_work"
    simulated: bool = Field(default=False)
    auto_build: bool = Field(
        default=False,
        description="If true, captain build enqueues a governed code.task.",
    )
    attach_run_id: Optional[str] = Field(
        default=None,
        description="Bind captains to an existing official/factory run instead of starting another.",
    )
    official: bool = False
    engine: Optional[str] = None
    review_panel_size: Optional[int] = None


class ProcessActorRequest(BaseModel):
    project_id: str
    actor: str = "executive_console"


def _service() -> AgentProcessService:
    return AgentProcessService(get_settings())


def _require(connection, project_id: str, actor: str) -> None:
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, "agent_chat")


def _spec_summary(spec: Any) -> dict[str, Any]:
    if not isinstance(spec, dict):
        spec = {}
    steps = spec.get("steps") or []
    if not isinstance(steps, list):
        steps = []
    step_keys: list[str] = []
    actions: list[str] = []
    for item in steps:
        if not isinstance(item, dict):
            continue
        step_keys.append(str(item.get("id") or item.get("step_key") or ""))
        actions.append(str(item.get("action") or ""))
    return {
        "key": spec.get("key"),
        "name": spec.get("name"),
        "entry": spec.get("entry"),
        "step_count": len(steps),
        "step_keys": [key for key in step_keys if key],
        "actions": [action for action in actions if action],
    }


def _normalize_compile_result(result: Any) -> dict[str, Any]:
    if isinstance(result, tuple) and result:
        result = result[0]
    if not isinstance(result, dict):
        raise HTTPException(status_code=422, detail="compiler returned invalid spec")
    if isinstance(result.get("spec"), dict):
        spec = result["spec"]
        warnings = result.get("warnings") or []
    else:
        spec = result
        warnings = result.get("warnings") or []
    if not isinstance(spec, dict) or not spec.get("steps"):
        raise HTTPException(status_code=422, detail="compiler returned invalid spec")
    if not isinstance(warnings, list):
        warnings = [str(warnings)]
    return {"spec": spec, "warnings": warnings}


def _compile_process_text(text: str) -> dict[str, Any]:
    if not (text or "").strip():
        raise HTTPException(status_code=422, detail="text is required")
    try:
        from boss_engine.services.process_compiler import compile_process_from_text
    except ImportError as exc:
        raise HTTPException(status_code=422, detail="compiler unavailable") from exc
    try:
        result = compile_process_from_text(text.strip())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — never 500 from authoring compile
        raise HTTPException(status_code=422, detail=str(exc) or "compile failed") from exc
    return _normalize_compile_result(result)


def _template_for_run(connection, project_id: str, run: dict[str, Any]) -> Optional[dict[str, Any]]:
    service = _service()
    process_key = str(run.get("process_key") or "").strip()
    if process_key:
        found = service.get_template_by_key(connection, project_id, process_key)
        if found:
            return found
    template_id = run.get("template_id")
    if not template_id:
        return None
    for item in service.list_templates(connection, project_id):
        if item.get("id") == template_id:
            return item
    return None


def _attach_run_view(connection, project_id: str, run: dict[str, Any]) -> dict[str, Any]:
    out = dict(run)
    service = _service()
    if "steps" not in out:
        try:
            out.update(service.get_run(connection, project_id, out["id"]))
        except ValueError:
            out.setdefault("steps", [])
    template = _template_for_run(connection, project_id, out)
    spec = (template or {}).get("spec") or out.get("spec") or {}
    if template:
        out.setdefault("process_key", template.get("process_key"))
        out.setdefault("template_name", template.get("name"))
    out["spec"] = spec
    out["spec_summary"] = _spec_summary(spec)
    return out


@router.post("/v1/agent-processes/compile")
@permit(P.AUTOPILOT_CONFIGURE, scope=Scope.project(body="project_id"))
def compile_process(payload: CompileProcessRequest) -> dict[str, Any]:
    if not payload.project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    with begin(get_engine()) as connection:
        _require(connection, payload.project_id, payload.actor)
        return _compile_process_text(payload.text or payload.description)


@router.get("/v1/agent-processes/templates")
@permit(P.AUTOPILOT_READ, scope=Scope.project(query="project_id"))
def list_templates(project_id: str) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        templates = _service().list_templates(connection, project_id)
        return {
            "project_id": project_id,
            "templates": [
                {**item, "spec_summary": _spec_summary(item.get("spec"))}
                for item in templates
            ],
        }


@router.post("/v1/agent-processes/templates", status_code=201)
@permit(P.AUTOPILOT_CONFIGURE, scope=Scope.project(body="project_id"))
def create_template(payload: TemplateCreateRequest) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        _require(connection, payload.project_id, payload.actor)
        spec = payload.spec
        if spec is None and payload.from_text:
            spec = _compile_process_text(payload.from_text)["spec"]
        if not spec:
            raise HTTPException(status_code=422, detail="spec or from_text is required")
        try:
            created = _service().create_template(
                connection,
                project_id=payload.project_id,
                actor=payload.actor,
                process_key=payload.process_key,
                name=payload.name,
                description=payload.description,
                spec=spec,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        created["spec_summary"] = _spec_summary(created.get("spec"))
        return created


@router.get("/v1/agent-processes/templates/{process_key}")
@permit(P.AUTOPILOT_READ, scope=Scope.project(query="project_id"))
def get_template(process_key: str, project_id: str) -> dict[str, Any]:
    if not project_id.strip() or not process_key.strip():
        raise HTTPException(status_code=422, detail="project_id and process_key are required")
    with begin(get_engine()) as connection:
        service = _service()
        if process_key == "prd_to_accepted_work":
            service.ensure_default_template(
                connection, project_id=project_id, actor="system"
            )
        found = service.get_template_by_key(connection, project_id, process_key)
        if found is None:
            raise HTTPException(status_code=404, detail="process template not found")
        found["spec_summary"] = _spec_summary(found.get("spec"))
        return found


@router.get("/v1/agent-processes/runs")
@permit(P.AUTOPILOT_READ, scope=Scope.project(query="project_id"))
def list_runs(project_id: str) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        runs = _service().list_runs(connection, project_id)
        return {
            "project_id": project_id,
            "runs": [_attach_run_view(connection, project_id, run) for run in runs],
        }


@router.post("/v1/agent-processes/runs", status_code=201)
@permit(P.AUTOPILOT_OPERATE, scope=Scope.project(body="project_id"))
def start_run(payload: ProcessStartRequest) -> dict[str, Any]:
    if not payload.project_id.strip() or not payload.prd.strip():
        raise HTTPException(status_code=422, detail="project_id and prd are required")
    with begin(get_engine()) as connection:
        _require(connection, payload.project_id, payload.actor)
        service = _service()
        process_key = (payload.process_key or "").strip() or "prd_to_accepted_work"
        if process_key != "prd_to_accepted_work":
            found = service.get_template_by_key(
                connection, payload.project_id, process_key
            )
            if found is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"unknown process_key {process_key}",
                )
        try:
            started = service.start_run(
                connection,
                project_id=payload.project_id,
                actor=payload.actor,
                prd=payload.prd,
                title=payload.title,
                process_key=process_key,
                simulated=payload.simulated,
                auto_build=payload.auto_build,
                attach_run_id=payload.attach_run_id,
                official=payload.official,
                engine=payload.engine,
                review_panel_size=payload.review_panel_size,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _attach_run_view(connection, payload.project_id, started)


@router.get("/v1/agent-processes/runs/{run_id}")
@permit(P.AUTOPILOT_READ, scope=Scope.project(query="project_id"))
def get_run(run_id: str, project_id: str) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        try:
            run = _service().get_run(connection, project_id, run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _attach_run_view(connection, project_id, run)


@router.post("/v1/agent-processes/runs/{run_id}/advance")
@permit(P.AUTOPILOT_OPERATE, scope=Scope.project(body="project_id"))
def advance_run(run_id: str, payload: ProcessActorRequest) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        _require(connection, payload.project_id, payload.actor)
        try:
            advanced = _service().advance(
                connection,
                project_id=payload.project_id,
                actor=payload.actor,
                run_id=run_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _attach_run_view(connection, payload.project_id, advanced)


@router.post("/v1/agent-processes/tick-all")
@permit(P.AUTOPILOT_OPERATE, scope=Scope.project(body="project_id"))
def tick_all(payload: ProcessActorRequest) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        _require(connection, payload.project_id, payload.actor)
        return _service().tick_due(
            connection, project_id=payload.project_id, actor=payload.actor
        )
