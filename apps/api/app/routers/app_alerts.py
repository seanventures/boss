"""HTTP surface for app alerts (engine services/app_alerts.py, app_alerts_view.py; BOSSAPP-598).

* ``GET  /v1/apps/{target_id}/alert-rules`` — the app's rules (Prometheus shape, in words too),
  its signal sources and the last rule changes.
* ``PUT  /v1/apps/{target_id}/alert-rules`` — change rules (or add a custom PromQL one); the
  console's edit wins over boss.deploy.yaml and is audited. Respond automatically asks for the
  person's standing grant (409 consent_required, the console asks once).
* ``POST /v1/apps/{target_id}/alert-rules/{rule_id}/silence`` — silence a rule for a time, with
  a reason; ``POST /v1/apps/{target_id}/alert-silences/{silence_id}/end`` ends it early.
* ``GET  /v1/apps/{target_id}/alerts`` — the app's alert history; ``GET /v1/alerts`` — every app's.
* ``POST /v1/apps/{target_id}/alerts/{alert_id}/triage`` — ask the agent again (after no model).
* ``POST /v1/apps/{target_id}/signal-sources/{source_id}/rotate`` — a new signals token for a
  reporting app; the value is never shown.
* ``POST /v1/observability/grafana/check`` — ask Grafana (and Prometheus) now; a Grafana link is
  shown only from a check that it answered.

Mounted from main.py with the auth helpers injected (as routers/observability_view.py is).
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit


class RulesBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    reason: str = ""
    #: [{id?, params?, severity?, for?, enabled?, triage?, auto_respond?, runbook_url?} | a
    #: custom rule {alert, expr, for, labels, annotations} without an id].
    rules: list[dict[str, Any]] = Field(default_factory=list)


class SilenceBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    minutes: float = 60
    reason: str = ""


class ProjectBody(BaseModel):
    project_id: str
    actor: Optional[str] = None


def build_app_alerts_router(
    *,
    engine,
    settings: Callable[[], Any],
    require_write_actor: Callable[..., str],
    require_reader: Callable[..., None],
    reader_project: Callable[[str], str],
    viewer_of: Callable[[Optional[str]], Optional[str]],
    etagged: Callable[[Request, Response, dict], Any],
) -> APIRouter:
    from boss_engine.services import app_alerts as aa
    from boss_engine.services import app_alerts_view as view

    router = APIRouter(tags=["observability"])

    def _target(connection, project_id: str, target_id: str) -> dict:
        from sqlalchemy import text

        row = connection.execute(text(
            "SELECT * FROM deployment_targets WHERE id = :t AND project_id = :p"),
            {"t": target_id, "p": project_id}).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="No such app in this project")
        return dict(row)

    def _read(project_id: str, request: Request, response: Response, build):
        pid = reader_project(project_id)
        with begin(engine) as connection:
            require_reader(connection, pid)
            try:
                body = build(connection, pid)
            except LookupError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
        return etagged(request, response, jsonable_encoder(body))

    def _rules_body(connection, pid: str, target_id: str) -> dict:
        tab = view.app_alerts_tab(connection, settings(), pid, target_id, state="firing")
        return {k: tab[k] for k in ("target_id", "app", "env", "rules", "sources", "changes",
                                    "evaluator", "evaluator_tip", "export", "grafana",
                                    "severities", "has_samples")}

    @router.get("/v1/apps/{target_id}/alert-rules")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def alert_rules(target_id: str, project_id: str, request: Request, response: Response):
        return _read(project_id, request, response,
                     lambda c, pid: _rules_body(c, pid, target_id))

    @router.put("/v1/apps/{target_id}/alert-rules")
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def put_alert_rules(target_id: str, payload: RulesBody):
        pid = reader_project(payload.project_id)
        if not payload.rules:
            raise HTTPException(status_code=422, detail="Nothing to change")
        with begin(engine) as connection:
            actor = require_write_actor(connection, pid, payload.actor, "providerops_manage")
            target = _target(connection, pid, target_id)
            aa.ensure_rules(connection, target)
            try:
                for change in payload.rules:
                    rid = change.get("id")
                    aa.update_rule(connection, target, str(rid) if rid else None,
                                   {k: v for k, v in change.items() if k != "id"},
                                   actor=actor, reason=payload.reason)
            except aa.RuleRefused as refused:
                raise HTTPException(status_code=422, detail=str(refused)) from refused
            except LookupError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            except ValueError as invalid:
                raise HTTPException(status_code=422, detail=str(invalid)) from invalid
            body = _rules_body(connection, pid, target_id)
        return jsonable_encoder(body)

    @router.post("/v1/apps/{target_id}/alert-rules/{rule_id}/silence")
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def silence_rule(target_id: str, rule_id: str, payload: SilenceBody):
        pid = reader_project(payload.project_id)
        with begin(engine) as connection:
            actor = require_write_actor(connection, pid, payload.actor, "providerops_manage")
            target = _target(connection, pid, target_id)
            try:
                return jsonable_encoder(aa.silence(connection, target, rule_id,
                                                   minutes=payload.minutes,
                                                   reason=payload.reason, actor=actor))
            except aa.RuleRefused as refused:
                raise HTTPException(status_code=422, detail=str(refused)) from refused
            except LookupError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error

    @router.post("/v1/apps/{target_id}/alert-silences/{silence_id}/end")
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def end_silence(target_id: str, silence_id: str, payload: ProjectBody):
        pid = reader_project(payload.project_id)
        with begin(engine) as connection:
            actor = require_write_actor(connection, pid, payload.actor, "providerops_manage")
            target = _target(connection, pid, target_id)
            try:
                return aa.end_silence(connection, target, silence_id, actor=actor)
            except LookupError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error

    @router.get("/v1/apps/{target_id}/alerts")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def app_alerts(target_id: str, project_id: str, request: Request, response: Response,
                   state: str = "all", since: str = "24h", severity: Optional[str] = None):
        def build(c, pid):
            _target(c, pid, target_id)
            return view.alerts(c, pid, target_id=target_id, state=state, since=since,
                               severity=severity)
        return _read(project_id, request, response, build)

    @router.get("/v1/alerts")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def all_alerts(project_id: str, request: Request, response: Response,
                   app: Optional[str] = None, state: str = "firing", since: str = "7d",
                   severity: Optional[str] = None, source: Optional[str] = None,
                   flapping: Optional[bool] = None, acked: Optional[bool] = None,
                   attention: bool = False):
        """Every app's and service's alerts (filters: app or service, state, severity, source
        kind, flapping, acked), with the open incidents in the same words every card uses."""
        return _read(project_id, request, response, lambda c, pid: view.alerts(
            c, pid, target_id=app or None, state=state, since=since, severity=severity,
            source=source or None, flapping=flapping, acked=acked, attention=attention))

    @router.get("/v1/apps/{target_id}/metric-names")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def metric_names(target_id: str, project_id: str, request: Request, response: Response):
        """The app's ACTUAL series names, from what BOSS scraped or was sent (the New alert rule
        builder's metric picker), with the selector its rules use."""
        from boss_engine.services import alert_samples

        def build(c, pid):
            return alert_samples.metric_names(c, _target(c, pid, target_id))
        return _read(project_id, request, response, build)

    @router.post("/v1/apps/{target_id}/alert-rules/samples")
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def add_sample_rules(target_id: str, payload: ProjectBody):
        """Seed the clearly labelled SAMPLE rules for this app (labels.sample="true"); idempotent."""
        from boss_engine.services import alert_samples

        pid = reader_project(payload.project_id)
        with begin(engine) as connection:
            actor = require_write_actor(connection, pid, payload.actor, "providerops_manage")
            target = _target(connection, pid, target_id)
            aa.ensure_rules(connection, target)
            got = alert_samples.seed(connection, target, actor=actor)
        return jsonable_encoder(got)

    @router.post("/v1/apps/{target_id}/alert-rules/samples/remove")
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def remove_sample_rules(target_id: str, payload: ProjectBody):
        """Remove every SAMPLE rule of this app (their alert history stays, marked sample)."""
        from boss_engine.services import alert_samples

        pid = reader_project(payload.project_id)
        with begin(engine) as connection:
            actor = require_write_actor(connection, pid, payload.actor, "providerops_manage")
            target = _target(connection, pid, target_id)
            got = alert_samples.remove(connection, target, actor=actor)
        return jsonable_encoder(got)

    @router.post("/v1/apps/{target_id}/alerts/{alert_id}/triage")
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def triage_again(target_id: str, alert_id: str, payload: ProjectBody):
        from sqlalchemy import text

        pid = reader_project(payload.project_id)
        with begin(engine) as connection:
            require_write_actor(connection, pid, payload.actor, "providerops_manage")
            _target(connection, pid, target_id)
            found = connection.execute(text(
                "UPDATE app_alerts SET triage_state = 'pending', triage_attempt_at = now() "
                "WHERE id = :a AND target_id = :t AND state = 'firing' "
                "AND triage_state IN ('no_model', 'pending') RETURNING id"),
                {"a": alert_id, "t": target_id}).first()
        if not found:
            raise HTTPException(status_code=409,
                                detail="Only a firing alert the agent has not triaged")
        record = aa.triage_alert(engine, settings(), alert_id)
        return jsonable_encoder(record)

    @router.post("/v1/apps/{target_id}/signal-sources/{source_id}/rotate")
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def rotate_source_token(target_id: str, source_id: str, payload: ProjectBody):
        from boss_engine.services import incident_signals

        pid = reader_project(payload.project_id)
        with begin(engine) as connection:
            actor = require_write_actor(connection, pid, payload.actor, "providerops_manage")
            _target(connection, pid, target_id)
            source = _target(connection, pid, source_id)
        got = incident_signals.rotate_token(engine, settings(), project_id=pid,
                                            target_id=source_id, actor=actor)
        return {**got, "word": f"Rotated. Redeploy {source['app_name']} so it has the new "
                               "token."}

    @router.post("/v1/observability/grafana/check")
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def check_grafana(payload: ProjectBody):
        """`Check` beside a missing Grafana link: ask Grafana and Prometheus now, record it."""
        from boss_engine.services import obs_view
        from boss_engine.services.observability_stack import check_endpoint, endpoint_state

        pid = reader_project(payload.project_id)
        with begin(engine) as connection:
            require_reader(connection, pid)
        for kind in ("grafana", "prometheus"):
            check_endpoint(engine, pid, kind)
        obs_view.forget_sources(pid)
        with begin(engine) as connection:
            g = endpoint_state(connection, pid, "grafana")
            p = endpoint_state(connection, pid, "prometheus")
        return jsonable_encoder({
            "grafana": {k: g.get(k) for k in ("state", "url", "reason", "checked_at", "why")},
            "prometheus": {k: p.get(k) for k in ("state", "reason", "checked_at", "why")}})

    return router
