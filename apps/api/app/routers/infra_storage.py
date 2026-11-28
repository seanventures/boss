"""Infra Agent · storage self-healing (BOSSAPP-1145/1151/1155/1157): the HTTP surface of
services/storage_watch, storage_incidents, storage_cleanup, storage_rules, work_hold, host_jobs.

The contract the console builds against is docs/infra/INFRA_AGENT_API.md. Routes stay thin: each
is one call into a service. Reads are `system:read` (every viewer, the strip polls them on every
area); operator actions are `autonomy:manage` (owner/admin, step-up — the same seat as the
install-wide ambient stop); the host helper's routes are `system:operate` with the host key
(as /v1/self/host-sample).
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit

from ..identity_guard import require_host_tools_principal, writing_get

router = APIRouter(tags=["infra-storage"])


def _actor(request: Request, claimed: Optional[str] = None) -> str:
    principal = getattr(request.state, "principal", None)
    pid = getattr(principal, "id", None) or getattr(principal, "principal_id", None)
    return str(pid or claimed or "operator")


class AcknowledgeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: Optional[str] = None


class ReleaseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="", max_length=500)
    actor: Optional[str] = None


class CleanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host_id: Optional[str] = Field(default=None, max_length=120)
    dry_run: bool = False
    actor: Optional[str] = None


class ReplanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str = Field(min_length=1, max_length=80)
    args: dict[str, Any] = Field(default_factory=dict)


class RuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Optional[str] = None
    host_id: Optional[str] = Field(default=None, max_length=120)
    kind: str = Field(min_length=1, max_length=40)
    rule: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class RulesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules: list[RuleIn] = Field(default_factory=list)
    thresholds: Optional[dict[str, float]] = None
    host_id: Optional[str] = Field(default=None, max_length=120)
    actor: Optional[str] = None


class ComposeService(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    service: str = Field(min_length=1, max_length=255)
    state: str = Field(min_length=1, max_length=40)


class SampleBody(BaseModel):
    """One reading from `boss steward` (docs/infra/INFRA_AGENT_API.md §10)."""
    model_config = ConfigDict(extra="allow")
    host_id: Optional[str] = Field(default=None, max_length=120)
    host_label: Optional[str] = Field(default=None, max_length=80)
    source: str = Field(default="steward", max_length=20)
    compose_services: Optional[list[ComposeService]] = Field(default=None, max_length=1024)
    at: Optional[str] = None


class HostJobResultBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    dry_run: bool = False
    items: list[str] = Field(default_factory=list)
    bytes: int = 0
    error: Optional[str] = Field(default=None, max_length=500)
    plan_hash: str = Field(default="", max_length=64)


# ── console reads ───────────────────────────────────────────────────────────────────────────

@router.get("/v1/infra/storage/summary")
@permit(P.SYSTEM_READ, scope=Scope.org())
def get_summary(host_id: Optional[str] = Query(default=None, max_length=120)) -> dict[str, Any]:
    """Usage by type, 30-day trend, forecast, thresholds, rules, last cleanup, every host (S3)."""
    from boss_engine.services import storage_watch

    with begin(get_engine()) as connection:
        return storage_watch.summary(connection, host_id)


@router.get("/v1/infra/storage/incidents/current")
@permit(P.SYSTEM_READ, scope=Scope.org())
def get_current_incidents() -> dict[str, Any]:
    """The open incidents with their StripNode[] flow, plan, freed, eta, paused runs, and the
    hold — what the strip (S1) and the Command Center card (S2) poll."""
    from boss_engine.services import storage_incidents

    with begin(get_engine()) as connection:
        return storage_incidents.current(connection)


@router.get("/v1/infra/storage/incidents")
@permit(P.SYSTEM_READ, scope=Scope.org())
def list_incidents(limit: int = Query(default=20, ge=1, le=200),
                   host_id: Optional[str] = Query(default=None, max_length=120)) -> dict[str, Any]:
    from boss_engine.services import storage_incidents

    with begin(get_engine()) as connection:
        return {"incidents": storage_incidents.listing(connection, limit=limit, host_id=host_id)}


@router.get("/v1/infra/storage/incidents/{incident_id}")
@permit(P.SYSTEM_READ, scope=Scope.org())
def get_incident(incident_id: str) -> dict[str, Any]:
    """The full incident: actions (dry runs too), receipts, paused work, decisions, report (S5)."""
    from boss_engine.services import storage_incidents

    with begin(get_engine()) as connection:
        found = storage_incidents.detail(connection, incident_id)
    if found is None:
        raise HTTPException(status_code=404, detail="no such storage incident")
    return found


@router.get("/v1/infra/storage/incidents/{incident_id}/report.md", response_class=PlainTextResponse)
@permit(P.SYSTEM_READ, scope=Scope.org())
def get_incident_report_md(incident_id: str) -> Any:
    from boss_engine.services import storage_incidents

    with begin(get_engine()) as connection:
        inc = storage_incidents.get(connection, incident_id)
        if inc is None:
            raise HTTPException(status_code=404, detail="no such storage incident")
        body = storage_incidents.report_markdown(connection, inc)
    return PlainTextResponse(body, media_type="text/markdown")


@router.get("/v1/infra/storage/hold")
@permit(P.SYSTEM_READ, scope=Scope.org())
def get_hold() -> dict[str, Any]:
    from boss_engine.services import work_hold

    with begin(get_engine()) as connection:
        return {"hold": work_hold.status(connection)}


@router.get("/v1/infra/storage/rules")
@permit(P.SYSTEM_READ, scope=Scope.org())
def get_rules(host_id: Optional[str] = Query(default=None, max_length=120)) -> dict[str, Any]:
    from boss_engine.services import storage_rules

    with begin(get_engine()) as connection:
        storage_rules.ensure_defaults(connection)
        return {"rules": storage_rules.list_rules(connection, host_id),
                "thresholds": storage_rules.thresholds(connection, host_id)}


# ── console actions ─────────────────────────────────────────────────────────────────────────

@router.post("/v1/infra/storage/incidents/{incident_id}/acknowledge")
@permit(P.SYSTEM_OPERATE, scope=Scope.org())
def acknowledge_incident(incident_id: str, request: Request,
                         payload: Optional[AcknowledgeBody] = None) -> dict[str, Any]:
    """The strip stops flashing and keeps a status line until resolved. Idempotent."""
    from boss_engine.services import storage_incidents

    with begin(get_engine()) as connection:
        found = storage_incidents.acknowledge(connection, incident_id,
                                              _actor(request, (payload or AcknowledgeBody()).actor))
    if found is None:
        raise HTTPException(status_code=404, detail="no such storage incident")
    return {"incident": found}


@router.post("/v1/infra/storage/clean")
@permit(P.AUTONOMY_MANAGE, scope=Scope.org())
def clean_now(request: Request, payload: Optional[CleanBody] = None) -> dict[str, Any]:
    """Tier 1 now, dry-run first, with no hold: opens (or reuses) a warn incident and runs it."""
    from boss_engine.services import storage_cleanup, storage_incidents, storage_rules, storage_watch

    body = payload or CleanBody()
    engine = get_engine()
    with begin(engine) as connection:
        host_id = body.host_id or (storage_watch.hosts(connection) or [storage_watch.local_host_id()])[0]
        inc = storage_incidents.current_for_host(connection, host_id)
        sample = storage_watch.host_disk_sample(connection, host_id) or {"host_id": host_id}
        th = storage_rules.thresholds(connection, host_id)
        if inc is None:
            from datetime import datetime, timezone

            from sqlalchemy import text

            from boss_engine.services.core import make_id

            inc_id = make_id("si")
            free_now = storage_watch.free_pct_of(sample)
            incident_id = connection.execute(text("""
                INSERT INTO storage_incidents (id, host_id, host_label, status, severity, opened_at,
                       cause_key, cause_text, free_pct_start, free_pct_now, target_pct, project_id)
                VALUES (:id, :h, :hl, 'warn', 'warn', :now, 'operator.clean_now',
                        'Cleanup asked for from the console.', :fp, :fp, :t, :p)
                ON CONFLICT (host_id) WHERE status <> 'resolved' DO UPDATE SET host_id = EXCLUDED.host_id
                RETURNING id"""),
                {"id": inc_id, "h": host_id, "hl": sample.get("host_label"),
                 "now": datetime.now(timezone.utc), "fp": free_now, "t": th["target_free_pct"],
                 "p": storage_incidents.default_project(connection)}).scalar_one()
        else:
            incident_id = inc["id"]
        storage_cleanup.request_safe_cleanup(connection, incident_id, sample)
    if not body.dry_run:
        storage_cleanup.advance(engine, incident_id, actor=_actor(request, body.actor))
    with begin(engine) as connection:
        row = storage_incidents.get(connection, incident_id)
        return {"incident": storage_incidents.public(connection, row)}


@router.post("/v1/infra/storage/incidents/{incident_id}/replan")
@permit(P.AUTONOMY_MANAGE, scope=Scope.org())
def replan_incident_step(incident_id: str, payload: ReplanBody) -> dict[str, Any]:
    """"Keep 20 instead": the tier-3 step is dry-run again with these arguments and re-asked."""
    from boss_engine.services import storage_cleanup, storage_incidents

    with begin(get_engine()) as connection:
        step = storage_cleanup.replan_step(connection, incident_id, tool=payload.tool, args=payload.args)
        if step is None:
            raise HTTPException(status_code=404, detail="no such step waiting on this incident")
        row = storage_incidents.get(connection, incident_id)
        return {"incident": storage_incidents.public(connection, row), "step": step}


@router.post("/v1/infra/storage/hold/release")
@permit(P.AUTONOMY_MANAGE, scope=Scope.org())
def release_hold(request: Request, payload: Optional[ReleaseBody] = None) -> dict[str, Any]:
    """The safety valve: clear the hold by hand; paused work resumes oldest first."""
    from boss_engine.services import work_hold

    body = payload or ReleaseBody()
    with begin(get_engine()) as connection:
        out = work_hold.release(connection, actor=f"person:{_actor(request, body.actor)}",
                                reason=body.reason or "released from the console")
    return out


@router.put("/v1/infra/storage/rules")
@permit(P.AUTONOMY_MANAGE, scope=Scope.org())
def put_rules(request: Request, payload: RulesBody) -> dict[str, Any]:
    """Retention rules and thresholds. An absent id creates; a known kind/host updates; nothing is
    deleted (disable instead)."""
    from boss_engine.services import storage_rules

    actor = _actor(request, payload.actor)
    with begin(get_engine()) as connection:
        storage_rules.ensure_defaults(connection)
        for r in payload.rules:
            if r.kind not in storage_rules.KINDS:
                raise HTTPException(status_code=422, detail=f"kind must be one of {', '.join(storage_rules.KINDS)}")
            try:
                storage_rules.upsert(connection, kind=r.kind, rule=r.rule, host_id=r.host_id or payload.host_id,
                                     enabled=r.enabled, source="operator", actor=actor, rule_id=r.id)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        if payload.thresholds:
            unknown = sorted(set(payload.thresholds) - set(storage_rules.DEFAULT_THRESHOLDS))
            if unknown:
                raise HTTPException(status_code=422, detail=f"unknown thresholds: {', '.join(unknown)}")
            try:
                storage_rules.upsert(connection, kind="threshold", rule=payload.thresholds,
                                     host_id=payload.host_id, source="operator", actor=actor)
            except ValueError as exc:      # out of order: 422 naming the keys, never a 500
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"rules": storage_rules.list_rules(connection, payload.host_id),
                "thresholds": storage_rules.thresholds(connection, payload.host_id)}


# ── the host helper (`boss steward`) ────────────────────────────────────────────────────────

@router.post("/v1/infra/storage/samples")
@permit(P.SYSTEM_OPERATE, scope=Scope.org())
def post_sample(payload: SampleBody) -> dict[str, Any]:
    """One reading from the host (df, docker system df, Docker.raw, backups, logs)."""
    from boss_engine.services import storage_incidents, storage_rules, storage_watch

    require_host_tools_principal()
    source = payload.source if payload.source in storage_watch.SOURCES else "steward"
    raw = payload.model_dump()
    try:
        sample = storage_watch.clean_sample(raw, source=source)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with begin(get_engine()) as connection:
        storage_watch.record_sample(connection, sample)
        if source == "steward" and payload.compose_services is not None and sample["host_id"] == storage_watch.local_host_id():
            from boss_engine.services import admission
            admission.set_state(connection, "compose_service_snapshot", {
                "at": sample["at"].isoformat(), "services": [x.model_dump() for x in payload.compose_services],
            })
        inc = storage_incidents.current_for_host(connection, sample["host_id"])
        th = storage_rules.thresholds(connection, sample["host_id"])
        level = storage_watch.raw_level(sample, None, th)
    return {"recorded": True, "level": level, "incident_id": (inc or {}).get("id"),
            "sample_every_seconds": int(storage_watch.INTERVAL_SECONDS)}


@router.get("/v1/host-jobs/public-key")
@permit(P.SYSTEM_READ, scope=Scope.org())
def get_host_jobs_public_key() -> dict[str, Any]:
    """The Ed25519 key the helper verifies every job against (pinned on first use: TOFU)."""
    from boss_engine.services import host_jobs

    require_host_tools_principal()
    with begin(get_engine()) as connection:
        return host_jobs.public_key(connection)


# A LEASE IS A WRITE: declared a writing GET, with its POST twin at /v1/host-jobs/lease/load (the
# method the helper moves to); the GET stays as the alias today's helpers call.
@writing_get(router, "/v1/host-jobs/lease")
@permit(P.SYSTEM_OPERATE, scope=Scope.org(), writes_on_get=True)
def lease_host_job(host_id: str = Query(min_length=1, max_length=120),
                   leased_to: str = Query(default="steward", max_length=120)) -> dict[str, Any]:
    """Zero or one signed, typed job for this host (the helper's pull; a lease is a write)."""
    from boss_engine.services import host_jobs

    require_host_tools_principal()
    with begin(get_engine()) as connection:
        return {"job": host_jobs.lease(connection, host_id=host_id, leased_to=leased_to)}


@router.post("/v1/host-jobs/{job_id}/result")
@permit(P.SYSTEM_OPERATE, scope=Scope.org())
def post_host_job_result(job_id: str, payload: HostJobResultBody) -> dict[str, Any]:
    """The helper's report: bytes freed and the exact item list; the executor reads it on its tick."""
    from boss_engine.services import host_jobs

    require_host_tools_principal()
    with begin(get_engine()) as connection:
        found = host_jobs.record_result(connection, job_id, payload.model_dump())
    if found is None:
        raise HTTPException(status_code=404, detail="no such leased host job")
    return {"recorded": True, "status": found["status"]}
