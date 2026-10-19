"""HTTP surface for monitoring and alerting, Phase 1a (BOSSAPP-598; engine services/monitoring.py,
alert_adapters.py, metric_ingest.py, notify.py, monitor_scheduler.py).

INGEST (public by route; each request proves itself with its SOURCE's token — the project comes
from the token, never from the payload):
* ``POST /v1/ingest/otlp/v1/metrics``    OTLP/HTTP metrics (protobuf or JSON, gzip)
* ``POST /v1/ingest/otlp/v1/{traces,logs}`` refused in words (501)
* ``POST /v1/ingest/prom/remote_write``  Prometheus remote write v1
* ``POST /v1/ingest/{source_id}/{kind}`` events | alertmanager | grafana | pagerduty | sentry |
  datadog | cloudwatch | heartbeat. ``202 {alert_id, status, word}``; any refusal is a 4xx
  with a sentence.
* ``GET/POST /v1/alert-actions/{token}`` an email's signed single-use ack link (GET shows a
  confirm page, so a link scanner never spends it; POST acts).
* ``GET /monitoring/watchdog``           the always-firing BossWatchdog, for an external
  dead-man's switch.

CONSOLE (observability:read / observability:manage; reliability:respond for ack):
Connect an app, its live first-data check, services, signal sources (rotate, test), synthetic
checks, contact points (test, verify), notification policies and their preview, matcher
silences, maintenance windows, inhibit rules, ack (one or many), a rule's 24 h preview, the
outbox, and the worker's tick.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from boss_engine.db import begin
from boss_engine.identity import P, PublicReason, Scope, permit, public


class ProjectBody(BaseModel):
    project_id: str
    actor: Optional[str] = None


class ConnectBody(ProjectBody):
    #: otel | collector | remote_write | metrics_url | url | alerts | boss
    path: str
    name: str = ""
    environment: str = ""
    url: Optional[str] = None
    #: for path=alerts: alertmanager | grafana | pagerduty_v2 | sentry | datadog |
    #: cloudwatch_sns | generic | heartbeat
    tool: Optional[str] = None
    heartbeat_every_s: Optional[int] = None
    expect_status: Optional[int] = None
    expect_contains: Optional[str] = None


class SourceBody(ProjectBody):
    kind: str
    name: str = ""
    service_id: Optional[str] = None
    require_signature: bool = False
    heartbeat_every_s: Optional[int] = None


class CheckBody(ProjectBody):
    service_id: str
    kind: str = "http"
    target: str
    method: str = "GET"
    interval_s: int = 60
    expect: dict[str, Any] = Field(default_factory=dict)


class ContactBody(ProjectBody):
    kind: str
    name: str = ""
    address: Optional[str] = None
    url: Optional[str] = None


class VerifyBody(ProjectBody):
    code: str


class PolicyBody(ProjectBody):
    parent_id: Optional[str] = None
    name: str = ""
    matchers: Any = Field(default_factory=list)
    continue_matching: bool = False
    group_by: Optional[list[str]] = None
    group_wait_s: Optional[int] = None
    group_interval_s: Optional[int] = None
    repeat_interval_s: Optional[int] = None
    contact_point_ids: Optional[list[str]] = None
    quiet_hours: Optional[dict[str, Any]] = None
    position: int = 0


class PreviewBody(ProjectBody):
    labels: dict[str, str] = Field(default_factory=dict)


class RuleNotifyBody(ProjectBody):
    contact_point_ids: list[str] = Field(default_factory=list)


class SilenceBody(ProjectBody):
    matchers: Any
    minutes: float = 60
    reason: str = ""


class WindowBody(ProjectBody):
    name: str
    matchers: Any = Field(default_factory=list)
    rrule: str = ""
    starts_at: datetime
    duration_min: int = 60
    timezone: str = "UTC"
    mode: str = "mute"


class InhibitBody(ProjectBody):
    name: str
    source_matchers: Any
    target_matchers: Any
    equal: list[str] = Field(default_factory=list)


class AckBody(ProjectBody):
    alert_ids: list[str] = Field(default_factory=list)
    incident_id: Optional[str] = None


class RulePreviewBody(ProjectBody):
    subject_id: str
    expr: str
    #: "5m" or seconds
    for_: Any = Field(default=0, alias="for")


_INGEST_STATUS = {"accepted": 202, "deduplicated": 202}


def build_monitoring_router(
    *,
    engine,
    settings: Callable[[], Any],
    require_write_actor: Callable[..., str],
    require_reader: Callable[..., None],
    reader_project: Callable[[str], str],
    viewer_of: Callable[[Optional[str]], Optional[str]],
    etagged: Callable[[Request, Response, dict], Any],
) -> APIRouter:
    from boss_engine.services import alert_adapters as ada
    from boss_engine.services import app_alerts as aa
    from boss_engine.services import metric_ingest as mi
    from boss_engine.services import monitor_scheduler as ms
    from boss_engine.services import monitoring as mon
    from boss_engine.services import notify

    router = APIRouter(tags=["monitoring"])

    def refused(exc: mon.IngestRefused) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.word,
                                                             "word": exc.word})

    def _read(project_id: str, request: Request, response: Response, build):
        pid = reader_project(project_id)
        with begin(engine) as connection:
            require_reader(connection, pid)
            try:
                body = build(connection, pid)
            except LookupError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
        return etagged(request, response, jsonable_encoder(body))

    def _write(payload: ProjectBody, fn):
        pid = reader_project(payload.project_id)
        with begin(engine) as connection:
            actor = require_write_actor(connection, pid, payload.actor, "providerops_manage")
            try:
                return jsonable_encoder(fn(connection, pid, actor))
            except mon.IngestRefused as exc:
                raise HTTPException(status_code=exc.status, detail=exc.word) from exc
            except LookupError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            except ValueError as invalid:
                raise HTTPException(status_code=422, detail=str(invalid)) from invalid

    # ── ingest ──────────────────────────────────────────────────────────────────────────────
    async def _body(request: Request, cap: int) -> bytes:
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > cap:
                raise mon.IngestRefused(413, f"The body is larger than {cap // (1024 * 1024)} MB")
        return raw

    @router.post("/v1/ingest/otlp/v1/metrics", status_code=202)
    @public(reason=PublicReason.HMAC_SIGNED_WEBHOOK, verifier=mon.authenticate)
    async def ingest_otlp(request: Request):
        now = datetime.now(timezone.utc)
        src = None
        try:
            raw = await _body(request, mon.MAX_OTLP_BYTES)
            with begin(engine) as c:
                src = mon.authenticate(c, headers=dict(request.headers), raw=raw,
                                       kinds=("otlp",), max_bytes=mon.MAX_OTLP_BYTES, now=now)
            with begin(engine) as c:
                got = mi.ingest_otlp(c, settings(), src, raw,
                                     content_type=request.headers.get("content-type", ""),
                                     content_encoding=request.headers.get("content-encoding",
                                                                          ""), now=now)
                mon.accepted(c, src["id"], now)
        except mon.IngestRefused as exc:
            if exc.source_id is None and src:
                exc.source_id = src["id"]
            mon.record_refusal(engine, exc, now)
            return refused(exc)
        # OTLP/HTTP's success answer is an ExportMetricsServiceResponse; an empty JSON object
        # is valid for both encodings' clients.
        return JSONResponse(status_code=200, content={"partialSuccess": {}, "word": got["word"]})

    @router.post("/v1/ingest/otlp/v1/{signal}")
    @public(reason=PublicReason.HMAC_SIGNED_WEBHOOK, verifier=mon.authenticate)
    def ingest_otlp_other(signal: str):
        word = mi.TRACES_WORD if signal == "traces" else mi.LOGS_WORD if signal == "logs" else \
            "Unknown OTLP signal; send metrics to /v1/ingest/otlp/v1/metrics"
        return JSONResponse(status_code=501, content={"detail": word, "word": word})

    @router.post("/v1/ingest/prom/remote_write", status_code=204)
    @public(reason=PublicReason.HMAC_SIGNED_WEBHOOK, verifier=mon.authenticate)
    async def ingest_remote_write(request: Request):
        now = datetime.now(timezone.utc)
        src = None
        try:
            raw = await _body(request, mon.MAX_OTLP_BYTES)
            with begin(engine) as c:
                src = mon.authenticate(c, headers=dict(request.headers), raw=raw,
                                       kinds=("remote_write",), max_bytes=mon.MAX_OTLP_BYTES,
                                       now=now)
            with begin(engine) as c:
                got = mi.ingest_remote_write(c, settings(), src, raw, content_encoding=(
                    request.headers.get("content-encoding") or "snappy"), now=now)
                mon.accepted(c, src["id"], now)
        except mon.IngestRefused as exc:
            if exc.source_id is None and src:
                exc.source_id = src["id"]
            mon.record_refusal(engine, exc, now)
            return refused(exc)
        return Response(status_code=204, headers={"X-BOSS-Word": got["word"][:200]})

    @router.post("/v1/ingest/{source_id}/{kind}", status_code=202)
    @public(reason=PublicReason.HMAC_SIGNED_WEBHOOK, verifier=mon.authenticate)
    async def ingest_events(source_id: str, kind: str, request: Request):
        from boss_engine.services import alert_ingest

        now = datetime.now(timezone.utc)
        try:
            raw = await _body(request, mon.MAX_BODY_BYTES)
            got = alert_ingest.receive(engine, settings(), source_id=source_id, kind=kind,
                                       headers=dict(request.headers),
                                       query=dict(request.query_params), raw=raw, now=now)
        except mon.IngestRefused as exc:
            return refused(exc)
        return JSONResponse(status_code=202, content=jsonable_encoder(got))

    # ── the email's ack link ────────────────────────────────────────────────────────────────
    @router.get("/v1/alert-actions/{token}")
    @public(reason=PublicReason.HMAC_SIGNED_WEBHOOK, verifier=mon.use_action_token)
    def action_page(token: str):
        with begin(engine) as c:
            seen = mon.peek_action_token(c, token)
        if not seen:
            return HTMLResponse("<p>This link is not one BOSS sent.</p>", status_code=404)
        if not seen["usable"]:
            return HTMLResponse(f"<p>{seen['word']}.</p>", status_code=410)
        label = "Acknowledge" if seen["action"] == "ack" else "Resolve"
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><meta name=viewport "
            "content='width=device-width'><title>BOSS</title><body style='font:16px "
            "system-ui;margin:2rem'><p>" + label + " this alert? Acknowledging stops repeats; "
            "it does not silence anything.</p><form method=post><button style='font-size:1rem;"
            "padding:.6rem 1.2rem'>" + label + "</button></form></body>")

    @router.post("/v1/alert-actions/{token}")
    @public(reason=PublicReason.HMAC_SIGNED_WEBHOOK, verifier=mon.use_action_token)
    def action_use(token: str, request: Request):
        try:
            with begin(engine) as c:
                got = mon.use_action_token(c, token)
        except ValueError as exc:
            if "text/html" in request.headers.get("accept", ""):
                return HTMLResponse(f"<p>{exc}.</p>", status_code=410)
            raise HTTPException(status_code=410, detail=str(exc)) from exc
        if "text/html" in request.headers.get("accept", ""):
            return HTMLResponse("<p>Done: " + ("acknowledged; repeats stop for 4 hours."
                                               if got["action"] == "ack" else "resolved.")
                                + "</p>")
        return jsonable_encoder(got)

    @router.get("/monitoring/watchdog")
    @public(reason=PublicReason.LIVENESS)
    def watchdog():
        return PlainTextResponse(ms.WATCHDOG, media_type="text/plain; version=0.0.4")

    # ── Connect an app ──────────────────────────────────────────────────────────────────────
    def _snippet(base: str, path: str, source: dict, token: str, svc: dict) -> dict[str, Any]:
        sid = source["id"]
        if path in ("otel", "collector"):
            values = [{"name": "OTEL_EXPORTER_OTLP_ENDPOINT", "value": f"{base}/v1/ingest/otlp"},
                      {"name": "OTEL_EXPORTER_OTLP_HEADERS",
                       "value": f"authorization=Bearer {token}"},
                      {"name": "OTEL_SERVICE_NAME", "value": svc["name"]}]
            block = (f"exporters:\n  otlphttp/boss:\n    endpoint: {base}/v1/ingest/otlp\n"
                     f"    headers:\n      authorization: \"Bearer {token}\"\n")
            return {"values": values, "block": block if path == "collector" else None}
        if path == "remote_write":
            return {"values": [], "block": (
                f"remote_write:\n  - url: {base}/v1/ingest/prom/remote_write\n"
                f"    authorization:\n      credentials: {token}\n")}
        kind = source["kind"]
        endpoint = {"alertmanager": "alertmanager", "grafana": "grafana",
                    "pagerduty_v2": "pagerduty", "generic": "events", "sentry": "sentry",
                    "datadog": "datadog", "cloudwatch_sns": "cloudwatch",
                    "heartbeat": "heartbeat"}[kind]
        url = f"{base}/v1/ingest/{sid}/{endpoint}"
        values = [{"name": "URL", "value": url}, {"name": "Token", "value": token}]
        block = None
        if kind == "alertmanager":
            block = (f"receivers:\n  - name: boss\n    webhook_configs:\n      - url: {url}\n"
                     f"        http_config:\n          authorization:\n"
                     f"            credentials: {token}\n        send_resolved: true\n")
        elif kind == "datadog":
            block = json.dumps(ada.DATADOG_TEMPLATE, indent=2)
        elif kind in ("sentry", "cloudwatch_sns"):
            values = [{"name": "URL", "value": f"{url}?token={token}"}]
        elif kind == "pagerduty_v2":
            values.append({"name": "routing_key", "value": token})
        elif kind == "heartbeat":
            block = f"curl -fsS -X POST -H 'Authorization: Bearer {token}' {url}"
        return {"values": values, "block": block}

    @router.post("/v1/monitoring/connect", status_code=201)
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def connect(payload: ConnectBody):
        """One sheet, one question (how does it talk?), the 2-3 values to paste, shown ONCE."""
        base = (str(getattr(settings(), "public_url", "") or "")
                or str(getattr(settings(), "app_base_url", "") or "")).rstrip("/") or \
            "https://<your BOSS address>"

        def go(c, pid, actor):
            path = payload.path
            if path == "boss":
                return {"path": path, "word": "Nothing to do: every app BOSS deploys is "
                        "watched from its first deploy (built-in rules, health checks)."}
            if path not in ("otel", "collector", "remote_write", "metrics_url", "url", "alerts"):
                raise ValueError("path is otel, collector, remote_write, metrics_url, url, "
                                 "alerts or boss")
            name = (payload.name or "").strip()
            if path in ("otel", "metrics_url", "url") and not name:
                raise ValueError("Name the service (for OpenTelemetry, its OTEL_SERVICE_NAME)")
            svc = mon.find_or_create_service(c, pid, name or (payload.tool or path),
                                             payload.environment, actor=actor,
                                             path=path)
            mon.audit(c, pid, "service", svc["id"], "connected", after={"path": path},
                      actor=actor)
            if path == "metrics_url":
                if not (payload.url or "").startswith(("http://", "https://")):
                    raise ValueError("Give the full /metrics URL")
                c.execute(text_sql("UPDATE monitored_services SET onboarding = onboarding || "
                                   "jsonb_build_object('scrape_url', CAST(:u AS TEXT)) "
                                   "WHERE id = :s"), {"u": payload.url, "s": svc["id"]})
                return {"path": path, "service": mon.service(c, pid, svc["id"]),
                        "word": "BOSS reads it every minute; waiting for the first read."}
            if path == "url":
                chk = _add_check(c, pid, actor, svc["id"], "http", payload.url or "", 60,
                                 {k: v for k, v in (("status", payload.expect_status),
                                                    ("contains", payload.expect_contains))
                                  if v})
                return {"path": path, "service": mon.service(c, pid, svc["id"]),
                        "check": chk, "word": "Checking it every minute from BOSS."}
            kind = {"otel": "otlp", "collector": "otlp", "remote_write": "remote_write"}.get(
                path) or (payload.tool or "")
            made = mon.create_source(c, pid, kind=kind, name=name or kind, actor=actor,
                                     service_id=svc["id"],
                                     heartbeat_every_s=payload.heartbeat_every_s)
            return {"path": path, "service": mon.service(c, pid, svc["id"]),
                    "source": made["source"],
                    "paste": _snippet(base, path, made["source"], made["token"], svc),
                    "shown_once": True,
                    "word": "Paste these values; BOSS waits for the first data and says what "
                            "arrived."}
        return _write(payload, go)

    def text_sql(q: str):
        from sqlalchemy import text

        return text(q)

    def _add_check(c, pid, actor, service_id, kind, target, interval, expect):
        from boss_engine.services.core import make_id

        if kind not in ("http", "tcp", "dns"):
            raise ValueError("kind is http, tcp or dns")
        if not target:
            raise ValueError("Give what to check (a URL, host:port or a name)")
        if int(interval) < 30:
            raise ValueError("A synthetic check runs every 30 seconds at the most")
        if not mon.service(c, pid, service_id):
            raise LookupError("No such service")
        cid = make_id("chk")
        c.execute(text_sql(
            "INSERT INTO synthetic_checks (id, project_id, service_id, kind, target, expect, "
            " interval_s, created_by) VALUES (:i, :p, :s, :k, :t, CAST(:e AS JSONB), :iv, :by)"),
            {"i": cid, "p": pid, "s": service_id, "k": kind, "t": target[:500],
             "e": json.dumps(expect or {}), "iv": int(interval), "by": actor})
        mon.audit(c, pid, "synthetic_check", cid, "created",
                  after={"kind": kind, "target": target}, actor=actor)
        return {"id": cid, "kind": kind, "target": target, "interval_s": int(interval)}

    @router.get("/v1/monitoring/services")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def services(project_id: str, request: Request, response: Response):
        def build(c, pid):
            rows = c.execute(text_sql(
                "SELECT id, name, environment, deployment_target_id, dialect, onboarding, "
                " created_at FROM monitored_services WHERE project_id = :p ORDER BY name"),
                {"p": pid}).mappings().all()
            return {"services": [{**dict(r), "onboarding": mon._json(r["onboarding"], {})}
                                 for r in rows]}
        return _read(project_id, request, response, build)

    @router.get("/v1/monitoring/services/{service_id}/live")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def live(service_id: str, project_id: str, request: Request, response: Response,
             since: Optional[str] = None):
        """The sheet's live check: has the first datapoint or event arrived, and what was it?"""
        def build(c, pid):
            svc = mon.service(c, pid, service_id)
            if not svc:
                raise LookupError("No such service")
            ob = svc["onboarding"]
            first = ob.get("last_data_at") or ob.get("first_data_at")
            arrived = bool(first) and (not since or str(first) >= since)
            rules_n = c.execute(text_sql("SELECT count(*) FROM app_alert_rules WHERE "
                                         "subject_id = :s OR service_id = :s"),
                                {"s": svc["id"]}).scalar()
            hint = None
            if not arrived:
                paths = ob.get("paths") or []
                hint = ("Check that OTEL_EXPORTER_OTLP_ENDPOINT and the authorization header "
                        "are set exactly as shown, that the app was restarted, and that it can "
                        "reach BOSS's address." if "otlp" in paths or "otel" in paths else
                        "Check the URL answers from BOSS's network and returns Prometheus text."
                        if "metrics_url" in paths or "scrape" in paths else
                        "Send a test alert from the tool; check the URL and token." )
                if ob.get("scrape_word"):
                    hint = ob["scrape_word"]
            refusal = c.execute(text_sql(
                "SELECT last_refusal_word FROM signal_sources WHERE service_id = :s AND "
                "last_refusal_at > now() - interval '10 minutes' ORDER BY last_refusal_at DESC "
                "LIMIT 1"), {"s": svc["id"]}).scalar()
            return {"service_id": svc["id"], "arrived": arrived,
                    "series": int(ob.get("series") or 0), "events": int(ob.get("events") or 0),
                    "first_data_at": ob.get("first_data_at"),
                    "last_data_at": ob.get("last_data_at"),
                    "word": (f"{int(ob.get('series') or 0)} series from {svc['name']}; "
                             f"{ob.get('recognised') or ''}".strip("; ") if arrived
                             else "Waiting for the first data…"),
                    "rules": int(rules_n or 0), "hint": refusal or hint}
        return _read(project_id, request, response, build)

    # ── signal sources ──────────────────────────────────────────────────────────────────────
    @router.get("/v1/signal-sources")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def list_sources(project_id: str, request: Request, response: Response):
        return _read(project_id, request, response,
                     lambda c, pid: {"sources": mon.sources(c, pid)})

    @router.post("/v1/signal-sources", status_code=201)
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def create_source(payload: SourceBody):
        return _write(payload, lambda c, pid, actor: {**mon.create_source(
            c, pid, kind=payload.kind, name=payload.name, actor=actor,
            service_id=payload.service_id, require_signature=payload.require_signature,
            heartbeat_every_s=payload.heartbeat_every_s), "shown_once": True})

    @router.post("/v1/signal-sources/{source_id}/rotate")
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def rotate(source_id: str, payload: ProjectBody):
        return _write(payload, lambda c, pid, actor: {
            **mon.rotate_source(c, pid, source_id, actor=actor), "shown_once": True,
            "word": "Rotated. The old token keeps working for one hour."})

    @router.post("/v1/signal-sources/{source_id}/test")
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def test_source(source_id: str, payload: ProjectBody):
        """A test event through the same pipeline (a firing then resolved generic alert)."""
        from boss_engine.services import alert_ingest

        def go(c, pid, actor):
            row = c.execute(text_sql("SELECT * FROM signal_sources WHERE id = :i AND "
                                     "project_id = :p"), {"i": source_id, "p": pid}
                            ).mappings().first()
            if not row:
                raise LookupError("No such source")
            return alert_ingest.test_event(c, dict(row), datetime.now(timezone.utc))
        return _write(payload, go)

    # ── synthetic checks ────────────────────────────────────────────────────────────────────
    @router.post("/v1/monitoring/synthetics", status_code=201)
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def add_check(payload: CheckBody):
        return _write(payload, lambda c, pid, actor: _add_check(
            c, pid, actor, payload.service_id, payload.kind, payload.target, payload.interval_s,
            payload.expect))

    @router.get("/v1/monitoring/synthetics")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def list_checks(project_id: str, request: Request, response: Response):
        return _read(project_id, request, response, lambda c, pid: {"checks": [
            dict(r) for r in c.execute(text_sql(
                "SELECT k.id, k.service_id, s.name AS service, k.kind, k.target, k.interval_s, "
                " k.enabled, k.last_run_at, k.last_ok, k.last_word FROM synthetic_checks k JOIN "
                " monitored_services s ON s.id = k.service_id WHERE k.project_id = :p "
                "ORDER BY s.name"), {"p": pid}).mappings()]})

    # ── contact points and the tree ─────────────────────────────────────────────────────────
    @router.get("/v1/contact-points")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def contact_points(project_id: str, request: Request, response: Response):
        def build(c, pid):
            notify.ensure_root(c, pid)
            return {"contact_points": [notify.contact_point_view(
                {**dict(r), "config": mon._json(r["config"], {})}) for r in c.execute(text_sql(
                    "SELECT * FROM contact_points WHERE project_id = :p ORDER BY created_at"),
                    {"p": pid}).mappings()]}
        return _read(project_id, request, response, build)

    @router.post("/v1/contact-points", status_code=201)
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def add_contact_point(payload: ContactBody):
        return _write(payload, lambda c, pid, actor: notify.create_contact_point(
            c, settings(), pid, kind=payload.kind, name=payload.name, address=payload.address,
            url=payload.url, actor=actor))

    @router.post("/v1/contact-points/{cp_id}/test")
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def test_contact_point(cp_id: str, payload: ProjectBody):
        return _write(payload, lambda c, pid, actor: notify.send_test(c, pid, cp_id,
                                                                      actor=actor))

    @router.post("/v1/contact-points/{cp_id}/verify")
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def verify_contact_point(cp_id: str, payload: VerifyBody):
        return _write(payload, lambda c, pid, actor: notify.verify(c, pid, cp_id, payload.code,
                                                                   actor=actor))

    @router.get("/v1/notification-policies")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def list_policies(project_id: str, request: Request, response: Response):
        return _read(project_id, request, response,
                     lambda c, pid: {"policies": notify.policies(c, pid)})

    def _policy_values(payload: PolicyBody) -> dict[str, Any]:
        m = mon.validate_matchers(payload.matchers, allow_empty=True)
        for k in ("group_wait_s", "group_interval_s", "repeat_interval_s"):
            v = getattr(payload, k)
            if v is not None and not 0 <= int(v) <= 7 * 86400:
                raise ValueError(f"{k} is between 0 seconds and 7 days")
        return {"m": json.dumps(m), "c": payload.continue_matching,
                "g": json.dumps(payload.group_by) if payload.group_by is not None else None,
                "gw": payload.group_wait_s, "gi": payload.group_interval_s,
                "ri": payload.repeat_interval_s,
                "cp": json.dumps(payload.contact_point_ids)
                if payload.contact_point_ids is not None else None,
                "q": json.dumps(payload.quiet_hours) if payload.quiet_hours else None,
                "n": payload.name[:120], "pos": int(payload.position)}

    @router.post("/v1/notification-policies", status_code=201)
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def add_policy(payload: PolicyBody):
        from boss_engine.services.core import make_id

        def go(c, pid, actor):
            root = notify.ensure_root(c, pid)
            parent = payload.parent_id or root["id"]
            if not c.execute(text_sql("SELECT 1 FROM notification_policies WHERE id = :i AND "
                                      "project_id = :p"), {"i": parent, "p": pid}).first():
                raise LookupError("No such parent policy")
            v = _policy_values(payload)
            nid = make_id("np")
            c.execute(text_sql(
                "INSERT INTO notification_policies (id, project_id, parent_id, position, name, "
                " matchers, continue_matching, group_by, group_wait_s, group_interval_s, "
                " repeat_interval_s, contact_point_ids, quiet_hours, created_by) VALUES (:i, :p, "
                " :par, :pos, :n, CAST(:m AS JSONB), :c, CAST(:g AS JSONB), :gw, :gi, :ri, "
                " CAST(:cp AS JSONB), CAST(:q AS JSONB), :by)"),
                {**v, "i": nid, "p": pid, "par": parent, "by": actor})
            mon.audit(c, pid, "notification_policy", nid, "created", after=payload.model_dump(
                exclude={"actor"}), actor=actor)
            return {"policies": notify.policies(c, pid)}
        return _write(payload, go)

    @router.put("/v1/notification-policies/{policy_id}")
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def edit_policy(policy_id: str, payload: PolicyBody):
        def go(c, pid, actor):
            v = _policy_values(payload)
            found = c.execute(text_sql(
                "UPDATE notification_policies SET name = :n, matchers = CASE WHEN parent_id IS "
                " NULL THEN '[]'::jsonb ELSE CAST(:m AS JSONB) END, continue_matching = :c, "
                " group_by = COALESCE(CAST(:g AS JSONB), group_by), group_wait_s = :gw, "
                " group_interval_s = :gi, repeat_interval_s = :ri, contact_point_ids = "
                " COALESCE(CAST(:cp AS JSONB), contact_point_ids), quiet_hours = "
                " CAST(:q AS JSONB), position = :pos WHERE id = :i AND project_id = :p "
                "RETURNING id"), {**v, "i": policy_id, "p": pid}).first()
            if not found:
                raise LookupError("No such policy")
            mon.audit(c, pid, "notification_policy", policy_id, "edited",
                      after=payload.model_dump(exclude={"actor"}), actor=actor)
            return {"policies": notify.policies(c, pid)}
        return _write(payload, go)

    @router.delete("/v1/notification-policies/{policy_id}")
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(query="project_id"))
    def delete_policy(policy_id: str, project_id: str, actor: Optional[str] = None):
        return _write(ProjectBody(project_id=project_id, actor=actor), lambda c, pid, who: (
            _delete_policy(c, pid, policy_id, who)))

    def _delete_policy(c, pid, policy_id, actor):
        found = c.execute(text_sql("DELETE FROM notification_policies WHERE id = :i AND "
                                   "project_id = :p AND parent_id IS NOT NULL RETURNING id"),
                          {"i": policy_id, "p": pid}).first()
        if not found:
            raise LookupError("No such policy (the root is never deleted)")
        mon.audit(c, pid, "notification_policy", policy_id, "deleted", actor=actor)
        return {"policies": notify.policies(c, pid)}

    @router.post("/v1/notification-policies/preview")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(body="project_id"), readonly_post=True)
    def preview(payload: PreviewBody):
        pid = reader_project(payload.project_id)
        with begin(engine) as c:
            require_reader(c, pid)
            return jsonable_encoder(notify.preview(c, pid, payload.labels))

    @router.put("/v1/alert-rules/{rule_id}/notify")
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def rule_notify(rule_id: str, payload: RuleNotifyBody):
        def go(c, pid, actor):
            if not c.execute(text_sql("SELECT 1 FROM app_alert_rules WHERE id = :r AND "
                                      "project_id = :p"), {"r": rule_id, "p": pid}).first():
                raise LookupError("No such rule")
            return notify.set_rule_notify(c, pid, rule_id, payload.contact_point_ids,
                                          actor=actor)
        return _write(payload, go)

    @router.get("/v1/notifications")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def list_notifications(project_id: str, request: Request, response: Response):
        return _read(project_id, request, response,
                     lambda c, pid: {"notifications": notify.outbox(c, pid)})

    # ── silences, maintenance, inhibition ───────────────────────────────────────────────────
    @router.get("/v1/silences")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def list_silences(project_id: str, request: Request, response: Response):
        def build(c, pid):
            now = datetime.now(timezone.utc)
            return {"silences": [
                {**{k: v for k, v in dict(r).items()}, "matchers": mon._json(r["matchers"], [])}
                for r in c.execute(text_sql(
                    "SELECT id, matchers, starts_at, ends_at, reason, kind, created_by, "
                    " expired_at FROM silences WHERE project_id = :p AND expired_at IS NULL AND "
                    " ends_at > :now ORDER BY ends_at"), {"p": pid, "now": now}).mappings()],
                "windows": [{**dict(r), "matchers": mon._json(r["matchers"], []),
                             "active_until": mon.iso(mon.window_active(dict(r), now))}
                            for r in c.execute(text_sql(
                                "SELECT * FROM maintenance_windows WHERE project_id = :p "
                                "ORDER BY created_at"), {"p": pid}).mappings()],
                "inhibit_rules": [{k: v for k, v in r.items()
                                   if k not in ("project_id",)}
                                  for r in mon.inhibit_rules(c, pid)]}
        return _read(project_id, request, response, build)

    @router.post("/v1/silences", status_code=201)
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def add_silence(payload: SilenceBody):
        from boss_engine.services.core import make_id

        def go(c, pid, actor):
            m = mon.validate_matchers(payload.matchers)
            if len((payload.reason or "").strip()) < 3:
                raise ValueError("Say why you are silencing it")
            if not 1 <= float(payload.minutes) <= 60 * 24 * 30:
                raise ValueError("A silence lasts between 1 minute and 30 days")
            sid = make_id("sil")
            now = datetime.now(timezone.utc)
            c.execute(text_sql(
                "INSERT INTO silences (id, project_id, matchers, starts_at, ends_at, reason, "
                " kind, created_by) VALUES (:i, :p, CAST(:m AS JSONB), :now, :end, :r, "
                " 'manual', :by)"),
                {"i": sid, "p": pid, "m": json.dumps(m), "now": now,
                 "end": now + timedelta(minutes=float(payload.minutes)),
                 "r": payload.reason.strip()[:500], "by": actor})
            mon.audit(c, pid, "silence", sid, "created", after={"matchers": m,
                      "minutes": payload.minutes}, actor=actor, reason=payload.reason)
            return {"id": sid}
        return _write(payload, go)

    @router.post("/v1/silences/{silence_id}/end")
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def end_silence(silence_id: str, payload: ProjectBody):
        def go(c, pid, actor):
            found = c.execute(text_sql(
                "UPDATE silences SET expired_at = now(), expired_by = :by WHERE id = :i AND "
                "project_id = :p AND expired_at IS NULL RETURNING id"),
                {"i": silence_id, "p": pid, "by": actor}).first()
            if not found:
                raise LookupError("No such silence")
            mon.audit(c, pid, "silence", silence_id, "ended", actor=actor)
            return {"ended": True}
        return _write(payload, go)

    @router.post("/v1/maintenance-windows", status_code=201)
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def add_window(payload: WindowBody):
        from boss_engine.services.core import make_id

        def go(c, pid, actor):
            m = mon.validate_matchers(payload.matchers, allow_empty=True)
            mon.parse_rrule(payload.rrule)
            if payload.mode not in ("mute", "hold"):
                raise ValueError("mode is mute (hold notifications) or hold (alerts go to the "
                                 "agent first)")
            starts = payload.starts_at if payload.starts_at.tzinfo else \
                payload.starts_at.replace(tzinfo=timezone.utc)
            wid = make_id("mw")
            c.execute(text_sql(
                "INSERT INTO maintenance_windows (id, project_id, name, matchers, rrule, "
                " starts_at, duration_min, timezone, mode, created_by) VALUES (:i, :p, :n, "
                " CAST(:m AS JSONB), :r, :s, :d, :tz, :mode, :by)"),
                {"i": wid, "p": pid, "n": payload.name[:120], "m": json.dumps(m),
                 "r": payload.rrule, "s": starts, "d": int(payload.duration_min),
                 "tz": payload.timezone, "mode": payload.mode, "by": actor})
            mon.audit(c, pid, "maintenance_window", wid, "created",
                      after=payload.model_dump(exclude={"actor"}), actor=actor)
            return {"id": wid}
        return _write(payload, go)

    @router.delete("/v1/maintenance-windows/{window_id}")
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(query="project_id"))
    def delete_window(window_id: str, project_id: str, actor: Optional[str] = None):
        def go(c, pid, who):
            found = c.execute(text_sql("DELETE FROM maintenance_windows WHERE id = :i AND "
                                       "project_id = :p RETURNING id"),
                              {"i": window_id, "p": pid}).first()
            if not found:
                raise LookupError("No such maintenance window")
            mon.audit(c, pid, "maintenance_window", window_id, "deleted", actor=who)
            return {"deleted": True}
        return _write(ProjectBody(project_id=project_id, actor=actor), go)

    @router.post("/v1/inhibit-rules", status_code=201)
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def add_inhibit(payload: InhibitBody):
        from boss_engine.services.core import make_id

        def go(c, pid, actor):
            src = mon.validate_matchers(payload.source_matchers)
            tgt = mon.validate_matchers(payload.target_matchers)
            iid = make_id("inh")
            c.execute(text_sql(
                "INSERT INTO inhibit_rules (id, project_id, name, source_matchers, "
                " target_matchers, equal, origin, created_by) VALUES (:i, :p, :n, "
                " CAST(:s AS JSONB), CAST(:t AS JSONB), CAST(:e AS JSONB), 'console', :by)"),
                {"i": iid, "p": pid, "n": payload.name[:120], "s": json.dumps(src),
                 "t": json.dumps(tgt), "e": json.dumps(payload.equal[:10]), "by": actor})
            mon.audit(c, pid, "inhibit_rule", iid, "created",
                      after=payload.model_dump(exclude={"actor"}), actor=actor)
            return {"id": iid}
        return _write(payload, go)

    # ── ack ─────────────────────────────────────────────────────────────────────────────────
    @router.post("/v1/alerts/ack")
    @permit(P.RELIABILITY_RESPOND, scope=Scope.project(body="project_id"))
    def ack(payload: AckBody):
        if not payload.alert_ids and not payload.incident_id:
            raise HTTPException(status_code=422, detail="Name the alerts or the incident")
        return _write(payload, lambda c, pid, actor: mon.ack(
            c, pid, alert_ids=payload.alert_ids, incident_id=payload.incident_id, actor=actor))

    # ── a rule's 24 h preview ───────────────────────────────────────────────────────────────
    @router.post("/v1/alert-rules/preview")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(body="project_id"), readonly_post=True)
    def rule_preview(payload: RulePreviewBody):
        from boss_engine.services import rule_preview as rp

        pid = reader_project(payload.project_id)
        with begin(engine) as c:
            require_reader(c, pid)
            subj = aa.subject(c, payload.subject_id)
            if not subj or subj["project_id"] != pid:
                raise HTTPException(status_code=404, detail="No such app or service")
            prom, scope = aa.datasource_for(c, settings(), subj)
            try:
                return jsonable_encoder(rp.preview(c, subj, payload.expr, payload.for_,
                                                   prometheus=prom, scope=scope))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ── the worker's tick ───────────────────────────────────────────────────────────────────
    @router.post("/v1/monitoring/tick")
    @permit(P.SYSTEM_OPERATE, scope=Scope.org())
    def tick():
        return jsonable_encoder(ms.tick(engine, settings()))

    return router
