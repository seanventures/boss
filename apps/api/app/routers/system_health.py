"""System health: the host BOSS runs on, as BOSS sees it (stability spec §4c-§4e).

Thin routes over engine services: deploy readiness (drain), and — with the system doctor — the
gauges, the findings that need a person, what BOSS did on its own, and the operator's choices.
"""
from __future__ import annotations

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
from typing import Any

from typing import Optional, Union

from fastapi import APIRouter
from pydantic import BaseModel, Field

from boss_engine.db import begin, get_engine

router = APIRouter(tags=["system-health"])


class BuildsAtOnceBody(BaseModel):
    """"auto" (derived from this machine) or a number of builds to run at once."""

    value: Optional[Union[int, str]] = Field(default="auto",
                                             description='"auto" or 1-8 builds at once')


@router.get("/v1/system/deploy-readiness")
@permit(P.SYSTEM_READ, scope=Scope.org())
def get_deploy_readiness() -> dict[str, Any]:
    """Would an update now interrupt builds, and did the operator ask to hold updates while
    builds run? Deploy scripts (remote.sh update) wait on this before recreating containers."""
    from boss_engine.services.drain import deploy_readiness

    with begin(get_engine()) as connection:
        return deploy_readiness(connection)


@router.get("/v1/system/health")
@permit(P.SYSTEM_READ, scope=Scope.org())
def get_system_health() -> dict[str, Any]:
    """Gauges with their scope ("This server" / "Docker Desktop VM, not your Mac"), the
    degradation level, and admission ("Builds at once: 2 (auto) · 1 queued, waiting for
    0.6 GB"). The worker reads `admission.builds_at_once.value` to size its lease slots."""
    from boss_engine.services import admission
    from boss_engine.services.obs_view import health_body

    cap = admission.sample()
    with begin(get_engine()) as connection:
        state = admission.snapshot(connection, cap=cap)
    # One derivation: Observability › System shows this same body (obs_view.system).
    return health_body(cap, state)


@router.put("/v1/projects/{project_id}/settings/builds_at_once")
@permit(P.PROJECT_UPDATE, scope=Scope.org())
def put_builds_at_once(project_id: str, payload: BuildsAtOnceBody) -> dict[str, Any]:
    """`{"value": "auto"}` or `{"value": N}`. Install-wide: builds share one machine."""
    from fastapi import HTTPException

    from boss_engine.services import admission

    raw = payload.value
    if raw in (None, "", "auto"):
        value: Any = None
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="value must be 'auto' or a number")
        if not 1 <= value <= admission.MAX_SLOTS:
            raise HTTPException(status_code=422,
                                detail=f"value must be between 1 and {admission.MAX_SLOTS}")
    cap = admission.sample()
    with begin(get_engine()) as connection:
        admission.set_state(connection, "builds_at_once",
                            {"value": value, "set_for_project": project_id})
        state = admission.snapshot(connection, cap=cap)
    return {"builds_at_once": state["builds_at_once"], "label": state["label"]}


class FindingActBody(BaseModel):
    """One of the card's options, by key."""

    option: str = Field(..., min_length=1, max_length=120, description="The option's key")
    actor: Optional[str] = Field(default=None, max_length=120,
                                 description="Who chose it; defaults to the caller")


class HostSampleBody(BaseModel):
    """What the optional host helper (`boss doctor --host`, macOS) measures of the machine
    under Docker Desktop, which BOSS cannot see from inside its VM."""

    swap_used_mb: float = Field(default=0, ge=0)
    swap_total_mb: float = Field(default=0, ge=0)
    memory_pressure: Optional[str] = Field(default=None, max_length=40)
    top: list[dict[str, Any]] = Field(default_factory=list, max_length=8,
                                      description='[{"name": "Chrome", "mb": 4192}, …]')


def _actor(explicit: Optional[str]) -> str:
    if explicit and explicit.strip():
        return explicit.strip()[:120]
    try:
        from apps.api.app.main import current_api_principal
        return str(current_api_principal.get() or "operator")
    except Exception:  # noqa: BLE001
        return "operator"


@router.get("/v1/system-doctor/findings")
@permit(P.SYSTEM_READ, scope=Scope.org())
def get_findings(include_resolved: bool = False) -> dict[str, Any]:
    """What the doctor has found: open (and snoozed) findings, each with its options."""
    from sqlalchemy import text

    from boss_engine.services import system_doctor

    with begin(get_engine()) as connection:
        found = system_doctor.open_findings(connection)
        resolved = []
        if include_resolved:
            resolved = [dict(r) for r in connection.execute(text(
                "SELECT id, kind, title, resolved_at, chosen_option, chosen_by FROM "
                "system_findings WHERE status = 'resolved' ORDER BY resolved_at DESC LIMIT 50"
            )).mappings()]
    return {"findings": found, "resolved": resolved, "last_tick": system_doctor.last_report()}


@router.post("/v1/system-doctor/findings/{finding_id}/act")
@permit(P.SYSTEM_OPERATE, scope=Scope.org())
def act_on_finding(finding_id: str, payload: FindingActBody) -> dict[str, Any]:
    """Do what the operator chose on a system-health card; answers what happened, in place."""
    from fastapi import HTTPException

    from boss_engine.config import get_settings
    from boss_engine.services import system_doctor

    try:
        return system_doctor.act(get_engine(), get_settings(), finding_id, payload.option,
                                 actor=_actor(payload.actor))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except system_doctor.ActRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/v1/system-doctor/actions")
@permit(P.SYSTEM_READ, scope=Scope.org())
def get_actions(since: Optional[str] = None, limit: int = 100) -> dict[str, Any]:
    """Everything the doctor did on its own and everything a person chose, newest first."""
    from boss_engine.services import system_doctor

    with begin(get_engine()) as connection:
        return {"actions": system_doctor.recent_actions(connection, since=since, limit=limit)}


@router.post("/v1/self/host-sample")
@permit(P.SYSTEM_OPERATE, scope=Scope.org())
def post_host_sample(payload: HostSampleBody) -> dict[str, Any]:
    """The Mac under Docker Desktop, as the optional helper measured it."""
    from boss_engine.services import system_doctor

    with begin(get_engine()) as connection:
        return {"recorded": system_doctor.record_host_sample(connection, payload.model_dump())}
